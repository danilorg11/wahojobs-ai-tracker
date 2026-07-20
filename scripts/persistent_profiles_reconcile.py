"""Read-only operator CLI for dormant persistent-profile reconciliation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.config import DB_PATH  # noqa: E402
from wahojobs.persistent_profiles_reconciliation import (  # noqa: E402
    DEFAULT_MAX_FINDINGS,
    MAX_FINDINGS,
    REPORT_VERSION,
    PersistentProfileReconciliationError,
    reconcile_persistent_profiles,
)


_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_ERROR_REASONS = frozenset(
    {
        "schema_capability_unavailable",
        "temporary_contention",
        "internal_consistency_failure",
        "invalid_reconciliation_request",
    }
)
_FIXED_JSON_OUTPUT_FAILURE = (
    '{"error_code":"internal_consistency_failure",'
    '"report_version":"persistent_profile_reconciliation_v1",'
    '"status":"unavailable"}\n'
)
_FIXED_HUMAN_OUTPUT_FAILURE = "Persistent-profile reconciliation unavailable.\n"


class _CliArgumentError(Exception):
    __slots__ = ()


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise _CliArgumentError()


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int


def main(argv=None, *, _workspace_path=DB_PATH, _connect=sqlite3.connect) -> int:
    parser = _parser()
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    json_requested = any(
        type(argument) is str and argument == "--json"
        for argument in raw_arguments
    )
    try:
        args = parser.parse_args(raw_arguments)
    except _CliArgumentError:
        return _emit_error(json_requested, "invalid_reconciliation_request")
    except SystemExit as exc:
        return int(exc.code)
    except BaseException:
        return _emit_error(json_requested, "invalid_reconciliation_request")

    max_findings = _parse_max_findings(args.max_findings)
    if max_findings is None:
        return _emit_error(args.json, "invalid_reconciliation_request")

    try:
        path = Path(args.db).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return _emit_error(args.json, "invalid_reconciliation_request")
    if (
        _is_workspace_database_file(path, workspace_path=_workspace_path)
        and not args.allow_workspace_db
    ):
        return _emit_error(args.json, "invalid_reconciliation_request")
    if not path.is_file():
        return _emit_error(args.json, "invalid_reconciliation_request")
    initial_identity = _file_identity(path)
    uses_wal = _database_uses_wal(path)
    if initial_identity is None or uses_wal is None:
        return _emit_error(args.json, "invalid_reconciliation_request")
    if _existing_sidecars(path):
        return _emit_error(args.json, "temporary_contention")

    connection = None
    report = None
    reason = None
    try:
        connection = _connect(
            f"{path.as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=2.0,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        if (
            not _opened_database_matches(connection, path, initial_identity)
            or _existing_sidecars(path)
        ):
            reason = "temporary_contention"
        elif not uses_wal and not _rollback_journal_is_unlocked(
            path,
            initial_identity,
            _connect,
        ):
            reason = "temporary_contention"
        elif (
            not args.allow_workspace_db
            and _is_workspace_database_file(
                _opened_database_path(connection), workspace_path=_workspace_path
            )
        ):
            reason = "invalid_reconciliation_request"
        else:
            report = reconcile_persistent_profiles(
                connection,
                max_findings=max_findings,
                summary_only=args.summary_only,
            )
            if (
                not _file_identity_matches(path, initial_identity)
                or _existing_sidecars(path)
            ):
                reason = "temporary_contention"
                report = None
    except (KeyboardInterrupt, SystemExit):
        reason = "internal_consistency_failure"
    except PersistentProfileReconciliationError as exc:
        reason = exc.reason_code
        exc = None
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", None)
        reason = (
            "temporary_contention"
            if type(code) is int
            and (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            else "internal_consistency_failure"
        )
        exc = None
    except Exception:
        reason = "internal_consistency_failure"
    finally:
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                reason = "internal_consistency_failure"
                report = None

    if (
        not _file_identity_matches(path, initial_identity)
        or _existing_sidecars(path)
    ):
        reason = "temporary_contention"
        report = None

    if reason is not None:
        return _emit_error(args.json, reason)
    if report is None:
        return _emit_error(args.json, "internal_consistency_failure")
    try:
        output = report.to_json() + "\n" if args.json else _render_human(report)
    except BaseException:
        return _emit_error(args.json, "internal_consistency_failure")
    if not _write_stdout(output):
        _write_output_failure_fallback(args.json)
        return 2
    if report.status == "clean":
        return 0
    if report.status == "findings":
        return 1
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Read-only persistent-profile row reconciliation."
    )
    parser.add_argument("--db", required=True, help="SQLite database to inspect.")
    parser.add_argument("--json", action="store_true", help="Emit compact JSON.")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Calculate all counts without displaying individual findings.",
    )
    parser.add_argument(
        "--max-findings",
        default=str(DEFAULT_MAX_FINDINGS),
        help=f"Maximum displayed findings (0-{MAX_FINDINGS}).",
    )
    parser.add_argument(
        "--allow-workspace-db",
        action="store_true",
        help="Allow an explicitly reviewed read-only scan of the workspace database.",
    )
    return parser


def _parse_max_findings(value) -> int | None:
    if type(value) is not str or not value.isascii() or not value.isdigit():
        return None
    parsed = int(value)
    return parsed if 0 <= parsed <= MAX_FINDINGS else None


def _emit_error(json_mode: bool, reason: str) -> int:
    if reason not in _ERROR_REASONS:
        reason = "internal_consistency_failure"
    if json_mode:
        output = (
            '{"error":"persistent_profile_reconciliation_error",'
            f'"reason_code":"{reason}",'
            f'"report_version":"{REPORT_VERSION}",'
            '"status":"unavailable"}\n'
        )
    else:
        output = (
            "Persistent-profile reconciliation unavailable.\n"
            f"Reason: {reason}\n"
        )
    if not _write_stdout(output):
        _write_output_failure_fallback(json_mode)
    return 2


def _render_human(report) -> str:
    lines = [
        "Persistent Profile Reconciliation",
        "=================================",
        f"Status: {report.status}",
    ]
    for key, value in report.inventory:
        lines.append(f"{key}: {value}")
    lines.append(f"total_findings: {report.total_findings}")
    for code, count in report.finding_counts_by_code:
        lines.append(f"finding.{code}: {count}")
    for finding in report.findings:
        locator = []
        if finding.profile_ordinal is not None:
            locator.append(f"profile={finding.profile_ordinal}")
        if finding.revision_number is not None:
            locator.append(f"revision={finding.revision_number}")
        if finding.source_ordinal is not None:
            locator.append(f"source={finding.source_ordinal}")
        if finding.orphan_ordinal is not None:
            locator.append(f"orphan={finding.orphan_ordinal}")
        suffix = " " + " ".join(locator) if locator else ""
        lines.append(f"{finding.severity}: {finding.code}{suffix}")
    lines.append(
        f"findings_truncated: {str(report.findings_truncated).lower()}"
    )
    return "\n".join(lines) + "\n"


def _write_stdout(output: str) -> bool:
    try:
        written = sys.stdout.write(output)
        if type(written) is int and written != len(output):
            return False
        flush = getattr(sys.stdout, "flush", None)
        if flush is not None:
            flush()
        return True
    except BaseException:
        return False


def _write_output_failure_fallback(json_mode: bool) -> None:
    output = (
        _FIXED_JSON_OUTPUT_FAILURE
        if json_mode
        else _FIXED_HUMAN_OUTPUT_FAILURE
    )
    _write_stdout(output)


def _is_workspace_database_file(candidate, *, workspace_path=DB_PATH) -> bool:
    if candidate is None:
        return False
    candidate = Path(candidate)
    workspace = Path(workspace_path)
    try:
        candidate_resolved = candidate.resolve()
        workspace_resolved = workspace.resolve()
    except (OSError, RuntimeError):
        return False
    if os.path.normcase(str(candidate_resolved)) == os.path.normcase(
        str(workspace_resolved)
    ):
        return True
    if not candidate.exists() or not workspace.exists():
        return False
    try:
        return os.path.samefile(candidate, workspace)
    except (OSError, ValueError):
        try:
            candidate_stat = candidate.stat()
            workspace_stat = workspace.stat()
        except OSError:
            return False
        return (
            candidate_stat.st_ino != 0
            and (candidate_stat.st_dev, candidate_stat.st_ino)
            == (workspace_stat.st_dev, workspace_stat.st_ino)
        )


def _file_identity(path: Path) -> _FileIdentity | None:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return _FileIdentity(
        device=stat_result.st_dev,
        inode=stat_result.st_ino,
        size=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
    )


def _file_identity_matches(path: Path, expected: _FileIdentity) -> bool:
    return _file_identity(path) == expected


def _existing_sidecars(path: Path) -> tuple[Path, ...]:
    result = []
    for suffix in _SIDECAR_SUFFIXES:
        candidate = Path(str(path) + suffix)
        try:
            if candidate.exists():
                result.append(candidate)
        except OSError:
            result.append(candidate)
    return tuple(result)


def _database_uses_wal(path: Path) -> bool | None:
    try:
        with path.open("rb") as database_file:
            header = database_file.read(20)
    except OSError:
        return None
    if len(header) != 20 or header[:16] != b"SQLite format 3\x00":
        return None
    if header[18:20] == b"\x02\x02":
        return True
    if header[18:20] == b"\x01\x01":
        return False
    return None


def _opened_database_matches(
    connection, requested_path: Path, expected: _FileIdentity
) -> bool:
    opened_path = _opened_database_path(connection)
    if opened_path is None:
        return False
    try:
        opened_path = opened_path.resolve(strict=True)
        requested_path = requested_path.resolve(strict=True)
        same_file = os.path.samefile(opened_path, requested_path)
    except (OSError, RuntimeError, ValueError):
        return False
    return (
        same_file
        and _file_identity(opened_path) == expected
        and _file_identity(requested_path) == expected
    )


def _opened_database_path(connection) -> Path | None:
    for row in connection.execute("PRAGMA database_list"):
        if row[1] == "main":
            return Path(row[2])
    return None


def _rollback_journal_is_unlocked(
    path: Path,
    expected: _FileIdentity,
    connect,
) -> bool:
    """Probe rollback-journal locks without using this path for WAL databases."""
    probe = None
    unlocked = False
    try:
        probe = connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=0.0,
        )
        probe.execute("PRAGMA query_only = ON")
        probe.execute("BEGIN")
        probe.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()
        unlocked = _opened_database_matches(probe, path, expected)
    except BaseException:
        unlocked = False
    finally:
        if probe is not None:
            try:
                if probe.in_transaction:
                    probe.rollback()
            except BaseException:
                unlocked = False
            try:
                probe.close()
            except BaseException:
                unlocked = False
    return unlocked and not _existing_sidecars(path)


if __name__ == "__main__":
    raise SystemExit(main())
