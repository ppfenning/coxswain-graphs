"""One-time importer of historical run records into the run-record store.

Run as `python -m harness.store_backfill RUNS_DIR WORK_DIR LEDGER STORE_URL`. Nothing is
assumed: every path and the store URL are arguments.

The parsers are pure. Each takes parsed data or text and returns store rows built by
harness/store_write.py, plus a count of what it could not read. The edge walks the paths,
writes each row once, and counts per table: records seen, rows inserted, rows already
present, and malformed. A table balances when seen minus malformed equals inserted plus
already present. The command exits non-zero unless every table balances.

Formats: a run record `<run>.json`, a phase record whose run_id is `<run>:<phase>`, a launch
record (`launched_by` and `at`, no run_id) named `<run>.launched.json`, `<run>.launch.json` or
`<run>.json`, call lines `<run>.calls.jsonl`, a usage file `<run>.usage.json`, the ledger, and the
`attempts` list of each task file. Task files are read with `core.workstore.read_item`, the loader
the driver uses. A launch record is folded into its run row and is not a table of its own.
An epic run has only phase records. A launch with no run record builds its run row from the launch
and the run's earliest phase record. A launch with no run record, phase record, call lines or usage
file builds its run row from the launch alone, with status `never_recorded`: the run died before it
recorded anything. Such runs were already imported before that status existed; a rerun labels them.
Ceiling, policy, chair and leader files and dot-files are not records. They are skipped and counted
in `skipped_files`. Each malformed runs-directory file is named, with its reason, in
`malformed_run_files`.
A usage file is imported only for a run with no call lines. A call with no id gets the id
`legacy:<run>:<seq>`, seq being its 0-based position in its file, so a rerun inserts nothing new.

Ledger rows go in exactly as the driver writes them, so a row the driver already stored is
recognised by its hash and skipped. The source key `schema` therefore stays in row_json and the
schema_tag column stays NULL, as it does for live writes.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.store_migrate import open_store
from harness.store_write import (
    _KEYS,
    _RUN_KEYS,
    Row,
    Store,
    attempt_row,
    call_row,
    gate_rows,
    ledger_row,
    phase_row,
    run_row,
    split_phase_id,
)

TABLES = ("runs", "phases", "gate_decisions", "node_calls", "ledger", "attempts")
_COUNTS = ("seen", "inserted", "already_present", "malformed")
_USAGE_SUFFIX = ".usage.json"

Report = dict[str, dict[str, int] | int | list[str]]
STAMPED_KEY = "runs_stamped_ended"
STAMPED_STATUS = "backfilled"
SKIPPED_KEY = "skipped_files"
MALFORMED_KEY = "malformed_run_files"
NEVER_RECORDED_STATUS = "never_recorded"


class ArchiveRefused(RuntimeError):
    """The report does not balance or counts malformed records, or the move would clobber or lose a file. Nothing was moved."""


# ── pure parsers ─────────────────────────────────────────────────────────────


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _text(value: Any) -> str | None:
    """A non-empty string, or an ISO string for a datetime that a YAML loader turned a timestamp into."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value if isinstance(value, str) and value else None


def _record(doc: Any) -> dict[str, Any] | None:
    """The record if it has a run_id and a gate_diffs that is absent or a list of objects."""
    if not isinstance(doc, dict):
        return None
    diffs = doc.get("gate_diffs")
    good_diffs = diffs is None or (isinstance(diffs, list) and all(isinstance(d, dict) for d in diffs))
    return doc if isinstance(doc.get("run_id"), str) and doc["run_id"] and good_diffs else None


def malformed_reason(doc: Any) -> str:
    """Why a runs-directory document that is neither launch, run nor phase record was refused."""
    if doc is None:
        return "not readable JSON"
    if not isinstance(doc, dict):
        return "not a JSON object"
    if not (isinstance(doc.get("run_id"), str) and doc["run_id"]):
        return "no run_id"
    return "gate_diffs is not a list of objects" if _record(doc) is None else "unrecognised record"


def is_non_record(name: str) -> bool:
    """Ceilings, policies, chair, leader and dot-files: never a run record."""
    return (
        name.startswith(".")
        or name.endswith(".ceiling.json")
        or (name.startswith("policy.") and name.endswith(".json"))
        or name in ("chair.json", "leader.json")
    )


def is_launch(doc: Any) -> bool:
    return isinstance(doc, dict) and "launched_by" in doc and "run_id" not in doc


def parse_run(doc: Any, launch: dict[str, Any] | None = None) -> tuple[Row, list[Row]] | None:
    """The run row and its run-level gate rows (phase ''), or None for a malformed or phase record."""
    record = _record(doc)
    if record is None or ":" in record["run_id"]:
        return None
    return run_row(record, launch), gate_rows(record["run_id"], "", record.get("gate_diffs") or [])


