"""python -m harness.store_cli: mark-landed, set-state and lease commands against the run-record store."""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.cli import _read_profile, _storage_url
from harness.store_cli_lease import lease_acquire, lease_release, lease_renew
from harness.store_dialect import Connection, StoreDriverMissing
from harness.store_landed import mark_landed
from harness.store_migrate import MigrationError, open_store
from harness.store_work_state import set_state

# Contract read by coxswain-tools. Exit 0 and exit 3 print exactly one JSON object on stdout.
# Exit 2 prints nothing on stdout. Help and every error go to stderr.
EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_PRECONDITION = 3
WORK_STATES = ("ready", "approved", "done", "dropped")

_DB_ERRORS: tuple[type[BaseException], ...] = (sqlite3.Error,)
try:  # the postgres driver is an optional extra
    import psycopg

    _DB_ERRORS = (*_DB_ERRORS, psycopg.Error)
except ImportError:  # pragma: no cover - depends on what is installed
    pass
# connect raises ValueError on an unknown scheme and OSError on an unreachable path.
# Those are caught around opening only, so a handler bug still raises with its traceback.
_OPEN_ERRORS = (*_DB_ERRORS, ValueError, OSError, StoreDriverMissing, MigrationError)


def resolve_store_url(flag: str | None, profile: Mapping[str, Any], runs_dir: Path | str) -> str:
    """The flag, else what `harness cli` resolves: the profile's storage_url, else cox.db in the runs dir."""
    return flag or _storage_url(profile, runs_dir)


def _positive_seconds(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, got {value}")
    return value


def _iso_time(text: str) -> str:
    """The text unchanged, once it parses as ISO 8601."""
    try:
        datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 time: {text!r}") from None
    return text


def _non_empty(text: str) -> str:
    if not text.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return text


def _common(parser: argparse.ArgumentParser, *, top: bool) -> None:
    """Leaf parsers default to SUPPRESS so a flag given before the subcommand survives."""

    def default(value: Any) -> Any:
        return value if top else argparse.SUPPRESS

    parser.add_argument("--store-url", default=default(None), help="database URL; required unless --runs-dir is given")
    parser.add_argument("--runs-dir", default=default(None), help="directory holding cox.db; required when --store-url is not given")
    parser.add_argument(
        "--provider-profile",
        default=default(None),
        help="optional; its storage_url is used when --store-url is not given, else cox.db in --runs-dir",
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m harness.store_cli", description=__doc__.splitlines()[0])
    _common(ap, top=True)
    commands = ap.add_subparsers(dest="command", required=True)

    landed = commands.add_parser("mark-landed", help="stamp the landed fields on a task record")
    _common(landed, top=False)
    for name in ("run_id", "phase", "task"):
        landed.add_argument(name)
    landed.add_argument("--pr", required=True, help="pull request URL")
    landed.add_argument("--at", required=True, type=_iso_time, help="landed time, ISO 8601")

    state = commands.add_parser("set-state", help="upsert one work item's state")
    _common(state, top=False)
    state.add_argument("initiative")
    state.add_argument("task")
    state.add_argument("state", choices=WORK_STATES)
    state.add_argument("--by", required=True, type=_non_empty, help="who made the change")
    state.add_argument("--phase", help="phase for a new row; ignored when the row exists")

    lease = commands.add_parser("lease", help="acquire, renew or release a named lease")
    actions = lease.add_subparsers(dest="action", required=True)

    acquire = actions.add_parser("acquire")
    _common(acquire, top=False)
    acquire.add_argument("name")
    acquire.add_argument("holder")
    acquire.add_argument("--ttl", type=_positive_seconds, required=True, help="seconds")

    renew = actions.add_parser("renew")
    _common(renew, top=False)
    renew.add_argument("name")
    renew.add_argument("holder")
    renew.add_argument("epoch", type=int)
    renew.add_argument("--ttl", type=_positive_seconds, required=True, help="seconds")

    release = actions.add_parser("release")
    _common(release, top=False)
    release.add_argument("name")
    release.add_argument("holder")
    release.add_argument("epoch", type=int)
    return ap


def dispatch(conn: Connection, args: argparse.Namespace, now: str) -> tuple[dict[str, Any], int]:
    """A missing task record is the empty object with exit 3."""
    if args.command == "mark-landed":
        record = mark_landed(conn, args.run_id, args.phase, args.task, args.pr, args.at)
        return ({}, EXIT_PRECONDITION) if record is None else (record, EXIT_OK)
    if args.command == "set-state":
        row = set_state(conn, args.initiative, args.task, args.state, args.by, args.phase, now)
        return ({}, EXIT_PRECONDITION) if row is None else (row, EXIT_OK)
    if args.action == "acquire":
        result = lease_acquire(conn, args.name, args.holder, now, args.ttl)
    elif args.action == "renew":
        result = lease_renew(conn, args.name, args.holder, args.epoch, now, args.ttl)
    else:
        result = lease_release(conn, args.name, args.holder, args.epoch)
    return result, EXIT_OK if result["ok"] else EXIT_PRECONDITION


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refusal(args: argparse.Namespace) -> str:
    if args.command == "mark-landed":
        return f"no task record for run {args.run_id} phase {args.phase} task {args.task}"
    if args.command == "set-state":
        return f"no work item {args.task} in initiative {args.initiative} and --phase was not given"
    return f"lease {args.name} refused for holder {args.holder}"


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_BAD_INPUT


def main(argv: Sequence[str] | None = None) -> int:
    try:
        with contextlib.redirect_stdout(sys.stderr):
            args = build_parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_BAD_INPUT
    if not args.store_url and args.runs_dir is None:
        return _fail("--store-url or --runs-dir is required")
    now = _now()
    profile = {} if args.provider_profile is None else _read_profile(args.provider_profile)
    url = resolve_store_url(args.store_url, profile, args.runs_dir)
    try:
        conn = open_store(url, now)
    except _OPEN_ERRORS as exc:
        return _fail(f"cannot open the store: {exc}")
    try:
        payload, code = dispatch(conn, args, now)
    except _DB_ERRORS as exc:
        return _fail(f"cannot read the store: {exc}")
    finally:
        conn.close()
    if code == EXIT_PRECONDITION:
        print(f"error: {_refusal(args)}", file=sys.stderr)
    print(json.dumps(payload, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
