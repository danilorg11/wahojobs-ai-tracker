"""Read-only operator CLI for durable Google OIDC transaction reconciliation."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.google_oidc_authorization_transactions_migration as database_boundary  # noqa: E402
from wahojobs.config import DB_PATH  # noqa: E402
from wahojobs.google_oidc_authorization_transaction_reconciliation import (  # noqa: E402
    DEFAULT_MAX_FINDINGS,
    MAX_FINDINGS,
    MAX_KEY_VERSION,
    REPORT_VERSION,
    GoogleOidcAuthorizationTransactionReconciliationError,
    reconcile_google_oidc_authorization_transactions,
)


_ERROR_REASONS = frozenset(
    {
        "invalid_reconciliation_request",
        "schema_capability_unavailable",
        "temporary_contention",
        "inspection_boundary_unavailable",
        "internal_consistency_failure",
    }
)
class _CliArgumentError(Exception):
    __slots__ = ()


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise _CliArgumentError()


def main(argv=None, *, _workspace_path=DB_PATH, _connect=sqlite3.connect) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in raw
    try:
        args = _parser().parse_args(raw)
    except _CliArgumentError:
        return _emit_error(json_requested, "invalid_reconciliation_request")
    except SystemExit as exc:
        return int(exc.code)
    except BaseException:
        return _emit_error(json_requested, "invalid_reconciliation_request")

    maximum = _parse_decimal(args.max_findings, maximum=MAX_FINDINGS, zero=True)
    lookup_versions = _parse_versions(args.lookup_key_version)
    protection_versions = _parse_versions(args.protection_key_version)
    if (
        maximum is None
        or lookup_versions is None
        or protection_versions is None
    ):
        return _emit_error(args.json, "invalid_reconciliation_request")

    try:
        path = database_boundary.canonical_database_path(args.db)
    except database_boundary.GoogleOidcAuthorizationTransactionsMigrationError:
        return _emit_error(args.json, "invalid_reconciliation_request")
    if (
        not path.is_file()
        or (
            _is_workspace_database_file(path, workspace_path=_workspace_path)
            and not args.allow_workspace_db
        )
    ):
        return _emit_error(args.json, "invalid_reconciliation_request")

    identity = _file_identity(path)
    wal_mode = _database_uses_wal(path)
    if identity is None or wal_mode is None:
        return _emit_error(args.json, "invalid_reconciliation_request")
    if _existing_sidecars(path):
        return _emit_error(args.json, "temporary_contention")

    connection = None
    report = None
    reason = None
    try:
        connection = database_boundary.open_canonical_sqlite_database(
            path,
            read_only=True,
            expected_identity=identity,
            connect=_connect,
            timeout=2.0,
            immutable=True,
        )
        if (
            not _opened_database_matches(connection, path, identity)
            or _existing_sidecars(path)
        ):
            reason = "temporary_contention"
        elif not wal_mode and not _rollback_journal_is_unlocked(
            path, identity, _connect
        ):
            reason = "temporary_contention"
        elif (
            not args.allow_workspace_db
            and _is_workspace_database_file(
                _opened_database_path(connection),
                workspace_path=_workspace_path,
            )
        ):
            reason = "invalid_reconciliation_request"
        else:
            report = reconcile_google_oidc_authorization_transactions(
                connection,
                accepted_lookup_key_versions=lookup_versions,
                accepted_protection_key_versions=protection_versions,
                max_findings=maximum,
                summary_only=args.summary_only,
                source_guarantees_no_sidecar_creation=True,
            )
    except GoogleOidcAuthorizationTransactionReconciliationError as exc:
        reason = exc.reason_code
    except database_boundary.GoogleOidcAuthorizationTransactionsMigrationError:
        reason = "temporary_contention"
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", None)
        reason = (
            "temporary_contention"
            if type(code) is int
            and (code & 0xFF)
            in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            else "internal_consistency_failure"
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        reason = "internal_consistency_failure"
    except Exception:
        reason = "internal_consistency_failure"
    finally:
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                report = None
                reason = "internal_consistency_failure"

    if not _file_identity_matches(path, identity) or _existing_sidecars(path):
        report = None
        reason = "temporary_contention"
    if reason is not None:
        return _emit_error(args.json, reason)
    if report is None:
        return _emit_error(args.json, "internal_consistency_failure")

    try:
        output = (
            report.to_json_bytes()
            if args.json
            else report.to_human_bytes()
        )
    except BaseException:
        return _emit_error(args.json, "internal_consistency_failure")
    if not _write_stdout(output):
        return 2
    if report.status == "clean" and report.complete and not report.blocking:
        return 0
    if report.status == "findings" and report.complete:
        return 1
    return 2


def _parser():
    parser = _SafeArgumentParser(
        description=(
            "Read-only durable Google OIDC authorization-transaction "
            "reconciliation."
        )
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--max-findings", default=str(DEFAULT_MAX_FINDINGS))
    parser.add_argument(
        "--lookup-key-version",
        action="append",
        help="Accepted lookup-key version; repeat at most three times.",
    )
    parser.add_argument(
        "--protection-key-version",
        action="append",
        help="Accepted protection-key version; repeat at most three times.",
    )
    parser.add_argument("--allow-workspace-db", action="store_true")
    return parser


def _parse_decimal(value, *, maximum, zero=False):
    if (
        type(value) is not str
        or not value.isascii()
        or not value.isdigit()
    ):
        return None
    parsed = int(value)
    minimum = 0 if zero else 1
    return parsed if minimum <= parsed <= maximum else None


def _parse_versions(values):
    if values is None:
        return (1,)
    if type(values) is not list or not 1 <= len(values) <= 3:
        return None
    parsed = tuple(
        _parse_decimal(value, maximum=MAX_KEY_VERSION)
        for value in values
    )
    if any(value is None for value in parsed) or len(set(parsed)) != len(parsed):
        return None
    return tuple(sorted(parsed))


def _emit_error(json_mode, reason):
    if reason not in _ERROR_REASONS:
        reason = "internal_consistency_failure"
    if json_mode:
        output = (
            '{"error":"google_oidc_authorization_transaction_reconciliation_error",'
            f'"reason_code":"{reason}",'
            f'"report_version":"{REPORT_VERSION}",'
            '"status":"unavailable"}\n'
        ).encode("ascii")
    else:
        output = (
            "Google OIDC authorization-transaction reconciliation unavailable.\n"
            f"Reason: {reason}\n"
        ).encode("ascii")
    _write_stdout(output)
    return 2


def _write_stdout(output):
    if type(output) is not bytes:
        return False
    try:
        stream = getattr(sys.stdout, "buffer", None)
        if stream is not None:
            written = stream.write(output)
            flush = getattr(stream, "flush", None)
            if type(written) is int and written != len(output):
                return False
        else:
            text = output.decode("utf-8")
            written = sys.stdout.write(text)
            flush = getattr(sys.stdout, "flush", None)
            if type(written) is int and written != len(text):
                return False
        if flush is not None:
            flush()
        return True
    except BaseException:
        return False


def _is_workspace_database_file(candidate, *, workspace_path=DB_PATH):
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


def _file_identity(path):
    return database_boundary.database_file_identity(path)


def _file_identity_matches(path, expected):
    return _file_identity(path) == expected


def _existing_sidecars(path):
    return database_boundary.existing_sqlite_sidecars(path)


def _database_uses_wal(path):
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
    except OSError:
        return None
    if len(header) != 20 or header[:16] != b"SQLite format 3\x00":
        return None
    if header[18:20] == b"\x02\x02":
        return True
    if header[18:20] == b"\x01\x01":
        return False
    return None


def _opened_database_matches(connection, requested_path, expected):
    return database_boundary.opened_database_matches(
        connection,
        requested_path,
        expected,
    )


def _opened_database_path(connection):
    return database_boundary.opened_database_path(connection)


def _rollback_journal_is_unlocked(path, expected, connect):
    probe = None
    okay = False
    try:
        probe = database_boundary.open_canonical_sqlite_database(
            path,
            read_only=True,
            expected_identity=expected,
            connect=connect,
            timeout=0.0,
        )
        probe.execute("BEGIN")
        probe.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
        okay = _opened_database_matches(probe, path, expected)
    except BaseException:
        okay = False
    finally:
        if probe is not None:
            try:
                if probe.in_transaction:
                    probe.rollback()
            except BaseException:
                okay = False
            try:
                probe.close()
            except BaseException:
                okay = False
    return okay and not _existing_sidecars(path)


if __name__ == "__main__":
    raise SystemExit(main())
