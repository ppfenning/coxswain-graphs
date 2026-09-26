"""Give attempts whose cause is still `unknown` the cheap model's cause, bounded and recorded.

Run as `python -m harness.store_classify STORE_URL --provider-profile PATH [--limit 50] [--dry-run]`.

An attempt is pending when its cause is `unknown` and its cause_why is null or starts `model call failed` or
`classifier failed`: no model or person has decided it. Any other why is never touched. Oldest first is
`ts`, then the key, so the order is stable; rows with a null `ts` sort by the database's own rule.

Each pending row goes to `classify_with_model` with the evidence in its task record, when the store has one.
The UPDATE repeats the pending condition, so a rerun or a driver write that landed first is never overwritten.
`LimitStop` ends the loop, and the run is stamped `stopped`. `--dry-run` lists the rows and calls no model.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, NamedTuple

from harness.cause_model import cause_evidence, classify_with_model
from harness.runners import build_runner
from harness.store_dialect import json_load
from harness.store_migrate import open_store
from harness.store_write import Store
from runner.protocol import LimitStop

PRINCIPAL = "store_classify"
FAILED_WHYS = ("model call failed%", "classifier failed%")  # parameters: a literal `%` breaks the postgres driver
REASON_CHARS = 80


class Pending(NamedTuple):
    run_id: str
    task_id: str
    seq: int
    kind: str | None
    reason: str | None


def _pending_where(mark: str) -> str:
    return f"cause = {mark} AND (cause_why IS NULL OR cause_why LIKE {mark} OR cause_why LIKE {mark})"


def pending(store: Store, limit: int) -> list[Pending]:
    """Attempts no model or person has decided, oldest first, at most `limit`."""
    mark = store.conn.dialect.placeholder
    sql = (
        f"SELECT run_id, task_id, seq, kind, reason FROM attempts WHERE {_pending_where(mark)} "
        f"ORDER BY ts, run_id, task_id, seq LIMIT {mark}"
    )
    return [Pending(*row) for row in store.conn.query_all(sql, ("unknown", *FAILED_WHYS, limit))]


def _task_record(store: Store, run_id: str, task_id: str) -> Mapping[str, Any] | None:
    mark = store.conn.dialect.placeholder
    sql = f"SELECT record_json FROM task_records WHERE run_id = {mark} AND task_id = {mark} ORDER BY phase_id"
    rows = store.conn.query_all(sql, (run_id, task_id))
    record = json_load(rows[0][0]) if rows else None
    return record if isinstance(record, Mapping) else None


def _write(store: Store, row: Pending, cause: str, why: str) -> int:
    """Rows changed: 0 when the attempt stopped being pending since it was read."""
    mark = store.conn.dialect.placeholder
    sql = (
        f"UPDATE attempts SET cause = {mark}, cause_why = {mark} "
        f"WHERE run_id = {mark} AND task_id = {mark} AND seq = {mark} AND {_pending_where(mark)}"
    )
    with store.conn.transaction():
        return store.conn.execute(sql, (cause, why, row.run_id, row.task_id, row.seq, "unknown", *FAILED_WHYS))


def classify_pending(store: Store, runner: Any, limit: int) -> dict[str, Any]:
    """Classify up to `limit` pending attempts. Counts rows written per cause; `stopped` is "limit" or None."""
    rows = pending(store, limit)
    report: dict[str, Any] = {"considered": len(rows), "still_unknown": 0, "stopped": None}
    for row in rows:
        reasoning, claims = cause_evidence(row.reason or "", _task_record(store, row.run_id, row.task_id))
        try:
            cause, why = classify_with_model(runner, reasoning, claims, task=row.task_id)
        except LimitStop:
            return {**report, "stopped": "limit"}
        written = _write(store, row, cause, why)
        if cause == "unknown":
            report = {**report, "still_unknown": report["still_unknown"] + 1}
        elif written:
            key = f"classified_{cause}"
            report = {**report, key: report.get(key, 0) + 1}
    return report


def _dry_run_line(row: Pending) -> str:
    return f"{row.run_id}\t{row.task_id}\t{row.kind or ''}\t{(row.reason or '')[:REASON_CHARS]}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.store_classify", description=__doc__.split("\n\n")[0])
    parser.add_argument("store_url", help="sqlite:///<absolute path> or postgresql://...")
    parser.add_argument("--provider-profile", required=True, help="the provider profile the runner is built from")
    parser.add_argument("--limit", type=int, default=50, help="most attempts to classify in this run")
    parser.add_argument("--dry-run", action="store_true", help="list the pending attempts and call no model")
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    conn = open_store(args.store_url, now.isoformat())
    store = Store(conn)
    try:
        if args.dry_run:
            print("\n".join(_dry_run_line(r) for r in pending(store, args.limit)))
            return 0
        run_id = f"classify-causes-{now:%Y%m%d%H%M%S}"
        record = {
            "run_id": run_id,
            "principal": PRINCIPAL,
            "cartridge_sha": None,
            "cartridge_team": None,
            "overlay_sha": None,
            "provider_profile": str(args.provider_profile),
        }
        store.record_run(record, {"launched_by": PRINCIPAL, "at": now.isoformat(), "graph_id": None})
        runner = build_runner(
            scripted=None, provider_profile=args.provider_profile, role_skills={}, workdir=None, repo=None
        )
        if hasattr(runner, "run_id"):
            runner.run_id = run_id
        if hasattr(runner, "store"):
            runner.store = store
        status = "error"
        try:
            report = classify_pending(store, runner, args.limit)
            status = "stopped" if report["stopped"] else "ok"
        finally:
            store.finish_run(run_id, datetime.now(UTC).isoformat(), status)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
