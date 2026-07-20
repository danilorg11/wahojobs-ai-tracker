"""Dormant, read-only reconciliation for persistent product profiles.

The caller owns the SQLite connection.  This module never opens a database,
installs schema, repairs state, or integrates with normal product runtime.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import re
import sqlite3
from types import MappingProxyType
from typing import Iterable

from wahojobs.persistent_profile_canonical_v2_schema import (
    attest_persistent_profile_canonical_v2_schema,
)
from wahojobs.persistent_profiles import (
    LIFECYCLE_SOURCE_SCHEMA_VERSION,
    MIGRATION_005_CAPABILITIES,
    REVISION_KINDS,
    SOURCE_BUNDLE_HASH_VERSION,
    SOURCE_TYPES,
    PersistentProfileDomainError,
    PersistentProfileSchemaCapabilities,
    _IDEMPOTENCY_KEY_PATTERN,
    _canonical_json_bytes,
    _validate_source_content,
    _validate_version,
    canonical_utc_timestamp,
    validate_profile_id,
    validate_revision_id,
    validate_source_id,
)
from wahojobs.profiles.canonical_v2 import (
    CanonicalProfileV2Error,
    SCHEMA_VERSION as CANONICAL_PROFILE_V2,
    canonical_profile_v2_json_bytes,
    parse_canonical_profile_v2_json,
)


REPORT_VERSION = "persistent_profile_reconciliation_v1"
DEFAULT_MAX_FINDINGS = 1_000
MAX_FINDINGS = 10_000
MAX_REPORT_BYTES = 1_048_576

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")

ERROR_REASON_CODES = frozenset(
    {
        "schema_capability_unavailable",
        "temporary_contention",
        "internal_consistency_failure",
        "invalid_reconciliation_request",
    }
)

ROW_REACHABLE = "row_reachable"
SCHEMA_UNREACHABLE = "schema_unreachable_under_exact_m005"
PREREQUISITE_ONLY = "prerequisite_only"
REACHABILITY_CLASSES = frozenset(
    {ROW_REACHABLE, SCHEMA_UNREACHABLE, PREREQUISITE_ONLY}
)
ENTITY_KINDS = frozenset(
    {"database", "profile", "revision", "source", "current_view"}
)
LOCATOR_FIELDS = frozenset(
    {"profile_ordinal", "revision_number", "source_ordinal", "orphan_ordinal"}
)
MAX_LOCATOR_VALUE = 9_223_372_036_854_775_807


@dataclass(frozen=True, slots=True)
class PersistentProfileFindingSpec:
    code: str
    severity: str
    entity_kinds: frozenset[str]
    allowed_locators: frozenset[str]
    required_any_locators: frozenset[str]
    meaning: str
    reachability: str
    reachability_reason: str | None = None


def _finding_specs(
    codes,
    *,
    entity_kinds,
    allowed_locators,
    required_any_locators,
    reachability=ROW_REACHABLE,
    reachability_reason=None,
):
    return tuple(
        PersistentProfileFindingSpec(
            code=code,
            severity="error",
            entity_kinds=frozenset(entity_kinds),
            allowed_locators=frozenset(allowed_locators),
            required_any_locators=frozenset(required_any_locators),
            meaning=code.replace("_", " "),
            reachability=reachability,
            reachability_reason=reachability_reason,
        )
        for code in codes
    )


_PROFILE_LOCATORS = {"profile_ordinal"}
_REVISION_LOCATORS = {"profile_ordinal", "revision_number", "orphan_ordinal"}
_SOURCE_LOCATORS = {
    "profile_ordinal",
    "revision_number",
    "source_ordinal",
    "orphan_ordinal",
}
_EXACT_UNIQUENESS_REASON = (
    "The exact M005 uniqueness contract rejects the duplicate before an "
    "attested row scan can begin."
)
_EXACT_VIEW_REASON = (
    "The exact M005 current view derives this value directly from uniquely "
    "numbered durable revisions; changing that behavior is schema drift."
)

FINDING_TAXONOMY = (
    *_finding_specs(
        ("foreign_key_violation",),
        entity_kinds={"database"},
        allowed_locators={"orphan_ordinal"},
        required_any_locators={"orphan_ordinal"},
    ),
    *_finding_specs(
        ("row_read_failure",),
        entity_kinds={"profile", "revision"},
        allowed_locators=_REVISION_LOCATORS,
        required_any_locators={"profile_ordinal", "orphan_ordinal"},
    ),
    *_finding_specs(
        (
            "invalid_profile_id",
            "missing_principal_relationship",
            "profile_environment_mismatch",
            "missing_current_revision",
            "invalid_profile_timestamp",
            "missing_revision_history",
            "missing_current_view_row",
        ),
        entity_kinds={"profile"},
        allowed_locators=_PROFILE_LOCATORS,
        required_any_locators=_PROFILE_LOCATORS,
    ),
    *_finding_specs(
        ("duplicate_principal_profile",),
        entity_kinds={"profile"},
        allowed_locators=_PROFILE_LOCATORS,
        required_any_locators=_PROFILE_LOCATORS,
        reachability=SCHEMA_UNREACHABLE,
        reachability_reason=_EXACT_UNIQUENESS_REASON,
    ),
    *_finding_specs(
        (
            "foreign_current_revision",
            "stale_current_revision",
            "profile_lifecycle_mismatch",
            "current_view_mismatch",
        ),
        entity_kinds={"profile"},
        allowed_locators=_PROFILE_LOCATORS,
        required_any_locators=_PROFILE_LOCATORS,
        reachability=SCHEMA_UNREACHABLE,
        reachability_reason=_EXACT_VIEW_REASON,
    ),
    *_finding_specs(
        (
            "orphan_revision",
            "revision_relationship_mismatch",
            "invalid_revision_id",
            "revision_number_gap",
            "invalid_revision_chain",
            "unexpected_initial_revision",
            "unsupported_revision_kind",
            "invalid_lifecycle_transition",
            "revision_after_deletion_request",
            "invalid_correction_target",
            "invalid_revision_timestamp",
            "malformed_structured_profile",
            "invalid_canonical_profile_v2",
            "structured_profile_identity_mismatch",
            "noncanonical_structured_profile",
            "malformed_structured_hash",
            "structured_hash_mismatch",
            "canonical_schema_version_mismatch",
            "malformed_idempotency_key",
            "malformed_request_fingerprint",
        ),
        entity_kinds={"revision"},
        allowed_locators=_REVISION_LOCATORS,
        required_any_locators={"profile_ordinal", "orphan_ordinal"},
    ),
    *_finding_specs(
        ("invalid_initial_revision",),
        entity_kinds={"profile", "revision"},
        allowed_locators=_REVISION_LOCATORS,
        required_any_locators={"profile_ordinal", "orphan_ordinal"},
    ),
    *_finding_specs(
        ("duplicate_revision_number",),
        entity_kinds={"revision"},
        allowed_locators=_REVISION_LOCATORS,
        required_any_locators={"profile_ordinal", "orphan_ordinal"},
        reachability=SCHEMA_UNREACHABLE,
        reachability_reason=_EXACT_UNIQUENESS_REASON,
    ),
    *_finding_specs(
        (
            "invalid_source_id",
            "orphan_source",
            "source_relationship_mismatch",
            "source_ordinal_gap",
            "unsupported_source_type",
            "malformed_source_payload",
            "invalid_source_timestamp",
            "malformed_source_hash",
            "source_hash_mismatch",
        ),
        entity_kinds={"source"},
        allowed_locators=_SOURCE_LOCATORS,
        required_any_locators={"profile_ordinal", "orphan_ordinal"},
    ),
    *_finding_specs(
        ("invalid_source_for_revision_kind",),
        entity_kinds={"revision", "source"},
        allowed_locators=_SOURCE_LOCATORS,
        required_any_locators={"profile_ordinal", "orphan_ordinal"},
    ),
    *_finding_specs(
        ("source_bundle_hash_mismatch", "source_count_mismatch"),
        entity_kinds={"revision"},
        allowed_locators=_REVISION_LOCATORS,
        required_any_locators={"profile_ordinal", "orphan_ordinal"},
    ),
    *_finding_specs(
        ("duplicate_source_ordinal",),
        entity_kinds={"source"},
        allowed_locators=_SOURCE_LOCATORS,
        required_any_locators={"profile_ordinal", "orphan_ordinal"},
        reachability=SCHEMA_UNREACHABLE,
        reachability_reason=_EXACT_UNIQUENESS_REASON,
    ),
    *_finding_specs(
        ("idempotency_scope_conflict",),
        entity_kinds={"revision"},
        allowed_locators=_REVISION_LOCATORS,
        required_any_locators={"profile_ordinal", "orphan_ordinal"},
        reachability=SCHEMA_UNREACHABLE,
        reachability_reason=_EXACT_UNIQUENESS_REASON,
    ),
    *_finding_specs(
        ("unexpected_current_view_row", "duplicate_current_view_row"),
        entity_kinds={"current_view", "profile"},
        allowed_locators={"profile_ordinal", "orphan_ordinal"},
        required_any_locators={"profile_ordinal", "orphan_ordinal"},
        reachability=SCHEMA_UNREACHABLE,
        reachability_reason=_EXACT_VIEW_REASON,
    ),
)

FINDING_SPEC_BY_CODE = MappingProxyType(
    {spec.code: spec for spec in FINDING_TAXONOMY}
)
FINDING_CODES = frozenset(FINDING_SPEC_BY_CODE)

_BUSY_CODES = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
_LIFECYCLE_STATES = frozenset({"active", "archived", "deletion_requested"})
_ALL_REVISION_KINDS = frozenset({"initial", *REVISION_KINDS})
_LIFECYCLE_KINDS = frozenset({"archive", "reactivate", "deletion_request"})


class PersistentProfileReconciliationError(Exception):
    """Bounded, stable reconciliation invocation failure."""

    __slots__ = ("reason_code",)

    def __init__(self, reason_code: str):
        if reason_code not in ERROR_REASON_CODES:
            reason_code = "internal_consistency_failure"
        self.reason_code = reason_code
        super().__init__("Persistent-profile reconciliation could not be completed.")

    def public_dict(self) -> dict:
        return {
            "error": "persistent_profile_reconciliation_error",
            "reason_code": self.reason_code,
        }

    def __repr__(self) -> str:
        return (
            "PersistentProfileReconciliationError("
            f"reason_code={self.reason_code!r})"
        )


@dataclass(frozen=True, slots=True)
class PersistentProfileReconciliationFinding:
    code: str
    entity_kind: str
    profile_ordinal: int | None = None
    revision_number: int | None = None
    source_ordinal: int | None = None
    orphan_ordinal: int | None = None
    severity: str = field(init=False)

    def __post_init__(self):
        spec = FINDING_SPEC_BY_CODE.get(self.code)
        if spec is None or self.entity_kind not in spec.entity_kinds:
            raise ValueError("invalid reconciliation finding")
        object.__setattr__(self, "severity", spec.severity)
        provided = set()
        for name in LOCATOR_FIELDS:
            value = getattr(self, name)
            if value is None:
                continue
            provided.add(name)
            if (
                type(value) is not int
                or value < 1
                or value > MAX_LOCATOR_VALUE
            ):
                raise ValueError("invalid reconciliation finding")
        if (
            not provided <= spec.allowed_locators
            or not provided & spec.required_any_locators
        ):
            raise ValueError("invalid reconciliation finding")

    def to_dict(self) -> dict:
        result = {
            "code": self.code,
            "entity_kind": self.entity_kind,
            "severity": self.severity,
        }
        for key in (
            "profile_ordinal",
            "revision_number",
            "source_ordinal",
            "orphan_ordinal",
        ):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result


@dataclass(frozen=True, slots=True)
class PersistentProfileReconciliationReport:
    report_version: str
    status: str
    complete: bool
    findings_truncated: bool
    total_findings: int
    finding_counts_by_code: tuple[tuple[str, int], ...]
    inventory: tuple[tuple[str, int | None], ...]
    lifecycle_counts: tuple[tuple[str, int], ...]
    revision_kind_counts: tuple[tuple[str, int], ...]
    source_type_counts: tuple[tuple[str, int], ...]
    foreign_key_violation_count: int | None
    findings: tuple[PersistentProfileReconciliationFinding, ...]
    unavailable_reason: str | None = None

    def to_dict(self) -> dict:
        result = {
            "complete": self.complete,
            "finding_counts_by_code": dict(self.finding_counts_by_code),
            "findings": [item.to_dict() for item in self.findings],
            "findings_truncated": self.findings_truncated,
            "foreign_key_violation_count": self.foreign_key_violation_count,
            "inventory": dict(self.inventory),
            "lifecycle_counts": dict(self.lifecycle_counts),
            "report_version": self.report_version,
            "revision_kind_counts": dict(self.revision_kind_counts),
            "source_type_counts": dict(self.source_type_counts),
            "status": self.status,
            "total_findings": self.total_findings,
        }
        if self.unavailable_reason is not None:
            result["unavailable_reason"] = self.unavailable_reason
        return result

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    def to_json(self) -> str:
        return self.to_json_bytes().decode("utf-8")


class _FindingCollector:
    def __init__(self, max_findings: int):
        self.max_findings = max_findings
        self.total = 0
        self.counts: Counter[str] = Counter()
        self.displayed: list[PersistentProfileReconciliationFinding] = []

    def add(
        self,
        code: str,
        entity_kind: str,
        *,
        profile_ordinal: int | None = None,
        revision_number: int | None = None,
        source_ordinal: int | None = None,
        orphan_ordinal: int | None = None,
    ) -> None:
        finding = PersistentProfileReconciliationFinding(
            code=code,
            entity_kind=entity_kind,
            profile_ordinal=profile_ordinal,
            revision_number=revision_number,
            source_ordinal=source_ordinal,
            orphan_ordinal=orphan_ordinal,
        )
        self.total += 1
        self.counts[code] += 1
        if len(self.displayed) < self.max_findings:
            self.displayed.append(finding)


def reconcile_persistent_profiles(
    connection,
    *,
    max_findings: int = DEFAULT_MAX_FINDINGS,
    summary_only: bool = False,
    _capabilities: PersistentProfileSchemaCapabilities = MIGRATION_005_CAPABILITIES,
) -> PersistentProfileReconciliationReport:
    """Reconcile all durable profile rows inside one read snapshot."""
    if (
        not isinstance(connection, sqlite3.Connection)
        or type(max_findings) is not int
        or not 0 <= max_findings <= MAX_FINDINGS
        or type(summary_only) is not bool
    ):
        raise PersistentProfileReconciliationError("invalid_reconciliation_request")

    displayed_limit = 0 if summary_only else max_findings
    owned_transaction = False
    report = None
    failure_reason = None
    cleanup_succeeded = True
    cleanup_had_failure = False
    try:
        caller_had_transaction = connection.in_transaction
        if not caller_had_transaction:
            try:
                connection.execute("BEGIN")
            finally:
                owned_transaction = connection.in_transaction
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            report = _unavailable_report("schema_capability_unavailable")
        elif (
            type(_capabilities) is not PersistentProfileSchemaCapabilities
            or _capabilities != MIGRATION_005_CAPABILITIES
            or _capabilities.source_types != SOURCE_TYPES
            or CANONICAL_PROFILE_V2 not in _capabilities.canonical_versions
            or LIFECYCLE_SOURCE_SCHEMA_VERSION
            not in _capabilities.lifecycle_source_schema_versions
        ):
            report = _unavailable_report("schema_capability_unavailable")
        else:
            attestation = attest_persistent_profile_canonical_v2_schema(connection)
            if (
                attestation.get("state") != "correctly_installed"
                or not attestation.get("migration_marker_present")
                or attestation.get("migration_version")
                != MIGRATION_005_CAPABILITIES.migration_version
            ):
                report = _unavailable_report("schema_capability_unavailable")
            else:
                report = _scan_snapshot(connection, displayed_limit)
    except KeyboardInterrupt:
        failure_reason = "internal_consistency_failure"
    except sqlite3.Error as exc:
        failure_reason = _sqlite_failure_reason(
            getattr(exc, "sqlite_errorcode", None)
        )
        exc = None
    except (PersistentProfileReconciliationError, PersistentProfileDomainError):
        failure_reason = "internal_consistency_failure"
    except Exception:
        failure_reason = "internal_consistency_failure"
    finally:
        if owned_transaction:
            cleanup_succeeded, cleanup_had_failure = (
                _cleanup_owned_transaction(connection)
            )
    if not cleanup_succeeded:
        return _unavailable_report("internal_consistency_failure")
    if cleanup_had_failure and failure_reason is None and (
        report is None or report.status != "unavailable"
    ):
        return _unavailable_report("internal_consistency_failure")
    if failure_reason is not None:
        return _unavailable_report(failure_reason)
    if report is None:
        return _unavailable_report("internal_consistency_failure")
    return report


def _cleanup_owned_transaction(connection) -> tuple[bool, bool]:
    """End only a reconciler-owned transaction and verify the postcondition."""
    cleanup_had_failure = False
    for _attempt in range(2):
        try:
            transaction_active = connection.in_transaction
        except BaseException:
            return False, True
        if not transaction_active:
            return True, cleanup_had_failure

        rollback_failed = False
        try:
            connection.rollback()
        except BaseException:
            rollback_failed = True
        if rollback_failed:
            cleanup_had_failure = True

        try:
            transaction_active = connection.in_transaction
        except BaseException:
            return False, True
        if not transaction_active:
            return True, cleanup_had_failure
        cleanup_had_failure = True

    sql_rollback_failed = False
    try:
        connection.execute("ROLLBACK")
    except BaseException:
        sql_rollback_failed = True
    if sql_rollback_failed:
        cleanup_had_failure = True

    try:
        return not connection.in_transaction, cleanup_had_failure
    except BaseException:
        return False, True


def _sqlite_failure_reason(error_code) -> str:
    if type(error_code) is int and (error_code & 0xFF) in _BUSY_CODES:
        return "temporary_contention"
    return "internal_consistency_failure"


def _unavailable_report(reason: str) -> PersistentProfileReconciliationReport:
    if reason not in ERROR_REASON_CODES - {"invalid_reconciliation_request"}:
        reason = "internal_consistency_failure"
    return PersistentProfileReconciliationReport(
        report_version=REPORT_VERSION,
        status="unavailable",
        complete=False,
        findings_truncated=False,
        total_findings=0,
        finding_counts_by_code=(),
        inventory=tuple(
            (key, None)
            for key in (
                "current_product_profiles",
                "distinct_principals",
                "product_profile_revisions",
                "product_profile_sources",
                "product_profiles",
            )
        ),
        lifecycle_counts=(),
        revision_kind_counts=(),
        source_type_counts=(),
        foreign_key_violation_count=None,
        findings=(),
        unavailable_reason=reason,
    )


def _scan_snapshot(
    connection: sqlite3.Connection,
    displayed_limit: int,
) -> PersistentProfileReconciliationReport:
    collector = _FindingCollector(displayed_limit)
    inventory, lifecycle_counts, revision_counts, source_counts = _inventory(
        connection
    )
    profile_rows, profile_ordinals = _profile_inventory(connection)

    foreign_key_count = 0
    for foreign_key_count, _row in enumerate(
        connection.execute("PRAGMA foreign_key_check"), start=1
    ):
        collector.add(
            "foreign_key_violation",
            "database",
            orphan_ordinal=foreign_key_count,
        )

    _scan_duplicate_relationships(connection, collector, profile_ordinals)
    _scan_profile_containers(connection, profile_rows, collector)
    _scan_unexpected_current_view_rows(connection, collector)
    _scan_revisions_and_sources(
        connection, profile_rows, profile_ordinals, collector
    )
    _scan_orphan_sources(connection, profile_ordinals, collector)
    _scan_idempotency_conflicts(connection, profile_ordinals, collector)

    return _bounded_report(
        collector,
        inventory=inventory,
        lifecycle_counts=lifecycle_counts,
        revision_kind_counts=revision_counts,
        source_type_counts=source_counts,
        foreign_key_violation_count=foreign_key_count,
    )


def _inventory(connection):
    inventory = {
        "product_profiles": _scalar(
            connection, "SELECT COUNT(*) FROM product_profiles"
        ),
        "product_profile_revisions": _scalar(
            connection, "SELECT COUNT(*) FROM product_profile_revisions"
        ),
        "product_profile_sources": _scalar(
            connection, "SELECT COUNT(*) FROM product_profile_sources"
        ),
        "current_product_profiles": _scalar(
            connection, "SELECT COUNT(*) FROM current_product_profiles"
        ),
        "distinct_principals": _scalar(
            connection,
            "SELECT COUNT(DISTINCT principal_id) FROM product_profiles",
        ),
    }
    lifecycle = _bounded_group_counts(
        connection,
        "SELECT lifecycle_status, COUNT(*) FROM product_profile_revisions "
        "GROUP BY lifecycle_status ORDER BY lifecycle_status",
        _LIFECYCLE_STATES,
    )
    revision_kinds = _bounded_group_counts(
        connection,
        "SELECT revision_kind, COUNT(*) FROM product_profile_revisions "
        "GROUP BY revision_kind ORDER BY revision_kind",
        _ALL_REVISION_KINDS,
    )
    source_types = _bounded_group_counts(
        connection,
        "SELECT source_type, COUNT(*) FROM product_profile_sources "
        "GROUP BY source_type ORDER BY source_type",
        SOURCE_TYPES,
    )
    return inventory, lifecycle, revision_kinds, source_types


def _bounded_group_counts(connection, sql: str, allowed: Iterable[str]) -> dict:
    allowed = frozenset(allowed)
    result = {key: 0 for key in sorted(allowed)}
    unsupported = 0
    for value, count in connection.execute(sql):
        if value in allowed:
            result[value] += count
        else:
            unsupported += count
    if unsupported:
        result["unsupported"] = unsupported
    return result


def _scalar(connection, sql: str):
    return connection.execute(sql).fetchone()[0]


def _profile_inventory(connection):
    cursor = connection.execute(
        "SELECT p.profile_id, p.principal_id, p.environment_namespace, "
        "p.initial_revision_id, p.created_at, principal.environment_namespace "
        "FROM product_profiles p "
        "LEFT JOIN product_principals principal ON principal.principal_id=p.principal_id "
        "ORDER BY p.profile_id"
    )
    rows = []
    ordinals = {}
    for ordinal, row in enumerate(cursor, start=1):
        values = tuple(row)
        rows.append(
            {
                "profile_id": values[0],
                "principal_id": values[1],
                "environment_namespace": values[2],
                "initial_revision_id": values[3],
                "created_at": values[4],
                "principal_environment": values[5],
                "profile_ordinal": ordinal,
            }
        )
        if values[0] not in ordinals:
            ordinals[values[0]] = ordinal
    return tuple(rows), ordinals


def _scan_duplicate_relationships(connection, collector, profile_ordinals):
    for row in connection.execute(
        "SELECT principal_id, MIN(profile_id), COUNT(*) FROM product_profiles "
        "GROUP BY principal_id HAVING COUNT(*) > 1 ORDER BY principal_id"
    ):
        collector.add(
            "duplicate_principal_profile",
            "profile",
            profile_ordinal=profile_ordinals.get(row[1]),
        )
    for row in connection.execute(
        "SELECT profile_id, revision_number, COUNT(*) "
        "FROM product_profile_revisions GROUP BY profile_id, revision_number "
        "HAVING COUNT(*) > 1 ORDER BY profile_id, revision_number"
    ):
        collector.add(
            "duplicate_revision_number",
            "revision",
            profile_ordinal=profile_ordinals.get(row[0]),
            revision_number=_safe_positive_int(row[1]),
        )
    for row in connection.execute(
        "SELECT revision_id, source_ordinal, COUNT(*) FROM product_profile_sources "
        "GROUP BY revision_id, source_ordinal HAVING COUNT(*) > 1 "
        "ORDER BY revision_id, source_ordinal"
    ):
        revision = connection.execute(
            "SELECT profile_id, revision_number FROM product_profile_revisions "
            "WHERE revision_id=?",
            (row[0],),
        ).fetchone()
        collector.add(
            "duplicate_source_ordinal",
            "source",
            profile_ordinal=(
                profile_ordinals.get(revision[0]) if revision is not None else None
            ),
            revision_number=(
                _safe_positive_int(revision[1]) if revision is not None else None
            ),
            source_ordinal=_safe_positive_int(row[1]),
        )


def _scan_profile_containers(connection, profile_rows, collector):
    sql = (
        "WITH ordered_profiles AS ("
        " SELECT profile_id, ROW_NUMBER() OVER (ORDER BY profile_id) AS profile_ordinal"
        " FROM product_profiles"
        "), revision_counts AS ("
        " SELECT profile_id, COUNT(*) AS revision_count, MAX(revision_number) AS max_number"
        " FROM product_profile_revisions GROUP BY profile_id"
        ") "
        "SELECT ordered.profile_ordinal, p.profile_id, p.principal_id, "
        "p.environment_namespace, p.initial_revision_id, p.created_at, "
        "principal.environment_namespace AS principal_environment, "
        "COALESCE(counts.revision_count, 0) AS revision_count, "
        "latest.revision_id AS latest_revision_id, "
        "latest.revision_number AS latest_revision_number, "
        "latest.revision_kind AS latest_revision_kind, "
        "latest.lifecycle_status AS latest_lifecycle_status, "
        "latest.canonical_schema_version AS latest_schema_version, "
        "latest.structured_profile_json AS latest_structured_json, "
        "latest.structured_profile_sha256 AS latest_structured_hash, "
        "latest.created_at AS latest_created_at, "
        "initial.revision_id AS initial_revision_id_found, "
        "initial.profile_id AS initial_profile_id_found, "
        "initial.revision_number AS initial_revision_number, "
        "initial.revision_kind AS initial_revision_kind, "
        "view.profile_id AS view_profile_id, view.principal_id AS view_principal_id, "
        "view.environment_namespace AS view_environment, "
        "view.initial_revision_id AS view_initial_revision_id, "
        "view.profile_created_at AS view_profile_created_at, "
        "view.current_revision_id AS view_revision_id, "
        "view.current_revision_number AS view_revision_number, "
        "view.current_revision_kind AS view_revision_kind, "
        "view.lifecycle_status AS view_lifecycle, "
        "view.canonical_schema_version AS view_schema_version, "
        "view.structured_profile_json AS view_structured_json, "
        "view.structured_profile_sha256 AS view_structured_hash, "
        "view.revised_at AS view_revised_at "
        "FROM ordered_profiles ordered "
        "JOIN product_profiles p ON p.profile_id=ordered.profile_id "
        "LEFT JOIN product_principals principal ON principal.principal_id=p.principal_id "
        "LEFT JOIN revision_counts counts ON counts.profile_id=p.profile_id "
        "LEFT JOIN product_profile_revisions latest "
        " ON latest.profile_id=p.profile_id AND latest.revision_number=counts.max_number "
        "LEFT JOIN product_profile_revisions initial "
        " ON initial.revision_id=p.initial_revision_id "
        "LEFT JOIN current_product_profiles view ON view.profile_id=p.profile_id "
        "ORDER BY ordered.profile_ordinal"
    )
    cursor = connection.execute(sql)
    columns = [item[0] for item in cursor.description]
    for raw in cursor:
        profile = dict(zip(columns, tuple(raw)))
        ordinal = profile["profile_ordinal"]
        try:
            _validate_profile_container(profile, collector)
        except Exception:
            collector.add(
                "row_read_failure", "profile", profile_ordinal=ordinal
            )


def _scan_unexpected_current_view_rows(connection, collector):
    for orphan_ordinal, _profile_id in enumerate(
        (
            row[0]
            for row in connection.execute(
                "SELECT view.profile_id FROM current_product_profiles view "
                "LEFT JOIN product_profiles profile ON profile.profile_id=view.profile_id "
                "WHERE profile.profile_id IS NULL ORDER BY view.profile_id"
            )
        ),
        start=1,
    ):
        collector.add(
            "unexpected_current_view_row",
            "current_view",
            orphan_ordinal=orphan_ordinal,
        )


def _validate_profile_container(profile, collector):
    ordinal = profile["profile_ordinal"]
    profile_id = profile["profile_id"]
    if not _valid_identifier(validate_profile_id, profile_id):
        collector.add("invalid_profile_id", "profile", profile_ordinal=ordinal)
    if profile["principal_environment"] is None:
        collector.add(
            "missing_principal_relationship", "profile", profile_ordinal=ordinal
        )
    elif profile["principal_environment"] != profile["environment_namespace"]:
        collector.add(
            "profile_environment_mismatch", "profile", profile_ordinal=ordinal
        )
    if not _valid_timestamp(profile["created_at"]):
        collector.add(
            "invalid_profile_timestamp", "profile", profile_ordinal=ordinal
        )

    revision_count = profile["revision_count"]
    latest_revision_id = profile["latest_revision_id"]
    if revision_count == 0 or latest_revision_id is None:
        collector.add(
            "missing_revision_history", "profile", profile_ordinal=ordinal
        )
        collector.add(
            "missing_current_revision", "profile", profile_ordinal=ordinal
        )

    if (
        profile["initial_revision_id_found"] is None
        or profile["initial_profile_id_found"] != profile_id
        or profile["initial_revision_number"] != 1
        or profile["initial_revision_kind"] != "initial"
    ):
        collector.add(
            "invalid_initial_revision", "profile", profile_ordinal=ordinal
        )

    if profile["view_profile_id"] is None:
        collector.add(
            "missing_current_view_row", "profile", profile_ordinal=ordinal
        )
        return
    if latest_revision_id is None:
        collector.add(
            "unexpected_current_view_row", "profile", profile_ordinal=ordinal
        )
        return
    view = (
        profile["view_profile_id"],
        profile["view_principal_id"],
        profile["view_environment"],
        profile["view_initial_revision_id"],
        profile["view_profile_created_at"],
        profile["view_revision_id"],
        profile["view_revision_number"],
        profile["view_revision_kind"],
        profile["view_lifecycle"],
        profile["view_schema_version"],
        profile["view_structured_json"],
        profile["view_structured_hash"],
        profile["view_revised_at"],
    )
    expected = (
        profile_id,
        profile["principal_id"],
        profile["environment_namespace"],
        profile["initial_revision_id"],
        profile["created_at"],
        latest_revision_id,
        profile["latest_revision_number"],
        profile["latest_revision_kind"],
        profile["latest_lifecycle_status"],
        profile["latest_schema_version"],
        profile["latest_structured_json"],
        profile["latest_structured_hash"],
        profile["latest_created_at"],
    )
    if view[5] != latest_revision_id:
        collector.add(
            "foreign_current_revision", "profile", profile_ordinal=ordinal
        )
    if view[6] != profile["latest_revision_number"]:
        collector.add(
            "stale_current_revision", "profile", profile_ordinal=ordinal
        )
    if view[8] != profile["latest_lifecycle_status"]:
        collector.add(
            "profile_lifecycle_mismatch", "profile", profile_ordinal=ordinal
        )
    if view != expected:
        collector.add(
            "current_view_mismatch", "profile", profile_ordinal=ordinal
        )


def _scan_revisions_and_sources(
    connection, profile_rows, profile_ordinals, collector
):
    profiles_by_id = {row["profile_id"]: row for row in profile_rows}
    sql = (
        "WITH orphan_order AS ("
        " SELECT revision_id, ROW_NUMBER() OVER (ORDER BY revision_id) AS orphan_ordinal"
        " FROM product_profile_revisions r"
        " WHERE NOT EXISTS (SELECT 1 FROM product_profiles p WHERE p.profile_id=r.profile_id)"
        ") "
        "SELECT r.revision_id, r.profile_id, r.principal_id, r.environment_namespace, "
        "r.revision_number, r.previous_revision_id, r.correction_of_revision_id, "
        "r.revision_kind, r.lifecycle_status, r.canonical_schema_version, "
        "r.structured_profile_json, r.structured_profile_sha256, r.source_count, "
        "r.source_bundle_sha256, r.normalizer_version, r.reviewer_version, "
        "r.actor_type, r.reason_code, r.idempotency_key, r.request_fingerprint, "
        "r.created_at, orphan_order.orphan_ordinal, "
        "s.source_id AS source_id, s.revision_id AS source_revision_id, "
        "s.profile_id AS source_profile_id, s.principal_id AS source_principal_id, "
        "s.environment_namespace AS source_environment_namespace, "
        "s.source_ordinal AS source_ordinal, s.source_type AS source_type, "
        "s.source_format AS source_format, s.source_content AS source_content, "
        "s.source_content_sha256 AS source_content_sha256, "
        "s.source_schema_version AS source_schema_version, "
        "s.parser_version AS source_parser_version, s.accepted_at AS source_accepted_at "
        "FROM product_profile_revisions r "
        "LEFT JOIN orphan_order ON orphan_order.revision_id=r.revision_id "
        "LEFT JOIN product_profile_sources s ON s.revision_id=r.revision_id "
        "ORDER BY CASE WHEN r.profile_id IN (SELECT profile_id FROM product_profiles) "
        "THEN 0 ELSE 1 END, r.profile_id, r.revision_number, r.revision_id, "
        "s.source_ordinal, s.source_id"
    )
    cursor = connection.execute(sql)
    columns = [item[0] for item in cursor.description]
    current_id = object()
    revision = None
    sources = []
    chains = {}
    for raw in cursor:
        row = dict(zip(columns, tuple(raw)))
        revision_id = row["revision_id"]
        if revision is not None and revision_id != current_id:
            _safe_validate_revision(
                revision,
                sources,
                profiles_by_id,
                profile_ordinals,
                chains,
                collector,
            )
            sources = []
        if revision_id != current_id:
            revision = {key: row[key] for key in columns[:22]}
            current_id = revision_id
        if row["source_id"] is not None:
            sources.append(
                {
                    "source_id": row["source_id"],
                    "revision_id": row["source_revision_id"],
                    "profile_id": row["source_profile_id"],
                    "principal_id": row["source_principal_id"],
                    "environment_namespace": row["source_environment_namespace"],
                    "source_ordinal": row["source_ordinal"],
                    "source_type": row["source_type"],
                    "source_format": row["source_format"],
                    "source_content": row["source_content"],
                    "source_content_sha256": row["source_content_sha256"],
                    "source_schema_version": row["source_schema_version"],
                    "parser_version": row["source_parser_version"],
                    "accepted_at": row["source_accepted_at"],
                }
            )
    if revision is not None:
        _safe_validate_revision(
            revision,
            sources,
            profiles_by_id,
            profile_ordinals,
            chains,
            collector,
        )


def _safe_validate_revision(
    revision, sources, profiles_by_id, profile_ordinals, chains, collector
):
    profile_id = revision["profile_id"]
    profile_ordinal = profile_ordinals.get(profile_id)
    revision_number = _safe_positive_int(revision["revision_number"])
    orphan_ordinal = _safe_positive_int(revision["orphan_ordinal"])
    try:
        if profile_ordinal is None:
            collector.add(
                "orphan_revision",
                "revision",
                revision_number=revision_number,
                orphan_ordinal=orphan_ordinal,
            )
        profile = profiles_by_id.get(profile_id)
        chain = chains.setdefault(
            profile_id,
            {
                "expected_number": 1,
                "previous_id": None,
                "previous_lifecycle": None,
                "previous_timestamp": None,
                "previous_structured": None,
                "revisions": {},
                "deletion_seen": False,
            },
        )
        _validate_revision(
            revision,
            sources,
            profile,
            chain,
            collector,
            profile_ordinal=profile_ordinal,
            revision_number=revision_number,
            orphan_ordinal=orphan_ordinal,
        )
    except Exception:
        collector.add(
            "row_read_failure",
            "revision",
            profile_ordinal=profile_ordinal,
            revision_number=revision_number,
            orphan_ordinal=orphan_ordinal,
        )


def _validate_revision(
    revision,
    sources,
    profile,
    chain,
    collector,
    *,
    profile_ordinal,
    revision_number,
    orphan_ordinal,
):
    locator = {
        "profile_ordinal": profile_ordinal,
        "revision_number": revision_number,
        "orphan_ordinal": orphan_ordinal if profile_ordinal is None else None,
    }
    revision_id = revision["revision_id"]
    if not _valid_identifier(validate_revision_id, revision_id):
        collector.add("invalid_revision_id", "revision", **locator)
    if profile is not None and (
        revision["principal_id"] != profile["principal_id"]
        or revision["environment_namespace"] != profile["environment_namespace"]
    ):
        collector.add("revision_relationship_mismatch", "revision", **locator)

    raw_number = revision["revision_number"]
    if type(raw_number) is not int or raw_number < 1:
        collector.add("revision_number_gap", "revision", **locator)
    elif raw_number != chain["expected_number"]:
        collector.add("revision_number_gap", "revision", **locator)
    if raw_number == 1:
        if (
            revision["revision_kind"] != "initial"
            or revision["previous_revision_id"] is not None
            or revision["correction_of_revision_id"] is not None
            or revision["lifecycle_status"] != "active"
            or (profile is not None and profile["initial_revision_id"] != revision_id)
        ):
            collector.add("invalid_initial_revision", "revision", **locator)
    elif revision["revision_kind"] == "initial":
        collector.add("unexpected_initial_revision", "revision", **locator)
    if raw_number != 1 and revision["previous_revision_id"] != chain["previous_id"]:
        collector.add("invalid_revision_chain", "revision", **locator)

    kind = revision["revision_kind"]
    lifecycle = revision["lifecycle_status"]
    if kind not in _ALL_REVISION_KINDS:
        collector.add("unsupported_revision_kind", "revision", **locator)
    if chain["deletion_seen"]:
        collector.add("revision_after_deletion_request", "revision", **locator)
    if raw_number != 1 and not _valid_lifecycle_transition(
        chain["previous_lifecycle"], kind, lifecycle
    ):
        collector.add("invalid_lifecycle_transition", "revision", **locator)

    correction_target = revision["correction_of_revision_id"]
    if kind == "correction":
        target_number = chain["revisions"].get(correction_target)
        if (
            correction_target is None
            or target_number is None
            or type(raw_number) is not int
            or target_number >= raw_number
        ):
            collector.add("invalid_correction_target", "revision", **locator)
    elif correction_target is not None:
        collector.add("invalid_correction_target", "revision", **locator)

    timestamp = revision["created_at"]
    if (
        not _valid_timestamp(timestamp)
        or (profile is not None and _valid_timestamp(profile["created_at"]) and timestamp < profile["created_at"])
        or (
            chain["previous_timestamp"] is not None
            and _valid_timestamp(timestamp)
            and timestamp < chain["previous_timestamp"]
        )
    ):
        collector.add("invalid_revision_timestamp", "revision", **locator)

    canonical_bytes = _validate_canonical_revision(
        revision, collector, locator
    )
    if kind in _LIFECYCLE_KINDS and chain["previous_structured"] is not None:
        previous_json, previous_hash, previous_schema = chain["previous_structured"]
        if (
            revision["structured_profile_json"] != previous_json
            or revision["structured_profile_sha256"] != previous_hash
            or revision["canonical_schema_version"] != previous_schema
        ):
            collector.add(
                "invalid_lifecycle_transition", "revision", **locator
            )

    _validate_sources(
        revision,
        sources,
        profile,
        collector,
        locator,
    )
    if not _IDEMPOTENCY_KEY_PATTERN.fullmatch(revision["idempotency_key"] or ""):
        collector.add("malformed_idempotency_key", "revision", **locator)
    if not _is_sha256(revision["request_fingerprint"]):
        collector.add("malformed_request_fingerprint", "revision", **locator)

    if type(raw_number) is int and raw_number >= 1:
        chain["expected_number"] = max(chain["expected_number"], raw_number + 1)
        chain["revisions"][revision_id] = raw_number
    chain["previous_id"] = revision_id
    chain["previous_lifecycle"] = lifecycle
    chain["previous_timestamp"] = timestamp if _valid_timestamp(timestamp) else None
    chain["previous_structured"] = (
        revision["structured_profile_json"],
        revision["structured_profile_sha256"],
        revision["canonical_schema_version"],
    )
    if lifecycle == "deletion_requested":
        chain["deletion_seen"] = True
    canonical_bytes = None


def _validate_canonical_revision(revision, collector, locator):
    if revision["canonical_schema_version"] != CANONICAL_PROFILE_V2:
        collector.add(
            "canonical_schema_version_mismatch", "revision", **locator
        )
        return None
    raw = revision["structured_profile_json"]
    if type(raw) is not str:
        collector.add("malformed_structured_profile", "revision", **locator)
        return None
    try:
        stored_bytes = raw.encode("utf-8")
        profile = parse_canonical_profile_v2_json(stored_bytes)
        canonical_bytes = canonical_profile_v2_json_bytes(profile)
    except CanonicalProfileV2Error as exc:
        reasons = frozenset(exc.reason_codes)
        exc = None
        code = (
            "malformed_structured_profile"
            if reasons & {"invalid_json", "duplicate_json_key", "root_not_object"}
            else "invalid_canonical_profile_v2"
        )
        collector.add(code, "revision", **locator)
        return None
    except (UnicodeEncodeError, TypeError):
        collector.add("malformed_structured_profile", "revision", **locator)
        return None
    if profile["identity"]["profile_id"] != revision["profile_id"]:
        collector.add(
            "structured_profile_identity_mismatch", "revision", **locator
        )
    if stored_bytes != canonical_bytes:
        collector.add("noncanonical_structured_profile", "revision", **locator)
    digest = revision["structured_profile_sha256"]
    if not _is_sha256(digest):
        collector.add("malformed_structured_hash", "revision", **locator)
    elif not hmac.compare_digest(hashlib.sha256(canonical_bytes).hexdigest(), digest):
        collector.add("structured_hash_mismatch", "revision", **locator)
    return canonical_bytes


def _validate_sources(revision, sources, profile, collector, locator):
    expected_count = revision["source_count"]
    if type(expected_count) is not int or expected_count != len(sources):
        collector.add("source_count_mismatch", "revision", **locator)
    valid_for_bundle = True
    expected_ordinal = 1
    manifest_sources = []
    for source in sources:
        source_ordinal = _safe_positive_int(source["source_ordinal"])
        source_locator = {
            **locator,
            "source_ordinal": source_ordinal,
        }
        if not _valid_identifier(validate_source_id, source["source_id"]):
            collector.add("invalid_source_id", "source", **source_locator)
            valid_for_bundle = False
        if (
            source["revision_id"] != revision["revision_id"]
            or source["profile_id"] != revision["profile_id"]
            or source["principal_id"] != revision["principal_id"]
            or source["environment_namespace"] != revision["environment_namespace"]
        ):
            collector.add(
                "source_relationship_mismatch", "source", **source_locator
            )
            valid_for_bundle = False
        if type(source["source_ordinal"]) is not int or source["source_ordinal"] != expected_ordinal:
            collector.add("source_ordinal_gap", "source", **source_locator)
            valid_for_bundle = False
        expected_ordinal += 1

        source_type = source["source_type"]
        if source_type not in SOURCE_TYPES:
            collector.add("unsupported_source_type", "source", **source_locator)
            valid_for_bundle = False
        if not _source_type_matches_revision(source_type, revision["revision_kind"]):
            collector.add(
                "invalid_source_for_revision_kind", "source", **source_locator
            )
            valid_for_bundle = False
        if (
            source_type == "confirmed_lifecycle_action"
            and _safe_lifecycle_action(source["source_content"])
            != revision["revision_kind"]
        ):
            collector.add(
                "invalid_source_for_revision_kind", "source", **source_locator
            )
            valid_for_bundle = False
        content_bytes = _validate_source_payload(source, collector, source_locator)
        if content_bytes is None:
            valid_for_bundle = False
        digest = source["source_content_sha256"]
        if not _is_sha256(digest):
            collector.add("malformed_source_hash", "source", **source_locator)
            valid_for_bundle = False
        elif content_bytes is not None and not hmac.compare_digest(
            hashlib.sha256(content_bytes).hexdigest(), digest
        ):
            collector.add("source_hash_mismatch", "source", **source_locator)
            valid_for_bundle = False
        if (
            not _valid_timestamp(source["accepted_at"])
            or (
                _valid_timestamp(revision["created_at"])
                and source["accepted_at"] > revision["created_at"]
            )
            or (
                profile is not None
                and _valid_timestamp(profile["created_at"])
                and source["accepted_at"] < profile["created_at"]
            )
        ):
            collector.add("invalid_source_timestamp", "source", **source_locator)
            valid_for_bundle = False
        if content_bytes is not None and _is_sha256(digest):
            manifest_sources.append(
                {
                    "ordinal": source["source_ordinal"],
                    "source_type": source_type,
                    "source_format": source["source_format"],
                    "source_schema_version": source["source_schema_version"],
                    "parser_version": source["parser_version"],
                    "confirmed_at": source["accepted_at"],
                    "byte_length": len(content_bytes),
                    "source_content_hash": hashlib.sha256(content_bytes).hexdigest(),
                }
            )

    if revision["revision_kind"] in _LIFECYCLE_KINDS and len(sources) != 1:
        collector.add(
            "invalid_source_for_revision_kind", "revision", **locator
        )
        valid_for_bundle = False
    if revision["revision_kind"] == "initial" and not any(
        source["source_type"] == "confirmed_about_you_text" for source in sources
    ):
        collector.add(
            "invalid_source_for_revision_kind", "revision", **locator
        )
        valid_for_bundle = False
    if revision["revision_kind"] == "correction" and not any(
        source["source_type"] == "user_confirmed_correction" for source in sources
    ):
        collector.add(
            "invalid_source_for_revision_kind", "revision", **locator
        )
        valid_for_bundle = False

    bundle_digest = revision["source_bundle_sha256"]
    if not _is_sha256(bundle_digest):
        collector.add("source_bundle_hash_mismatch", "revision", **locator)
    elif not sources:
        empty_manifest = {
            "version": SOURCE_BUNDLE_HASH_VERSION,
            "sources": [],
        }
        recomputed = hashlib.sha256(
            _canonical_json_bytes(empty_manifest)
        ).hexdigest()
        if not hmac.compare_digest(recomputed, bundle_digest):
            collector.add("source_bundle_hash_mismatch", "revision", **locator)
    elif valid_for_bundle and len(manifest_sources) == len(sources):
        manifest = {
            "version": SOURCE_BUNDLE_HASH_VERSION,
            "sources": manifest_sources,
        }
        recomputed = hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest()
        if not hmac.compare_digest(recomputed, bundle_digest):
            collector.add("source_bundle_hash_mismatch", "revision", **locator)


def _validate_source_payload(source, collector, locator):
    content = source["source_content"]
    source_type = source["source_type"]
    expected_format = {
        "confirmed_about_you_text": "text/plain",
        "user_confirmed_correction": "application/json",
        "confirmed_lifecycle_action": "application/json",
    }.get(source_type)
    failed = False
    encoded = None
    try:
        if expected_format is None or source["source_format"] != expected_format:
            failed = True
        else:
            encoded = _validate_source_content(
                content,
                require_json_object=source["source_format"] == "application/json",
            )
            _validate_version(source["source_schema_version"])
            _validate_version(source["parser_version"], optional=True)
            if source_type == "confirmed_lifecycle_action":
                expected = json.dumps(
                    {
                        "action": _lifecycle_action_from_source(content),
                        "schema_version": LIFECYCLE_SOURCE_SCHEMA_VERSION,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if (
                    source["source_schema_version"]
                    != LIFECYCLE_SOURCE_SCHEMA_VERSION
                    or content != expected
                ):
                    failed = True
    except (PersistentProfileDomainError, TypeError, ValueError):
        failed = True
        encoded = None
    if failed:
        collector.add("malformed_source_payload", "source", **locator)
        return None
    return encoded


def _lifecycle_action_from_source(content):
    parsed = json.loads(content)
    action = parsed.get("action") if type(parsed) is dict else None
    if action not in _LIFECYCLE_KINDS:
        raise ValueError
    return action


def _safe_lifecycle_action(content):
    try:
        return _lifecycle_action_from_source(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _source_type_matches_revision(source_type, revision_kind) -> bool:
    if revision_kind in _LIFECYCLE_KINDS:
        return source_type == "confirmed_lifecycle_action"
    if revision_kind in {"initial", "edit", "correction"}:
        return source_type != "confirmed_lifecycle_action"
    return False


def _valid_lifecycle_transition(previous, kind, lifecycle) -> bool:
    if previous not in _LIFECYCLE_STATES or lifecycle not in _LIFECYCLE_STATES:
        return False
    if previous == "deletion_requested":
        return False
    if kind in {"edit", "correction"}:
        return lifecycle == previous
    if kind == "archive":
        return previous == "active" and lifecycle == "archived"
    if kind == "reactivate":
        return previous == "archived" and lifecycle == "active"
    if kind == "deletion_request":
        return previous in {"active", "archived"} and lifecycle == "deletion_requested"
    return False


def _scan_orphan_sources(connection, profile_ordinals, collector):
    cursor = connection.execute(
        "SELECT s.profile_id, s.source_ordinal, "
        "ROW_NUMBER() OVER (ORDER BY s.source_id) "
        "FROM product_profile_sources s "
        "LEFT JOIN product_profile_revisions r ON r.revision_id=s.revision_id "
        "WHERE r.revision_id IS NULL ORDER BY s.source_id"
    )
    for profile_id, source_ordinal, orphan_ordinal in cursor:
        collector.add(
            "orphan_source",
            "source",
            profile_ordinal=profile_ordinals.get(profile_id),
            source_ordinal=_safe_positive_int(source_ordinal),
            orphan_ordinal=_safe_positive_int(orphan_ordinal),
        )


def _scan_idempotency_conflicts(connection, profile_ordinals, collector):
    for profile_id, revision_number in connection.execute(
        "SELECT MIN(profile_id), MIN(revision_number) "
        "FROM product_profile_revisions GROUP BY principal_id, idempotency_key "
        "HAVING COUNT(*) > 1 ORDER BY principal_id, idempotency_key"
    ):
        collector.add(
            "idempotency_scope_conflict",
            "revision",
            profile_ordinal=profile_ordinals.get(profile_id),
            revision_number=_safe_positive_int(revision_number),
        )


def _valid_identifier(validator, value) -> bool:
    try:
        validator(value)
    except PersistentProfileDomainError:
        return False
    return True


def _valid_timestamp(value) -> bool:
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() != timedelta(0) or parsed.microsecond != 0:
            return False
        return canonical_utc_timestamp(parsed) == value
    except (ValueError, PersistentProfileDomainError):
        return False


def _is_sha256(value) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _safe_positive_int(value):
    return value if type(value) is int and value >= 1 else None


def _bounded_report(
    collector,
    *,
    inventory,
    lifecycle_counts,
    revision_kind_counts,
    source_type_counts,
    foreign_key_violation_count,
):
    displayed = list(collector.displayed)
    def build(display_count):
        return PersistentProfileReconciliationReport(
            report_version=REPORT_VERSION,
            status="clean" if collector.total == 0 else "findings",
            complete=True,
            findings_truncated=display_count < collector.total,
            total_findings=collector.total,
            finding_counts_by_code=tuple(sorted(collector.counts.items())),
            inventory=tuple(sorted(inventory.items())),
            lifecycle_counts=tuple(sorted(lifecycle_counts.items())),
            revision_kind_counts=tuple(sorted(revision_kind_counts.items())),
            source_type_counts=tuple(sorted(source_type_counts.items())),
            foreign_key_violation_count=foreign_key_violation_count,
            findings=tuple(displayed[:display_count]),
        )

    complete_display = build(len(displayed))
    if len(complete_display.to_json_bytes()) <= MAX_REPORT_BYTES:
        return complete_display
    empty_display = build(0)
    if len(empty_display.to_json_bytes()) > MAX_REPORT_BYTES:
        return _unavailable_report("internal_consistency_failure")
    low = 0
    high = len(displayed)
    while low < high:
        middle = (low + high + 1) // 2
        if len(build(middle).to_json_bytes()) <= MAX_REPORT_BYTES:
            low = middle
        else:
            high = middle - 1
    return build(low)