def synth_run_record(run_id: str, phase_docs: Sequence[Any]) -> dict[str, Any]:
    """A run record for a run that wrote only phase records: its id plus the run keys of the earliest phase."""
    records = [r for r in (_record(d) for d in phase_docs) if r is not None]
    earliest = min(records, key=lambda r: (not r.get("ts"), str(r.get("ts")), r["run_id"]), default={})
    return {"run_id": run_id, **{k: earliest.get(k) for k in _RUN_KEYS if k in earliest}}


def parse_phase(doc: Any) -> tuple[Row, list[Row]] | None:
    """The phase row and its gate rows, or None for a malformed or run-level record."""
    record = _record(doc)
    if record is None or ":" not in record["run_id"]:
        return None
    run_id, phase_id = split_phase_id(record["run_id"])
    return phase_row(record), gate_rows(run_id, phase_id, record.get("gate_diffs") or [])


def _call(run_id: str, seq: int, call: Any) -> Row | None:
    """A call with no id gets `legacy:<run>:<seq>`, so the same file always yields the same ids."""
    if not isinstance(call, dict):
        return None
    return call_row({**call, "id": call.get("id") or f"legacy:{run_id}:{seq}"}, run_id=run_id, seq=seq)


def _calls(run_id: str, calls: Sequence[Any]) -> tuple[list[Row], int]:
    built = [_call(run_id, seq, call) for seq, call in enumerate(calls)]
    rows = [row for row in built if row is not None]
    return rows, len(built) - len(rows)


def parse_call_lines(run_id: str, lines: Sequence[str]) -> tuple[list[Row], int]:
    """Rows for the calls in a .calls.jsonl file, and the malformed count. seq counts non-blank lines from 0."""
    return _calls(run_id, [_json_object(line) for line in lines if line.strip()])


def parse_usage(run_id: str, doc: Any) -> tuple[list[Row], int]:
    """Rows for the calls list of a usage file, in the shape of call lines. No calls list is one malformed record."""
    calls = doc.get("calls") if isinstance(doc, dict) else None
    return _calls(run_id, calls) if isinstance(calls, list) else ([], 1)


def parse_ledger_line(line: str) -> Row | None:
    entry = _json_object(line)
    good = entry is not None and all(_text(entry.get(k)) for k in ("run_id", "ts", "kind"))
    return ledger_row(entry) if good and entry is not None else None


def _attempt(task_id: str, attempts: Sequence[Any], index: int) -> Row | None:
    entry = attempts[index]
    if not isinstance(entry, dict):
        return None
    run, phase, kind, ts = (_text(entry.get(k)) for k in ("run", "phase", "kind", "ts"))
    if not (run and phase and ts):
        return None
    kind = kind or "unknown"  # the older attempt format carries no kind
    # seq counts the earlier attempts of the same run, as the driver's _next_attempt_seq does.
    seq = sum(1 for e in attempts[:index] if isinstance(e, dict) and e.get("run") == entry["run"])
    return attempt_row(run, task_id, seq, phase, kind, entry.get("reason"), ts)


def parse_attempts(task_id: str, attempts: Any) -> tuple[list[Row], int]:
    """Attempt rows for a task's attempts list, and the malformed count. No list is no attempts."""
    if attempts is None:
        return [], 0
    if not isinstance(attempts, list):
        return [], 1
    built = [_attempt(task_id, attempts, i) for i in range(len(attempts))]
    rows = [row for row in built if row is not None]
    return rows, len(built) - len(rows)


# ── report ───────────────────────────────────────────────────────────────────


def new_report() -> Report:
    return {table: dict.fromkeys(_COUNTS, 0) for table in TABLES}


def balanced(report: Report) -> bool:
    """True when every table has seen minus malformed equal to inserted plus already present. An empty report is not.

    Only the per-table dicts are import counts. `runs_stamped_ended`, `skipped_files` and `malformed_run_files` are not.
    """
    tables = [t for t in report.values() if isinstance(t, dict)]
    return bool(tables) and all(t["seen"] - t["malformed"] == t["inserted"] + t["already_present"] for t in tables)


def _tally(report: Report, table: str, **counts: int) -> None:
    report[table] = {k: v + counts.get(k, 0) for k, v in report[table].items()}


# ── edge ─────────────────────────────────────────────────────────────────────


def _exists(store: Store, table: str, row: Row) -> bool:
    mark = store.conn.dialect.placeholder
    where = " AND ".join(f"{k} = {mark}" for k in _KEYS[table])
    return store.conn.query_one(f"SELECT 1 FROM {table} WHERE {where}", [row[k] for k in _KEYS[table]]) is not None


def _import(store: Store, report: Report, table: str, rows: Sequence[Row], malformed: int = 0) -> None:
    """Write each row once. Presence is asked of the store before the insert, so a silent skip shows as an imbalance."""
    with store.conn.transaction():
        for row in rows:
            present = _exists(store, table, row)
            inserted = store._insert(table, row)
            _tally(report, table, seen=1, inserted=inserted, already_present=int(present))
    _tally(report, table, seen=malformed, malformed=malformed)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _load(path: Path) -> Any:
    """The parsed JSON of a file, or None when it cannot be read or parsed."""
    text = _read(path)
    try:
        return None if text is None else json.loads(text)
    except ValueError:
        return None


def _cut(name: str, suffix: str) -> str:
    return name[: -len(suffix)]


def _launch_key(name: str) -> str:
    """The run id a launch file names: `<run>.launched.json`, `<run>.launch.json` or `<run>.json`."""
    suffix = next((s for s in (".launched.json", ".launch.json") if name.endswith(s)), ".json")
    return _cut(name, suffix)


def run_files(runs_dir: Path | str) -> list[Path]:
    """The importable files directly inside `runs_dir`: records, launches, call lines and usage files."""
    return sorted(p for p in Path(runs_dir).iterdir() if p.is_file() and p.name.endswith((".json", ".jsonl")))


def _label_never_recorded(store: Store, run_id: str) -> None:
    """Label a launch-only run imported by an earlier backfill with a null status."""
    mark = store.conn.dialect.placeholder
    sql = f"UPDATE runs SET status = {mark} WHERE run_id = {mark} AND status IS NULL AND ended_at IS NULL"
    store.conn.execute(sql, (NEVER_RECORDED_STATUS, run_id))


def _import_records(store: Store, report: Report, files: Sequence[Path]) -> list[str]:
    """Import run, phase and launch records. Returns `<file>: <reason>` for each malformed file."""
    docs = {p: _load(p) for p in files if p.name.endswith(".json") and not p.name.endswith(_USAGE_SUFFIX)}
    # A run that left call lines or a usage file recorded something, so it is not never_recorded.
    recorded = {_cut(p.name, s) for p in files for s in (".calls.jsonl", _USAGE_SUFFIX) if p.name.endswith(s)}
    malformed: list[str] = []
    # A launch file is a launch by its name: the oldest ones hold only {"at": ...}, with no launched_by.
    launch_paths = {p for p, d in docs.items() if p.name.endswith((".launched.json", ".launch.json")) or is_launch(d)}
    launches = {_launch_key(p.name): d for p, d in docs.items() if p in launch_paths}
    phases: dict[str, list[Any]] = {}
    for doc in docs.values():
        if parse_phase(doc) is not None:
            phases.setdefault(split_phase_id(doc["run_id"])[0], []).append(doc)
    used: set[str] = set()
    for path, doc in ((p, d) for p, d in docs.items() if p not in launch_paths):
        run = parse_run(doc, launches.get(doc.get("run_id") if isinstance(doc, dict) else None))
        phase = None if run is not None else parse_phase(doc)
        if run is not None:
            used.add(doc["run_id"])
            _import(store, report, "runs", [run[0]])
            _import(store, report, "gate_decisions", run[1])
        elif phase is not None:
            _import(store, report, "phases", [phase[0]])
            _import(store, report, "gate_decisions", phase[1])
        else:
            malformed.append(f"{path.name}: {malformed_reason(doc)}")
            _import(store, report, "runs", [], malformed=1)
    for run_id, launch in sorted((r, d) for r, d in launches.items() if r not in used):
        if phases.get(run_id) or run_id in recorded:
            _import(store, report, "runs", [run_row(synth_run_record(run_id, phases.get(run_id, [])), launch)])
        else:
            launch_only = {"launched_by": launch.get("launched_by"), "at": launch.get("at"), "graph_id": None}
            row = {**run_row({"run_id": run_id}, launch_only), "status": NEVER_RECORDED_STATUS}
            _import(store, report, "runs", [row])
            _label_never_recorded(store, run_id)
    return malformed


def _import_calls(store: Store, report: Report, files: Sequence[Path]) -> None:
    with_lines: set[str] = set()
    for path in (p for p in files if p.name.endswith(".calls.jsonl")):
        run_id = _cut(path.name, ".calls.jsonl")
        text = _read(path)
        rows, bad = parse_call_lines(run_id, [] if text is None else text.splitlines())
        if text is None:
            bad = 1
        if rows or bad:
            with_lines.add(run_id)
        _import(store, report, "node_calls", rows, malformed=bad)
    for path in (p for p in files if p.name.endswith(".usage.json")):
        run_id = _cut(path.name, ".usage.json")
        if run_id not in with_lines:
            rows, bad = parse_usage(run_id, _load(path))
            _import(store, report, "node_calls", rows, malformed=bad)


def _import_ledger(store: Store, report: Report, ledger_path: Path) -> None:
    lines = [line for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    parsed = [parse_ledger_line(line) for line in lines]
    rows = [row for row in parsed if row is not None]
    _import(store, report, "ledger", rows, malformed=len(parsed) - len(rows))


def _import_attempts(store: Store, report: Report, work_dir: Path) -> None:
    from core.workstore import WorkStoreError, read_item

    for path in sorted(p for p in work_dir.rglob("*.md") if p.name != "initiative.md"):
        try:
            item = read_item(path)
        except (WorkStoreError, OSError, ValueError):
            _import(store, report, "attempts", [], malformed=1)
            continue
        task_id = _text(item.get("id")) or path.stem
        rows, bad = parse_attempts(task_id, item.get("attempts"))
        _import(store, report, "attempts", rows, malformed=bad)


def _mtime_iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, UTC).isoformat()


def _ended_at(latest_ts: str | None, mtime: float) -> str:
    """The latest call ts when the run has calls, else the usage file's mtime as ISO UTC."""
    return latest_ts if latest_ts is not None else _mtime_iso(mtime)


def _stamp_ended(store: Store, runs_dir: Path) -> int:
    """Stamp every run with a null ended_at and a usage file. A run with no usage file may still be running: left null."""
    mark = store.conn.dialect.placeholder
    stamped = 0
    with store.conn.transaction():
        for (run_id,) in store.conn.query_all("SELECT run_id FROM runs WHERE ended_at IS NULL ORDER BY run_id"):
            usage = runs_dir / f"{run_id}{_USAGE_SUFFIX}"
            if usage.is_file():
                latest = store.conn.query_one(f"SELECT MAX(ts) FROM node_calls WHERE run_id = {mark}", [run_id])[0]
                stamped += store.finish_run(run_id, _ended_at(latest, usage.stat().st_mtime), STAMPED_STATUS)
    return stamped


def backfill(store: Store, runs_dir: Path | str, work_dir: Path | str, ledger_path: Path | str) -> Report:
    """Import everything under the three paths, then stamp ended the runs that have a usage file.

    A second run inserts nothing: every row counts as already present, and no run is stamped twice.
    """
    report = new_report()
    skipped = [p for p in run_files(runs_dir) if is_non_record(p.name)]
    files = [p for p in run_files(runs_dir) if not is_non_record(p.name)]
    malformed = _import_records(store, report, files)
    _import_calls(store, report, files)
    _import_ledger(store, report, Path(ledger_path))
    _import_attempts(store, report, Path(work_dir))
    stamped = _stamp_ended(store, Path(runs_dir))
    return {**report, STAMPED_KEY: stamped, SKIPPED_KEY: len(skipped), MALFORMED_KEY: malformed}


def archive_imported(files: Sequence[Path | str], archive_dir: Path | str, report: Report) -> list[Path]:
    """Move `files` into `archive_dir` when `report` balances and counts no malformed record. Otherwise refuse. Never deletes."""
    if not balanced(report):
        raise ArchiveRefused(f"report does not balance, nothing moved: {json.dumps(report, sort_keys=True)}")
    malformed = {t: c["malformed"] for t, c in report.items() if isinstance(c, dict) and c["malformed"]}
    if malformed:
        counted = ", ".join(f"{t}={n}" for t, n in malformed.items())
        raise ArchiveRefused(f"malformed records, nothing moved: {counted}")
    sources = [Path(f) for f in files]
    targets = [Path(archive_dir) / s.name for s in sources]
    problems = [
        *(f"missing source {s}" for s in sources if not s.is_file()),
        *(f"already archived {t}" for t in targets if t.exists()),
        *(f"two sources named {n}" for n in {s.name for s in sources} if [s.name for s in sources].count(n) > 1),
    ]
    if problems:
        raise ArchiveRefused("; ".join(problems))
    Path(archive_dir).mkdir(parents=True, exist_ok=True)
    for source, target in zip(sources, targets, strict=True):
        shutil.move(str(source), str(target))
    return targets


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.store_backfill", description=__doc__.split("\n\n")[0])
    parser.add_argument("runs_dir", help="directory of run, phase, launch, call and usage files")
    parser.add_argument("work_dir", help="directory of task markdown files")
    parser.add_argument("ledger", help="the ledger file, one JSON object per line")
    parser.add_argument("store_url", help="sqlite:///<absolute path> or postgresql://...")
    args = parser.parse_args(argv)
    conn = open_store(args.store_url, datetime.now(UTC).isoformat())
    try:
        report = backfill(Store(conn), args.runs_dir, args.work_dir, args.ledger)
    finally:
        conn.close()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if balanced(report) else 1


if __name__ == "__main__":
    sys.exit(main())
