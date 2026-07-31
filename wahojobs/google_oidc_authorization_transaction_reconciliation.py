"""Bounded read-only reconciliation for durable Google OIDC transactions.

The public operation never executes SQL on its caller's connection.  It makes
a bounded coherent copy into callback-free private SQLite connections, applies
a deny-write inspection policy there, loads no keys, decrypts no protected
material, and never repairs durable state.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3

from wahojobs.google_oidc_authorization_transaction_schema import (
    MIGRATION_VERSION,
    attest_google_oidc_authorization_transaction_schema,
    is_m006_verification_index_list_pragma,
)
from wahojobs.google_oidc_authorization_transactions import (
    GOOGLE_OIDC_RECONCILIATION_BUDGET,
)


REPORT_VERSION = "google_oidc_authorization_transaction_reconciliation_v1"
INTEGRITY_SCOPE = (
    "structural_and_exact_reuse_without_cryptographic_authentication"
)
DEFAULT_MAX_FINDINGS = 250
MAX_RECONCILIATION_ROWS = (
    GOOGLE_OIDC_RECONCILIATION_BUDGET.max_scan_rows
)
MAX_FINDINGS = (
    GOOGLE_OIDC_RECONCILIATION_BUDGET.max_retained_findings
)
MAX_OUTPUT_BYTES = GOOGLE_OIDC_RECONCILIATION_BUDGET.max_output_bytes
_REPOSITORY_RECONCILIATION_BUDGET = GOOGLE_OIDC_RECONCILIATION_BUDGET
# Compatibility for the phase-2B1 report API.  Both names are the same
# repository-wide output contract; there is no second report-size budget.
MAX_REPORT_BYTES = MAX_OUTPUT_BYTES
MAX_KEY_VERSION = 2_147_483_647
_MAX_AUTHORIZER_TABLE_XINFO_SCOPE = 8_192
_TABLE_XINFO_SCOPE_ROW_LIMIT = 8_193
_TABLE_XINFO_SCOPE_PROGRESS_GRANULARITY = 100
_MAX_TABLE_XINFO_SCOPE_PROGRESS_CALLS = 4_096
_PROGRESS_HANDLER_CLEAR_PASSES = 8

_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$"
)
_TRANSACTION_ID = re.compile(r"^oidctx_[0-9a-f]{32}$")
_LIFECYCLES = ("prepared", "consumed", "expired", "invalidated")
_BUSY_CODES = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
_ERROR_REASONS = frozenset(
    {
        "invalid_reconciliation_request",
        "schema_capability_unavailable",
        "temporary_contention",
        "inspection_boundary_unavailable",
        "internal_consistency_failure",
    }
)
_INCOMPLETE_REASONS = frozenset(
    {
        "operation_budget_exceeded",
        "row_scan_limit_exceeded",
        "integrity_check_limit_exceeded",
        "foreign_key_scan_limit_exceeded",
        "finding_retention_limit_exceeded",
        "output_byte_limit_exceeded",
    }
)

_FINDING_CODE_ORDER = (
    "integrity_check_failed",
    "foreign_keys_disabled",
    "foreign_key_violation",
    "invalid_storage_class",
    "invalid_transaction_id",
    "unsupported_record_version",
    "unexpected_provider",
    "invalid_environment_namespace",
    "malformed_configuration_fingerprint",
    "unsupported_state_digest_version",
    "unknown_lookup_key_version",
    "malformed_state_lookup_digest",
    "invalid_created_at",
    "invalid_expires_at",
    "invalid_ten_minute_chronology",
    "unsupported_lifecycle",
    "invalid_lifecycle_row_version",
    "invalid_claimed_chronology",
    "invalid_terminal_chronology",
    "prepared_already_expired",
    "terminal_missing_chronology",
    "unsupported_protection_envelope_version",
    "unknown_protection_key_version",
    "malformed_protection_nonce",
    "malformed_protected_material",
    "duplicate_state_lookup",
    "duplicate_protection_nonce",
    "protected_material_reuse",
    "protected_material_metadata_conflict",
    "nonce_protected_material_reuse",
    "protection_nonce_metadata_conflict",
    "contradictory_transaction_row",
    "row_read_failure",
)
_FINDING_CODES = frozenset(_FINDING_CODE_ORDER)
_FINDING_CODE_RANK = {
    code: rank for rank, code in enumerate(_FINDING_CODE_ORDER)
}
_FINDING_CATEGORY = {
    **{
        code: 0
        for code in (
            "integrity_check_failed",
            "foreign_keys_disabled",
            "foreign_key_violation",
        )
    },
    **{
        code: 1
        for code in (
            "invalid_storage_class",
            "invalid_transaction_id",
            "unsupported_record_version",
            "unexpected_provider",
            "invalid_environment_namespace",
            "malformed_configuration_fingerprint",
            "unsupported_state_digest_version",
            "unknown_lookup_key_version",
            "malformed_state_lookup_digest",
            "invalid_created_at",
            "invalid_expires_at",
            "invalid_ten_minute_chronology",
            "unsupported_lifecycle",
            "invalid_lifecycle_row_version",
            "invalid_claimed_chronology",
            "invalid_terminal_chronology",
            "prepared_already_expired",
            "terminal_missing_chronology",
            "unsupported_protection_envelope_version",
            "unknown_protection_key_version",
            "malformed_protection_nonce",
            "malformed_protected_material",
            "contradictory_transaction_row",
        )
    },
    **{
        code: 2
        for code in (
            "duplicate_state_lookup",
            "duplicate_protection_nonce",
            "protected_material_reuse",
            "protected_material_metadata_conflict",
            "nonce_protected_material_reuse",
            "protection_nonce_metadata_conflict",
        )
    },
    "row_read_failure": 3,
}
if set(_FINDING_CATEGORY) != _FINDING_CODES:
    raise RuntimeError("reconciliation_finding_taxonomy_invalid")

_COLUMNS = (
    "transaction_id",
    "record_version",
    "provider",
    "environment_namespace",
    "configuration_fingerprint",
    "state_digest_version",
    "lookup_key_version",
    "state_lookup_digest",
    "created_at",
    "expires_at",
    "lifecycle",
    "claimed_at",
    "terminal_at",
    "row_version",
    "protection_envelope_version",
    "protection_key_version",
    "protection_nonce",
    "protected_material",
)
_VALUE_CAPS = {
    "transaction_id": 40,
    "record_version": 33,
    "provider": 16,
    "environment_namespace": 65,
    "configuration_fingerprint": 33,
    "state_digest_version": 33,
    "lookup_key_version": 33,
    "state_lookup_digest": 33,
    "created_at": 26,
    "expires_at": 26,
    "lifecycle": 16,
    "claimed_at": 26,
    "terminal_at": 26,
    "row_version": 33,
    "protection_envelope_version": 33,
    "protection_key_version": 33,
    "protection_nonce": 13,
    "protected_material": 529,
}
_TEXT_COLUMNS = frozenset(
    {
        "transaction_id",
        "provider",
        "environment_namespace",
        "created_at",
        "expires_at",
        "lifecycle",
        "claimed_at",
        "terminal_at",
    }
)
_EXPECTED_STORAGE = {
    "transaction_id": "text",
    "record_version": "integer",
    "provider": "text",
    "environment_namespace": "text",
    "configuration_fingerprint": "blob",
    "state_digest_version": "integer",
    "lookup_key_version": "integer",
    "state_lookup_digest": "blob",
    "created_at": "text",
    "expires_at": "text",
    "lifecycle": "text",
    "claimed_at": ("null", "text"),
    "terminal_at": ("null", "text"),
    "row_version": "integer",
    "protection_envelope_version": "integer",
    "protection_key_version": "integer",
    "protection_nonce": "blob",
    "protected_material": "blob",
}
_M006_FOREIGN_KEY_LIST_PRAGMA_SCOPE = frozenset(
    {
        ("google_oidc_authorization_transactions", "main"),
        ("legacy_owner_aliases", None),
        ("ownership_binding_events", None),
        ("principal_account_bindings", None),
        ("product_principals", None),
        ("product_profile_revisions", None),
        ("product_profile_sources", None),
        ("product_profiles", None),
        ("user_pipeline_state", "main"),
        ("user_pipeline_transitions", "main"),
        ("wahojobs_schema_migrations", "main"),
    }
)
_M006_INDEX_XINFO_PRAGMA_SCOPE = frozenset(
    {
        (
            "idx_google_oidc_authorization_transactions_prepared_expiry",
            "main",
        ),
        (
            "idx_google_oidc_authorization_transactions_terminal_cleanup",
            "main",
        ),
        ("idx_legacy_owner_aliases_family_coherence", None),
        ("idx_legacy_owner_aliases_principal", None),
        ("idx_ownership_binding_events_binding_time", None),
        ("idx_ownership_binding_events_principal_version", None),
        ("idx_principal_account_bindings_active_identity", None),
        ("idx_principal_account_bindings_user_status", None),
        ("idx_product_principals_environment_type", None),
        ("idx_product_profile_revisions_lifecycle", None),
        ("idx_product_profile_revisions_principal_history", None),
        ("idx_product_profile_revisions_profile_history", None),
        ("idx_product_profile_sources_profile", None),
        ("idx_product_profile_sources_revision", None),
        ("idx_product_profiles_environment", None),
        ("idx_user_pipeline_items_pipeline_profile", "main"),
        ("idx_user_pipeline_transitions_correction", "main"),
        ("idx_user_pipeline_transitions_occurred", "main"),
        ("idx_user_pipeline_transitions_pipeline_occurred", "main"),
        ("idx_user_pipeline_transitions_profile_occurred", "main"),
        ("idx_user_pipeline_transitions_undo", "main"),
        (
            "sqlite_autoindex_google_oidc_authorization_transactions_1",
            "main",
        ),
        ("sqlite_autoindex_legacy_owner_aliases_1", None),
        ("sqlite_autoindex_legacy_owner_aliases_2", None),
        ("sqlite_autoindex_ownership_binding_events_1", None),
        ("sqlite_autoindex_ownership_binding_events_2", None),
        ("sqlite_autoindex_ownership_binding_events_3", None),
        ("sqlite_autoindex_principal_account_bindings_1", None),
        ("sqlite_autoindex_principal_account_bindings_2", None),
        ("sqlite_autoindex_product_principals_1", None),
        ("sqlite_autoindex_product_profile_revisions_1", None),
        ("sqlite_autoindex_product_profile_revisions_2", None),
        ("sqlite_autoindex_product_profile_revisions_3", None),
        ("sqlite_autoindex_product_profile_revisions_4", None),
        ("sqlite_autoindex_product_profile_revisions_5", None),
        ("sqlite_autoindex_product_profile_sources_1", None),
        ("sqlite_autoindex_product_profile_sources_2", None),
        ("sqlite_autoindex_product_profile_sources_3", None),
        ("sqlite_autoindex_product_profiles_1", None),
        ("sqlite_autoindex_product_profiles_2", None),
        ("sqlite_autoindex_product_profiles_3", None),
        ("sqlite_autoindex_product_profiles_4", None),
        ("sqlite_autoindex_user_pipeline_state_1", "main"),
        ("sqlite_autoindex_user_pipeline_transitions_1", "main"),
        ("sqlite_autoindex_user_pipeline_transitions_2", "main"),
        ("sqlite_autoindex_wahojobs_schema_migrations_1", "main"),
        (
            "uq_google_oidc_authorization_transactions_protection_nonce",
            "main",
        ),
        (
            "uq_google_oidc_authorization_transactions_state_lookup",
            "main",
        ),
    }
)
_M006_UNQUALIFIED_TABLE_XINFO_ARGUMENTS = frozenset(
    {
        "legacy_owner_aliases",
        "ownership_binding_events",
        "principal_account_bindings",
        "product_principals",
        "product_profile_revisions",
        "product_profile_sources",
        "product_profiles",
    }
)
_RECONCILIATION_EXACT_PRAGMA_TUPLES = (
    frozenset(
        {
            ("foreign_key_check", None, "main", None),
            ("foreign_keys", None, None, None),
            ("integrity_check", "101", "main", None),
            ("trusted_schema", None, None, None),
        }
    )
    | frozenset(
        ("foreign_key_list", argument, database, None)
        for argument, database in _M006_FOREIGN_KEY_LIST_PRAGMA_SCOPE
    )
    | frozenset(
        ("index_xinfo", argument, database, None)
        for argument, database in _M006_INDEX_XINFO_PRAGMA_SCOPE
    )
)
_ALLOWED_AUTHORIZER_ACTIONS = frozenset(
    value
    for value in (
        getattr(sqlite3, "SQLITE_SELECT", None),
        getattr(sqlite3, "SQLITE_READ", None),
        getattr(sqlite3, "SQLITE_FUNCTION", None),
        getattr(sqlite3, "SQLITE_RECURSIVE", None),
    )
    if type(value) is int
)
_SQLITE_CONNECT = sqlite3.connect
_SQLITE_BACKUP = sqlite3.Connection.backup
_SQLITE_EXECUTE = sqlite3.Connection.execute
_SQLITE_CURSOR = sqlite3.Connection.cursor
_SQLITE_CLOSE = sqlite3.Connection.close
_SQLITE_SERIALIZE = sqlite3.Connection.serialize
_SQLITE_SETCONFIG = sqlite3.Connection.setconfig
_SQLITE_SET_AUTHORIZER = sqlite3.Connection.set_authorizer
_SQLITE_SET_PROGRESS_HANDLER = sqlite3.Connection.set_progress_handler


class GoogleOidcAuthorizationTransactionReconciliationError(Exception):
    """Stable, bounded invocation error."""

    __slots__ = ("reason_code",)

    def __init__(self, reason_code: str):
        if reason_code not in _ERROR_REASONS:
            reason_code = "internal_consistency_failure"
        self.reason_code = reason_code
        super().__init__(
            "Google OIDC authorization-transaction reconciliation failed."
        )

    def public_dict(self) -> dict:
        return {
            "error": "google_oidc_authorization_transaction_reconciliation_error",
            "reason_code": self.reason_code,
        }

    def __repr__(self) -> str:
        return (
            "GoogleOidcAuthorizationTransactionReconciliationError("
            f"reason_code={self.reason_code!r})"
        )


@dataclass(frozen=True, slots=True)
class GoogleOidcAuthorizationTransactionFinding:
    """One sanitized finding located only by a report-local ordinal."""

    code: str
    transaction_ordinal: int | None = None
    severity: str = field(default="error", init=False)

    def __post_init__(self):
        if self.code not in _FINDING_CODES:
            raise ValueError("invalid reconciliation finding")
        if self.transaction_ordinal is not None and (
            type(self.transaction_ordinal) is not int
            or not 1 <= self.transaction_ordinal <= MAX_RECONCILIATION_ROWS
        ):
            raise ValueError("invalid reconciliation finding")

    def to_dict(self) -> dict:
        value = {"code": self.code, "severity": self.severity}
        if self.transaction_ordinal is not None:
            value["transaction_ordinal"] = self.transaction_ordinal
        return value


@dataclass(frozen=True, slots=True)
class GoogleOidcAuthorizationTransactionReconciliationReport:
    """Bounded deterministic reconciliation projection."""

    report_version: str
    migration_version: str
    status: str
    complete: bool
    blocking: bool
    row_scan_limit: int
    rows_observed: int
    rows_inspected: int
    rows_structurally_valid: int
    rows_invalid: int
    rows_omitted: int | None
    rows_known_remaining: int
    row_total_exact: bool
    row_scan_truncated: bool
    total_findings: int | None
    findings_observed: int
    findings_retained: int
    findings_omitted: int | None
    findings_known_omitted: int
    finding_total_exact: bool
    finding_retention_truncated: bool
    output_rendering_truncated: bool
    finding_counts_by_code: tuple[tuple[str, int], ...]
    inventory: tuple[tuple[str, int | None], ...]
    lifecycle_counts: tuple[tuple[str, int], ...]
    accepted_lookup_key_versions: tuple[int, ...]
    accepted_protection_key_versions: tuple[int, ...]
    operation_budget_contract: tuple[tuple[str, int], ...]
    findings: tuple[GoogleOidcAuthorizationTransactionFinding, ...]
    integrity_scope: str = INTEGRITY_SCOPE
    cryptographic_authenticity_verified: bool = False
    semantic_integrity: str = "unverified"
    runtime_safety_established: bool = False
    unavailable_reason: str | None = None
    incomplete_reason: str | None = None

    def __post_init__(self):
        if not _valid_operation_budget_contract(
            self.operation_budget_contract,
            row_scan_limit=self.row_scan_limit,
        ):
            raise ValueError("invalid reconciliation report")
        operation_limits = dict(self.operation_budget_contract)
        integer_fields = (
            self.row_scan_limit,
            self.rows_observed,
            self.rows_inspected,
            self.rows_structurally_valid,
            self.rows_invalid,
            self.rows_known_remaining,
            self.findings_observed,
            self.findings_retained,
            self.findings_known_omitted,
        )
        boolean_fields = (
            self.complete,
            self.blocking,
            self.row_total_exact,
            self.row_scan_truncated,
            self.finding_total_exact,
            self.finding_retention_truncated,
            self.output_rendering_truncated,
            self.cryptographic_authenticity_verified,
            self.runtime_safety_established,
        )
        if (
            self.report_version != REPORT_VERSION
            or self.migration_version != MIGRATION_VERSION
            or self.status
            not in {"clean", "findings", "incomplete", "unavailable"}
            or any(type(value) is not int or value < 0 for value in integer_fields)
            or any(type(value) is not bool for value in boolean_fields)
            or self.row_scan_limit != MAX_RECONCILIATION_ROWS
            or self.rows_inspected > self.row_scan_limit
            or self.rows_observed > self.row_scan_limit + 1
            or self.rows_observed < self.rows_inspected
            or self.rows_structurally_valid + self.rows_invalid
            != self.rows_inspected
            or self.rows_known_remaining not in {0, 1}
            or not _valid_optional_count(self.rows_omitted)
            or not _valid_optional_count(self.total_findings)
            or not _valid_optional_count(self.findings_omitted)
            or not _valid_finding_counts(
                self.finding_counts_by_code,
                operation_limits["result_and_finding_items"] + 1,
            )
            or not _valid_inventory(self.inventory, self.row_scan_limit)
            or not _valid_lifecycle_counts(
                self.lifecycle_counts,
                self.row_scan_limit,
            )
            or self.findings_observed
            > operation_limits["result_and_finding_items"] + 1
            or _validated_key_versions(
                self.accepted_lookup_key_versions
            )
            != self.accepted_lookup_key_versions
            or _validated_key_versions(
                self.accepted_protection_key_versions
            )
            != self.accepted_protection_key_versions
            or type(self.findings) is not tuple
            or len(self.findings) > operation_limits["retained_findings"]
            or any(
                type(finding)
                is not GoogleOidcAuthorizationTransactionFinding
                for finding in self.findings
            )
            or self.findings_retained != len(self.findings)
            or self.findings_retained > self.findings_observed
            or self.findings_known_omitted
            != self.findings_observed - self.findings_retained
            or sum(count for _, count in self.finding_counts_by_code)
            != self.findings_observed
            or not _retained_findings_match_counts(
                self.findings,
                self.finding_counts_by_code,
            )
            or self.integrity_scope != INTEGRITY_SCOPE
            or self.cryptographic_authenticity_verified is not False
            or self.runtime_safety_established is not False
            or self.semantic_integrity not in {"unverified", "contradictory"}
        ):
            raise ValueError("invalid reconciliation report")
        if self.row_total_exact:
            if (
                self.rows_omitted
                != self.rows_observed - self.rows_inspected
                or self.rows_known_remaining != 0
                or self.row_scan_truncated
                != (self.rows_inspected < self.rows_observed)
            ):
                raise ValueError("invalid reconciliation report")
        else:
            if self.rows_omitted is not None:
                raise ValueError("invalid reconciliation report")
            if self.row_scan_truncated:
                if (
                    self.rows_known_remaining == 1
                    and self.rows_observed != self.row_scan_limit + 1
                ):
                    raise ValueError("invalid reconciliation report")
            elif (
                self.rows_observed != 0
                or self.rows_inspected != 0
                or self.rows_known_remaining != 0
            ):
                raise ValueError("invalid reconciliation report")
        inventory = dict(self.inventory)
        if (
            self.row_total_exact
            and inventory["transaction_count"] != self.rows_observed
        ) or (
            not self.row_total_exact
            and inventory["transaction_count"] is not None
        ):
            raise ValueError("invalid reconciliation report")
        if sum(count for _, count in self.lifecycle_counts) > self.rows_inspected:
            raise ValueError("invalid reconciliation report")
        if self.finding_total_exact:
            if (
                self.total_findings != self.findings_observed
                or self.findings_omitted != self.findings_known_omitted
            ):
                raise ValueError("invalid reconciliation report")
        elif self.total_findings is not None or self.findings_omitted is not None:
            raise ValueError("invalid reconciliation report")
        if self.finding_retention_truncated != (
            self.findings_retained < self.findings_observed
        ):
            raise ValueError("invalid reconciliation report")
        if self.complete and (
            self.row_scan_truncated
            or self.finding_retention_truncated
            or self.output_rendering_truncated
            or not self.row_total_exact
            or not self.finding_total_exact
        ):
            raise ValueError("invalid reconciliation report")
        if self.status == "clean" and (
            not self.complete
            or self.blocking
            or self.findings_observed
            or self.semantic_integrity != "unverified"
        ):
            raise ValueError("invalid reconciliation report")
        if self.status == "findings" and (
            not self.complete
            or not self.blocking
            or not self.findings_observed
        ):
            raise ValueError("invalid reconciliation report")
        if self.status in {"incomplete", "unavailable"} and (
            self.complete or not self.blocking
        ):
            raise ValueError("invalid reconciliation report")
        if self.status == "unavailable":
            if (
                self.unavailable_reason is None
                or self.incomplete_reason is not None
            ):
                raise ValueError("invalid reconciliation report")
        elif self.status == "incomplete":
            if (
                self.incomplete_reason is None
                or self.unavailable_reason is not None
            ):
                raise ValueError("invalid reconciliation report")
        elif (
            self.unavailable_reason is not None
            or self.incomplete_reason is not None
        ):
            raise ValueError("invalid reconciliation report")
        if (
            self.unavailable_reason is not None
            and self.unavailable_reason
            not in _ERROR_REASONS - {"invalid_reconciliation_request"}
        ):
            raise ValueError("invalid reconciliation report")
        if (
            self.incomplete_reason is not None
            and self.incomplete_reason not in _INCOMPLETE_REASONS
        ):
            raise ValueError("invalid reconciliation report")

    @property
    def findings_truncated(self) -> bool:
        """Compatibility projection for the original report contract."""

        return (
            self.finding_retention_truncated
            or self.output_rendering_truncated
        )

    @property
    def operation_budget(self) -> dict:
        return dict(self.operation_budget_contract)

    def to_dict(self) -> dict:
        result = {
            "accepted_key_versions": {
                "lookup": list(self.accepted_lookup_key_versions),
                "protection": list(self.accepted_protection_key_versions),
            },
            "blocking": self.blocking,
            "complete": self.complete,
            "cryptographic_authenticity_verified": (
                self.cryptographic_authenticity_verified
            ),
            "finding_counts_by_code": dict(self.finding_counts_by_code),
            "finding_retention_truncated": (
                self.finding_retention_truncated
            ),
            "finding_total_exact": self.finding_total_exact,
            "findings": [finding.to_dict() for finding in self.findings],
            "findings_known_omitted": self.findings_known_omitted,
            "findings_observed": self.findings_observed,
            "findings_omitted": self.findings_omitted,
            "findings_retained": self.findings_retained,
            "findings_truncated": self.findings_truncated,
            "integrity_scope": self.integrity_scope,
            "inventory": dict(self.inventory),
            "lifecycle_counts": dict(self.lifecycle_counts),
            "migration_version": self.migration_version,
            "output_rendering_truncated": self.output_rendering_truncated,
            "operation_budget": self.operation_budget,
            "report_version": self.report_version,
            "row_scan_limit": self.row_scan_limit,
            "row_scan_truncated": self.row_scan_truncated,
            "row_total_exact": self.row_total_exact,
            "rows_inspected": self.rows_inspected,
            "rows_invalid": self.rows_invalid,
            "rows_known_remaining": self.rows_known_remaining,
            "rows_observed": self.rows_observed,
            "rows_omitted": self.rows_omitted,
            "rows_structurally_valid": self.rows_structurally_valid,
            "runtime_safety_established": self.runtime_safety_established,
            "semantic_integrity": self.semantic_integrity,
            "status": self.status,
            "total_findings": self.total_findings,
        }
        if self.unavailable_reason is not None:
            result["unavailable_reason"] = self.unavailable_reason
        if self.incomplete_reason is not None:
            result["incomplete_reason"] = self.incomplete_reason
        return result

    def to_json_bytes(self) -> bytes:
        bounded = _apply_output_budget(
            self,
            self.operation_budget["output_bytes"],
        )
        return _render_json_bytes(bounded)

    def to_json(self) -> str:
        return self.to_json_bytes().decode("utf-8")

    def to_human(self) -> str:
        return self.to_human_bytes().decode("utf-8")

    def to_human_bytes(self) -> bytes:
        bounded = _apply_output_budget(
            self,
            self.operation_budget["output_bytes"],
        )
        return _render_human_bytes(bounded)


def _valid_optional_count(value):
    return value is None or (type(value) is int and value >= 0)


def _valid_finding_counts(value, maximum):
    if type(value) is not tuple or len(value) > len(_FINDING_CODES):
        return False
    previous_rank = -1
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            return False
        code, count = item
        if (
            code not in _FINDING_CODES
            or type(count) is not int
            or not (
                0
                < count
                <= maximum
            )
        ):
            return False
        rank = _FINDING_CODE_RANK[code]
        if rank <= previous_rank:
            return False
        previous_rank = rank
    return True


def _valid_inventory(value, maximum):
    if type(value) is not tuple or len(value) != 2:
        return False
    if any(type(item) is not tuple or len(item) != 2 for item in value):
        return False
    if tuple(item[0] for item in value) != (
        "foreign_key_violation_count",
        "transaction_count",
    ):
        return False
    return all(
        _valid_optional_count(item[1])
        and (item[1] is None or item[1] <= maximum)
        for item in value
    )


def _valid_lifecycle_counts(value, maximum):
    if type(value) is not tuple or len(value) != len(_LIFECYCLES):
        return False
    if any(type(item) is not tuple or len(item) != 2 for item in value):
        return False
    return tuple(item[0] for item in value) == _LIFECYCLES and all(
        type(item[1]) is int and 0 <= item[1] <= maximum
        for item in value
    )


def _operation_budget_projection(contract=None):
    if contract is None:
        contract = GOOGLE_OIDC_RECONCILIATION_BUDGET
    return (
        ("authorizer_calls", contract.max_authorizer_calls),
        ("backup_callbacks", contract.max_backup_callbacks),
        ("output_bytes", contract.max_output_bytes),
        ("result_and_finding_items", contract.max_result_rows),
        ("retained_findings", contract.max_retained_findings),
        ("scan_rows", contract.max_scan_rows),
        ("snapshot_pages", contract.max_snapshot_pages),
        ("sqlite_progress_calls", contract.max_sqlite_progress_calls),
    )


def _valid_operation_budget_contract(value, *, row_scan_limit):
    if type(value) is not tuple or len(value) != 8:
        return False
    if any(type(item) is not tuple or len(item) != 2 for item in value):
        return False
    expected = _operation_budget_projection(
        _REPOSITORY_RECONCILIATION_BUDGET
    )
    if tuple(name for name, _value in value) != tuple(
        name for name, _value in expected
    ):
        return False
    ceilings = dict(expected)
    if any(
        type(limit) is not int
        or limit < 0
        or limit > ceilings[name]
        for name, limit in value
    ):
        return False
    limits = dict(value)
    return (
        limits["scan_rows"] == row_scan_limit
        and limits["scan_rows"] > 0
        and limits["output_bytes"] > 0
    )


def _retained_findings_match_counts(findings, counts):
    retained = Counter(finding.code for finding in findings)
    observed = dict(counts)
    return all(
        count <= observed.get(code, 0)
        for code, count in retained.items()
    )


class _OperationBudgetExceeded(Exception):
    __slots__ = ()


class _OperationBudget:
    __slots__ = (
        "authorizer_calls",
        "backup_callbacks",
        "contract",
        "exhausted",
        "finding_counts",
        "findings_observed",
        "lifecycle_counts",
        "progress_calls",
        "result_rows",
        "rows_inspected",
        "rows_invalid",
        "rows_structurally_valid",
        "sql_parameter_types",
        "snapshot_pages",
        "transaction_rows_observed",
        "transaction_scan_finished",
        "transaction_scan_has_more",
        "transaction_scan_started",
    )

    def __init__(self):
        self.contract = GOOGLE_OIDC_RECONCILIATION_BUDGET
        self.authorizer_calls = 0
        self.backup_callbacks = 0
        self.exhausted = False
        self.finding_counts = Counter()
        self.findings_observed = 0
        self.lifecycle_counts = Counter(
            {name: 0 for name in _LIFECYCLES}
        )
        self.progress_calls = 0
        self.result_rows = 0
        self.rows_inspected = 0
        self.rows_invalid = 0
        self.rows_structurally_valid = 0
        self.sql_parameter_types = _new_private_sql_types()
        self.snapshot_pages = 0
        self.transaction_rows_observed = 0
        self.transaction_scan_finished = False
        self.transaction_scan_has_more = False
        self.transaction_scan_started = False

    def _consume(self, attribute: str, amount: int, maximum: int) -> None:
        if (
            self.exhausted
            or type(amount) is not int
            or amount < 0
            or amount > maximum - getattr(self, attribute)
        ):
            self.exhausted = True
            raise _OperationBudgetExceeded()
        setattr(self, attribute, getattr(self, attribute) + amount)

    def mark_exhausted(self) -> None:
        self.exhausted = True

    def consume_result(self, amount: int = 1) -> None:
        self._consume(
            "result_rows",
            amount,
            self.contract.max_result_rows,
        )

    def remaining_results(self) -> int:
        if self.exhausted:
            return 0
        return self.contract.max_result_rows - self.result_rows

    def consume_authorizer(self) -> None:
        self._consume(
            "authorizer_calls",
            1,
            self.contract.max_authorizer_calls,
        )

    def consume_progress(self) -> None:
        self._consume(
            "progress_calls",
            1,
            self.contract.max_sqlite_progress_calls,
        )

    def consume_backup_callback(self) -> None:
        self._consume(
            "backup_callbacks",
            1,
            self.contract.max_backup_callbacks,
        )

    def consume_snapshot_pages(self, amount: int) -> None:
        self._consume(
            "snapshot_pages",
            amount,
            self.contract.max_snapshot_pages,
        )

    def observe_finding(self, code: str) -> None:
        self.findings_observed += 1
        self.finding_counts[code] += 1

    def start_transaction_scan(self) -> None:
        self.transaction_scan_started = True

    def observe_transaction_row(self) -> None:
        self.transaction_rows_observed += 1

    def finish_transaction_scan(self, *, has_more: bool) -> None:
        self.transaction_scan_finished = True
        self.transaction_scan_has_more = has_more

    def observe_inspected_row(self, *, valid: bool, lifecycle) -> None:
        self.rows_inspected += 1
        if valid:
            self.rows_structurally_valid += 1
        else:
            self.rows_invalid += 1
        if lifecycle in _LIFECYCLES:
            self.lifecycle_counts[lifecycle] += 1


def _new_private_sql_types():
    class PrivateSqlText(str):
        __slots__ = ()

    class PrivateSqlInteger(int):
        __slots__ = ()

    class PrivateSqlReal(float):
        __slots__ = ()

    class PrivateSqlBytes(bytes):
        __slots__ = ()

    return (
        PrivateSqlText,
        PrivateSqlInteger,
        PrivateSqlReal,
        PrivateSqlBytes,
    )


def _private_sql_parameters(parameters, private_types):
    if type(parameters) is tuple:
        return tuple(
            _private_sql_value(value, private_types)
            for value in parameters
        )
    if type(parameters) is list:
        return tuple(
            _private_sql_value(value, private_types)
            for value in parameters
        )
    if type(parameters) is dict:
        if any(type(key) is not str for key in parameters):
            raise TypeError("private_sql_parameters_invalid")
        return {
            key: _private_sql_value(value, private_types)
            for key, value in parameters.items()
        }
    raise TypeError("private_sql_parameters_invalid")


def _private_sql_value(value, private_types):
    private_text, private_integer, private_real, private_bytes = private_types
    if value is None:
        return None
    if type(value) is str:
        return private_text(value)
    if type(value) is int:
        return private_integer(value)
    if type(value) is float:
        return private_real(value)
    if type(value) is bytes:
        return private_bytes(value)
    raise TypeError("private_sql_parameter_invalid")


class _BudgetedCursor:
    __slots__ = ("_budget", "_cursor", "_fetched_on_budget_exhaustion")

    def __init__(self, cursor, budget):
        self._cursor = cursor
        self._budget = budget
        self._fetched_on_budget_exhaustion = False

    @property
    def row_factory(self):
        return self._cursor.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._cursor.row_factory = value

    def execute(self, sql, parameters=()):
        sqlite3.Cursor.execute(
            self._cursor,
            sql,
            _private_sql_parameters(
                parameters,
                self._budget.sql_parameter_types,
            ),
        )
        return self

    def fetchone(self):
        row = sqlite3.Cursor.fetchone(self._cursor)
        if row is not None:
            try:
                self._budget.consume_result()
            except _OperationBudgetExceeded:
                self._fetched_on_budget_exhaustion = True
                raise
        return row

    @property
    def fetched_on_budget_exhaustion(self):
        return self._fetched_on_budget_exhaustion

    def fetchmany(self, size=None):
        requested = self._cursor.arraysize if size is None else size
        if type(requested) is not int or requested < 0:
            requested = 0
        rows = []
        for _index in range(requested):
            row = self.fetchone()
            if row is None:
                break
            rows.append(row)
        return rows

    def fetchall(self):
        rows = []
        while True:
            row = self.fetchone()
            if row is None:
                return rows
            rows.append(row)

    def close(self):
        return sqlite3.Cursor.close(self._cursor)

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


class _BudgetedConnection:
    __slots__ = ("_budget", "_connection")

    def __init__(self, connection, budget):
        self._connection = connection
        self._budget = budget

    def execute(self, sql, parameters=()):
        cursor = _SQLITE_EXECUTE(
            self._connection,
            sql,
            _private_sql_parameters(
                parameters,
                self._budget.sql_parameter_types,
            ),
        )
        return _BudgetedCursor(cursor, self._budget)

    def cursor(self):
        return _BudgetedCursor(
            _SQLITE_CURSOR(self._connection),
            self._budget,
        )

    def getlimit(self, category):
        return sqlite3.Connection.getlimit(self._connection, category)

    def setlimit(self, category, limit):
        return sqlite3.Connection.setlimit(self._connection, category, limit)

    def setconfig(self, operation, value):
        return _SQLITE_SETCONFIG(self._connection, operation, value)


class _Collector:
    __slots__ = ("_budget", "_entries", "counts", "total")

    def __init__(self, budget):
        self._budget = budget
        self.total = 0
        self.counts: Counter[str] = Counter()
        self._entries: list[
            tuple[
                tuple[int, int, int, bytes, int],
                GoogleOidcAuthorizationTransactionFinding,
            ]
        ] = []

    def add(
        self,
        code: str,
        ordinal: int | None = None,
        *,
        private_key: bytes = b"",
    ) -> None:
        finding = GoogleOidcAuthorizationTransactionFinding(code, ordinal)
        self._budget.observe_finding(code)
        self._budget.consume_result()
        self.total += 1
        self.counts[code] += 1
        self._entries.append(
            (
                (
                    _FINDING_CATEGORY[code],
                    0,
                    _FINDING_CODE_RANK[code],
                    private_key,
                    0 if ordinal is None else ordinal,
                ),
                finding,
            )
        )

    def retained(self, limit: int):
        ordered = sorted(self._entries, key=lambda item: item[0])
        return tuple(item[1] for item in ordered[:limit])


def reconcile_google_oidc_authorization_transactions(
    connection,
    *,
    accepted_lookup_key_versions=(1,),
    accepted_protection_key_versions=(1,),
    max_findings: int = DEFAULT_MAX_FINDINGS,
    summary_only: bool = False,
    source_guarantees_no_sidecar_creation: bool = False,
) -> GoogleOidcAuthorizationTransactionReconciliationReport:
    """Inspect a bounded private snapshot without invoking caller callbacks."""

    lookup_versions = _validated_key_versions(accepted_lookup_key_versions)
    protection_versions = _validated_key_versions(
        accepted_protection_key_versions
    )
    if (
        type(connection) is not sqlite3.Connection
        or lookup_versions is None
        or protection_versions is None
        or type(max_findings) is not int
        or not 0 <= max_findings <= MAX_FINDINGS
        or type(summary_only) is not bool
        or type(source_guarantees_no_sidecar_creation) is not bool
    ):
        raise GoogleOidcAuthorizationTransactionReconciliationError(
            "invalid_reconciliation_request"
        )

    displayed_limit = 0 if summary_only else max_findings
    budget = _OperationBudget()
    if not source_guarantees_no_sidecar_creation:
        return _unavailable_report(
            "inspection_boundary_unavailable",
            lookup_versions,
            protection_versions,
            budget=budget,
        )
    owned = None
    report = None
    reason = None
    try:
        initial_transaction = connection.in_transaction
        initial_changes = connection.total_changes
        if initial_transaction:
            reason = "inspection_boundary_unavailable"
        else:
            owned, boundary_reason = _owned_inspection_snapshot(
                connection,
                budget,
            )
            if boundary_reason is not None:
                reason = boundary_reason
            elif (
                connection.in_transaction != initial_transaction
                or connection.total_changes != initial_changes
            ):
                reason = "inspection_boundary_unavailable"
            else:
                _configure_owned_connection(owned, budget)
                bounded = _BudgetedConnection(owned, budget)
                attestation = (
                    attest_google_oidc_authorization_transaction_schema(
                        bounded,
                        _operation_budget=budget,
                    )
                )
                if budget.exhausted:
                    raise _OperationBudgetExceeded()
                if (
                    type(attestation) is not dict
                    or attestation.get("state") != "correctly_installed"
                    or not attestation.get("migration_marker_present")
                ):
                    reason = "schema_capability_unavailable"
                else:
                    report = _scan_snapshot(
                        bounded,
                        budget,
                        lookup_versions,
                        protection_versions,
                        displayed_limit,
                    )
    except _OperationBudgetExceeded:
        report = _incomplete_report(
            "operation_budget_exceeded",
            lookup_versions,
            protection_versions,
            budget=budget,
        )
    except sqlite3.Error as exc:
        if budget.exhausted:
            report = _incomplete_report(
                "operation_budget_exceeded",
                lookup_versions,
                protection_versions,
                budget=budget,
            )
        else:
            reason = _sqlite_reason(
                getattr(exc, "sqlite_errorcode", None)
            )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        reason = "internal_consistency_failure"
    except Exception:
        reason = "internal_consistency_failure"
    finally:
        if owned is not None:
            _close_owned_connection(owned)

    if reason is not None:
        return _unavailable_report(
            reason,
            lookup_versions,
            protection_versions,
            budget=budget,
        )
    if report is None:
        return _unavailable_report(
            "internal_consistency_failure",
            lookup_versions,
            protection_versions,
            budget=budget,
        )
    return report


def _owned_inspection_snapshot(source, budget):
    if source.in_transaction:
        return None, "inspection_boundary_unavailable"
    temp_before = None
    main_copy = None
    temp_after = None
    keep_main = False
    try:
        temp_before = _new_private_connection()
        main_copy = _new_private_connection()
        temp_after = _new_private_connection()
        _bounded_backup(source, temp_before, "temp", budget)
        _bounded_backup(source, main_copy, "main", budget)
        _bounded_backup(source, temp_after, "temp", budget)
        if _SQLITE_SERIALIZE(temp_before) != _SQLITE_SERIALIZE(temp_after):
            return None, "inspection_boundary_unavailable"
        _configure_owned_connection(temp_before, budget)
        temp_view = _BudgetedConnection(temp_before, budget)
        cursor = temp_view.execute(
            "SELECT 1 FROM main.sqlite_schema "
            "WHERE type IN ('table','index','trigger','view') LIMIT 1"
        )
        try:
            if cursor.fetchone() is not None:
                return None, "schema_capability_unavailable"
        finally:
            cursor.close()
        keep_main = True
        return main_copy, None
    except _OperationBudgetExceeded:
        raise
    except sqlite3.Error as exc:
        if budget.exhausted:
            raise _OperationBudgetExceeded() from None
        return None, _sqlite_reason(
            getattr(exc, "sqlite_errorcode", None),
            boundary=True,
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        return None, "internal_consistency_failure"
    except Exception:
        return None, "inspection_boundary_unavailable"
    finally:
        if temp_before is not None:
            _close_owned_connection(temp_before)
        if temp_after is not None:
            _close_owned_connection(temp_after)
        if main_copy is not None and not keep_main:
            _close_owned_connection(main_copy)


def _new_private_connection():
    connection = _SQLITE_CONNECT(":memory:", isolation_level=None)
    if type(connection) is not sqlite3.Connection:
        try:
            connection.close()
        except BaseException:
            pass
        raise RuntimeError("private_sqlite_connection_invalid")
    return connection


def _bounded_backup(source, target, name, budget):
    accounted_total = 0

    def progress(status, remaining, total):
        nonlocal accounted_total
        budget.consume_backup_callback()
        if (
            type(status) is not int
            or type(remaining) is not int
            or type(total) is not int
            or remaining < 0
            or total < 0
            or remaining > total
        ):
            budget.exhausted = True
            raise _OperationBudgetExceeded()
        if total > accounted_total:
            budget.consume_snapshot_pages(total - accounted_total)
            accounted_total = total
        if status not in {
            sqlite3.SQLITE_OK,
            sqlite3.SQLITE_DONE,
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        }:
            raise sqlite3.DatabaseError("private_snapshot_failed")

    _SQLITE_BACKUP(
        source,
        target,
        pages=1,
        progress=progress,
        name=name,
        sleep=0.0,
    )


def _validated_main_table_xinfo_scope_rows(rows) -> frozenset[str]:
    if type(rows) is not list:
        raise sqlite3.DatabaseError("private_table_scope_invalid")
    if len(rows) > _MAX_AUTHORIZER_TABLE_XINFO_SCOPE:
        raise sqlite3.DatabaseError("private_table_scope_exceeded")
    names = set()
    for row in rows:
        if (
            type(row) is not tuple
            or len(row) != 1
            or type(row[0]) is not bytes
        ):
            raise sqlite3.DatabaseError("private_table_scope_invalid")
        try:
            name = row[0].decode("utf-8")
        except UnicodeDecodeError:
            raise sqlite3.DatabaseError(
                "private_table_scope_invalid"
            ) from None
        if not name or name.encode("utf-8") != row[0]:
            raise sqlite3.DatabaseError("private_table_scope_invalid")
        names.add(name)
    if len(names) != len(rows):
        raise sqlite3.DatabaseError("private_table_scope_invalid")
    return frozenset(names)


def _bounded_main_table_xinfo_scope(
    connection,
    budget,
) -> frozenset[str]:
    progress_calls = 0

    def bounded_progress():
        nonlocal progress_calls
        progress_calls += 1
        if progress_calls > _MAX_TABLE_XINFO_SCOPE_PROGRESS_CALLS:
            return 1
        try:
            budget.consume_progress()
        except _OperationBudgetExceeded:
            return 1
        return 0

    cursor = None
    rows = None
    failure = None
    try:
        _SQLITE_SET_PROGRESS_HANDLER(
            connection,
            bounded_progress,
            _TABLE_XINFO_SCOPE_PROGRESS_GRANULARITY,
        )
        cursor = _SQLITE_EXECUTE(
            connection,
            "SELECT CAST(name AS BLOB) FROM main.sqlite_schema "
            "WHERE type = CAST('table' AS TEXT) LIMIT 8193",
        )
        rows = cursor.fetchmany(_TABLE_XINFO_SCOPE_ROW_LIMIT)
    except BaseException as caught:
        failure = caught
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except BaseException as caught:
                if failure is None:
                    failure = caught
        progress_cleared = False
        last_cleanup_failure = None
        progress_clear_operation = _SQLITE_SET_PROGRESS_HANDLER
        progress_clear_arguments = (connection, None, 0)
        for _unused in range(_PROGRESS_HANDLER_CLEAR_PASSES):
            try:
                progress_clear_operation(*progress_clear_arguments)
                progress_cleared = True
            except BaseException as caught:
                last_cleanup_failure = caught
                continue
            break
        if not progress_cleared:
            cleanup_failure = (
                last_cleanup_failure
                if last_cleanup_failure is not None
                else sqlite3.DatabaseError(
                    "private_table_scope_cleanup_failed"
                )
            )
            if failure is None:
                failure = cleanup_failure
            else:
                try:
                    failure.add_note(
                        "Private table-scope cleanup did not complete."
                    )
                except BaseException:
                    pass
            try:
                _SQLITE_CLOSE(connection)
            except BaseException:
                try:
                    failure.add_note(
                        "Private table-scope connection close did not "
                        "complete."
                    )
                except BaseException:
                    pass
    if failure is not None:
        raise failure
    return _validated_main_table_xinfo_scope_rows(rows)


def _configure_owned_connection(connection, budget):
    _SQLITE_EXECUTE(connection, "PRAGMA foreign_keys = ON").close()
    _SQLITE_EXECUTE(connection, "PRAGMA recursive_triggers = OFF").close()
    # SQLite 3.40 rejects exact built-in CHECK constraints such as
    # json_valid() while compiling schema-introspection PRAGMAs with
    # trusted_schema disabled. This connection is a fresh private snapshot
    # with no caller functions or callbacks. Keep trusted-schema processing
    # enabled only for the fixed schema/integrity verification phase, then
    # disable it through sqlite3_db_config before any application-row scan.
    _SQLITE_EXECUTE(connection, "PRAGMA trusted_schema = ON").close()
    _SQLITE_EXECUTE(connection, "PRAGMA temp_store = MEMORY").close()
    _SQLITE_EXECUTE(connection, "PRAGMA query_only = ON").close()
    if hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"):
        try:
            sqlite3.Connection.setconfig(
                connection,
                sqlite3.SQLITE_DBCONFIG_DEFENSIVE,
                True,
            )
        except (AttributeError, sqlite3.NotSupportedError):
            pass
    main_table_xinfo_scope = _bounded_main_table_xinfo_scope(
        connection,
        budget,
    )

    def progress():
        try:
            budget.consume_progress()
        except _OperationBudgetExceeded:
            return 1
        return 0

    def authorize(action, first, second, database, source):
        try:
            budget.consume_authorizer()
        except _OperationBudgetExceeded:
            return sqlite3.SQLITE_DENY
        if type(action) is not int:
            return sqlite3.SQLITE_DENY
        if action in _ALLOWED_AUTHORIZER_ACTIONS:
            return sqlite3.SQLITE_OK
        if action == getattr(sqlite3, "SQLITE_PRAGMA", -1):
            pragma = first.lower() if type(first) is str else ""
            if pragma == "table_xinfo":
                return (
                    sqlite3.SQLITE_OK
                    if (
                        type(first) is str
                        and first == pragma
                        and type(second) is str
                        and (
                            database is None
                            or type(database) is str
                        )
                        and source is None
                        and second in main_table_xinfo_scope
                        and (
                            database == "main"
                            or (
                                database is None
                                and second
                                in _M006_UNQUALIFIED_TABLE_XINFO_ARGUMENTS
                            )
                        )
                    )
                    else sqlite3.SQLITE_DENY
                )
            if pragma == "index_list":
                return (
                    sqlite3.SQLITE_OK
                    if (
                        type(first) is str
                        and first == pragma
                        and type(second) is str
                        and (
                            database is None
                            or type(database) is str
                        )
                        and source is None
                        and is_m006_verification_index_list_pragma(
                            pragma,
                            second,
                            database,
                            source,
                        )
                    )
                    else sqlite3.SQLITE_DENY
                )
            if (
                type(first) is str
                and first == pragma
                and (second is None or type(second) is str)
                and (database is None or type(database) is str)
                and source is None
                and (pragma, second, database, source)
                in _RECONCILIATION_EXACT_PRAGMA_TUPLES
            ):
                return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    _SQLITE_SET_PROGRESS_HANDLER(connection, progress, 1_000)
    _SQLITE_SET_AUTHORIZER(connection, authorize)


def _harden_owned_connection_for_data_scan(connection):
    operation = getattr(
        sqlite3,
        "SQLITE_DBCONFIG_TRUSTED_SCHEMA",
        None,
    )
    if type(operation) is not int:
        raise sqlite3.NotSupportedError(
            "trusted_schema_hardening_unavailable"
        )
    connection.setconfig(operation, False)
    cursor = connection.execute("PRAGMA trusted_schema")
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    if row != (0,):
        raise sqlite3.DatabaseError(
            "trusted_schema_hardening_unavailable"
        )


def _close_owned_connection(connection):
    try:
        _SQLITE_SET_PROGRESS_HANDLER(connection, None, 0)
    except BaseException:
        pass
    try:
        _SQLITE_SET_AUTHORIZER(connection, None)
    except BaseException:
        pass
    try:
        _SQLITE_CLOSE(connection)
    except BaseException:
        pass


def _scan_snapshot(
    connection,
    budget,
    lookup_versions: tuple[int, ...],
    protection_versions: tuple[int, ...],
    displayed_limit: int,
):
    collector = _Collector(budget)
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
    if (
        foreign_keys is None
        or len(foreign_keys) != 1
        or foreign_keys[0] != 1
    ):
        collector.add("foreign_keys_disabled")

    integrity_cursor = connection.execute(
        "PRAGMA main.integrity_check(101)"
    )
    try:
        integrity_rows, integrity_more = _bounded_cursor_rows(
            integrity_cursor,
            100,
        )
    finally:
        integrity_cursor.close()
    if integrity_more or integrity_rows != [("ok",)]:
        collector.add("integrity_check_failed")

    foreign_key_cursor = connection.execute(
        "PRAGMA main.foreign_key_check"
    )
    try:
        foreign_key_rows, foreign_key_more = _bounded_cursor_rows(
            foreign_key_cursor,
            MAX_RECONCILIATION_ROWS,
        )
    finally:
        foreign_key_cursor.close()
    for index, _row in enumerate(foreign_key_rows, start=1):
        collector.add(
            "foreign_key_violation",
            private_key=_canonical_integer(index),
        )

    _harden_owned_connection_for_data_scan(connection)
    budget.start_transaction_scan()
    transaction_cursor = connection.execute(_transaction_scan_sql())
    try:
        raw_rows, row_scan_truncated = _bounded_cursor_rows(
            transaction_cursor,
            MAX_RECONCILIATION_ROWS,
            observe=budget.observe_transaction_row,
        )
    finally:
        transaction_cursor.close()
    budget.finish_transaction_scan(has_more=row_scan_truncated)

    if row_scan_truncated:
        return _build_report(
            collector,
            inventory={
                "foreign_key_violation_count": (
                    len(foreign_key_rows)
                    if not foreign_key_more
                    else None
                ),
                "transaction_count": None,
            },
            lifecycle_counts=Counter(
                {name: 0 for name in _LIFECYCLES}
            ),
            lookup_versions=lookup_versions,
            protection_versions=protection_versions,
            displayed_limit=displayed_limit,
            rows_observed=len(raw_rows),
            rows_inspected=0,
            rows_structurally_valid=0,
            rows_invalid=0,
            row_scan_truncated=True,
            finding_total_exact=False,
            incomplete_reason="row_scan_limit_exceeded",
        )

    inspected_rows = raw_rows[:MAX_RECONCILIATION_ROWS]
    prepared_rows = []
    for raw in inspected_rows:
        try:
            values, storage, lengths, row_key = _prepare_projected_row(raw)
            prepared_rows.append((row_key, values, storage, lengths))
        except _OperationBudgetExceeded:
            raise
        except Exception:
            prepared_rows.append((b"", None, None, None))
    prepared_rows.sort(key=lambda item: item[0])

    lifecycle_counts = Counter({name: 0 for name in _LIFECYCLES})
    structurally_valid = 0
    invalid = 0
    reusable_rows = []
    now = _clock_now()
    for ordinal, (row_key, values, storage, lengths) in enumerate(
        prepared_rows,
        start=1,
    ):
        if values is None:
            budget.observe_inspected_row(valid=False, lifecycle=None)
            invalid += 1
            collector.add(
                "row_read_failure",
                ordinal,
                private_key=row_key,
            )
            continue
        lifecycle = values["lifecycle"]
        if lifecycle in _LIFECYCLES:
            lifecycle_counts[lifecycle] += 1
        valid = _scan_row(
            values,
            storage,
            lengths,
            ordinal,
            row_key,
            lookup_versions,
            protection_versions,
            collector,
            now,
        )
        if valid:
            structurally_valid += 1
        else:
            invalid += 1
        budget.observe_inspected_row(
            valid=valid,
            lifecycle=lifecycle,
        )
        reusable_rows.append(
            (ordinal, row_key, values, storage, lengths)
        )

    _scan_reuse_groups(reusable_rows, collector, budget)
    findings_exact = (
        not row_scan_truncated
        and not integrity_more
        and not foreign_key_more
    )
    return _build_report(
        collector,
        inventory={
            "foreign_key_violation_count": (
                len(foreign_key_rows) if not foreign_key_more else None
            ),
            "transaction_count": (
                len(inspected_rows) if not row_scan_truncated else None
            ),
        },
        lifecycle_counts=lifecycle_counts,
        lookup_versions=lookup_versions,
        protection_versions=protection_versions,
        displayed_limit=displayed_limit,
        rows_observed=len(raw_rows),
        rows_inspected=len(inspected_rows),
        rows_structurally_valid=structurally_valid,
        rows_invalid=invalid,
        row_scan_truncated=row_scan_truncated,
        finding_total_exact=findings_exact,
        incomplete_reason=(
            "row_scan_limit_exceeded"
            if row_scan_truncated
            else (
                "integrity_check_limit_exceeded"
                if integrity_more
                else (
                    "foreign_key_scan_limit_exceeded"
                    if foreign_key_more
                    else None
                )
            )
        ),
    )


def _bounded_cursor_rows(cursor, maximum, *, observe=None):
    rows = []
    for _index in range(maximum + 1):
        try:
            row = cursor.fetchone()
        except _OperationBudgetExceeded:
            if (
                observe is not None
                and getattr(
                    cursor,
                    "fetched_on_budget_exhaustion",
                    False,
                )
            ):
                observe()
            raise
        if row is None:
            return rows, False
        rows.append(tuple(row))
        if observe is not None:
            observe()
    return rows, True


def _transaction_scan_sql():
    values = []
    storage = []
    lengths = []
    for name in _COLUMNS:
        quoted = f'"{name}"'
        cap = _VALUE_CAPS[name]
        values.append(
            "CASE typeof({0}) "
            "WHEN 'text' THEN substr(CAST({0} AS BLOB),1,{1}) "
            "WHEN 'blob' THEN substr({0},1,{1}) "
            "ELSE {0} END".format(quoted, cap)
        )
        storage.append(f"typeof({quoted})")
        lengths.append(f"length(CAST({quoted} AS BLOB))")
    projection = ", ".join((*values, *storage, *lengths))
    return (
        f"SELECT {projection} "
        "FROM google_oidc_authorization_transactions "
        f"LIMIT {MAX_RECONCILIATION_ROWS + 1}"
    )


def _prepare_projected_row(raw):
    column_count = len(_COLUMNS)
    if type(raw) is not tuple or len(raw) != column_count * 3:
        raise ValueError("invalid projected row")
    raw_values = dict(zip(_COLUMNS, raw[:column_count]))
    storage = dict(
        zip(_COLUMNS, raw[column_count : 2 * column_count])
    )
    lengths = dict(zip(_COLUMNS, raw[2 * column_count :]))
    values = {}
    for name in _COLUMNS:
        value = raw_values[name]
        if name in _TEXT_COLUMNS and storage[name] == "text":
            if (
                type(value) is bytes
                and type(lengths[name]) is int
                and lengths[name] == len(value)
                and lengths[name] <= _VALUE_CAPS[name]
            ):
                try:
                    value = value.decode("utf-8", "strict")
                except UnicodeDecodeError:
                    value = None
            else:
                value = None
        values[name] = value
    row_key = _canonical_projected_row(raw_values, storage, lengths)
    return values, storage, lengths, row_key


def _canonical_projected_row(values, storage, lengths):
    parts = [b"wahojobs-google-oidc-reconciliation-row-v1"]
    for name in _COLUMNS:
        parts.append(name.encode("ascii"))
        parts.append(_canonical_scalar(storage[name]))
        parts.append(_canonical_scalar(lengths[name]))
        parts.append(_canonical_scalar(values[name]))
    return b"".join(
        len(part).to_bytes(4, "big") + part for part in parts
    )


def _canonical_scalar(value):
    if value is None:
        return b"n"
    if type(value) is bytes:
        return b"b" + value
    if type(value) is str:
        return b"t" + value.encode("utf-8", "strict")
    if type(value) is int:
        return b"i" + str(value).encode("ascii")
    if type(value) is float:
        return b"r" + value.hex().encode("ascii")
    return b"x" + type(value).__name__.encode("ascii", "replace")


def _canonical_integer(value):
    return str(value).encode("ascii")


def _scan_row(
    row,
    storage,
    lengths,
    ordinal,
    row_key,
    lookup_versions,
    protection_versions,
    collector,
    now,
):
    structurally_valid = True

    def add(code, *, structural=True):
        nonlocal structurally_valid
        collector.add(code, ordinal, private_key=row_key)
        if structural:
            structurally_valid = False

    for name, expected in _EXPECTED_STORAGE.items():
        accepted = (expected,) if type(expected) is str else expected
        if storage.get(name) not in accepted:
            add("invalid_storage_class")
            break

    if (
        type(row["transaction_id"]) is not str
        or _TRANSACTION_ID.fullmatch(row["transaction_id"]) is None
    ):
        add("invalid_transaction_id")
    if type(row["record_version"]) is not int or row["record_version"] != 1:
        add("unsupported_record_version")
    if row["provider"] != "google":
        add("unexpected_provider")
    if (
        type(row["environment_namespace"]) is not str
        or not 1 <= len(row["environment_namespace"]) <= 64
        or row["environment_namespace"]
        not in {"development", "test", "private_beta"}
    ):
        add("invalid_environment_namespace")
    if not _exact_projected_bytes(
        row["configuration_fingerprint"],
        storage["configuration_fingerprint"],
        lengths["configuration_fingerprint"],
        32,
    ):
        add("malformed_configuration_fingerprint")
    if (
        type(row["state_digest_version"]) is not int
        or row["state_digest_version"] != 1
    ):
        add("unsupported_state_digest_version")
    if row["lookup_key_version"] not in lookup_versions:
        add("unknown_lookup_key_version", structural=False)
    if not _exact_projected_bytes(
        row["state_lookup_digest"],
        storage["state_lookup_digest"],
        lengths["state_lookup_digest"],
        32,
    ):
        add("malformed_state_lookup_digest")

    created = _parse_timestamp(row["created_at"])
    expires = _parse_timestamp(row["expires_at"])
    if created is None:
        add("invalid_created_at")
    if expires is None:
        add("invalid_expires_at")
    if (
        created is not None
        and expires is not None
        and expires - created != timedelta(seconds=600)
    ):
        add("invalid_ten_minute_chronology")

    lifecycle = row["lifecycle"]
    row_version = row["row_version"]
    if lifecycle not in _LIFECYCLES:
        add("unsupported_lifecycle")
    expected_row_version = 1 if lifecycle == "prepared" else 2
    if (
        lifecycle not in _LIFECYCLES
        or type(row_version) is not int
        or row_version != expected_row_version
    ):
        add("invalid_lifecycle_row_version")

    claimed = _parse_nullable_timestamp(row["claimed_at"])
    terminal = _parse_nullable_timestamp(row["terminal_at"])
    if row["claimed_at"] is not None and claimed is None:
        add("invalid_claimed_chronology")
    if row["terminal_at"] is not None and terminal is None:
        add("invalid_terminal_chronology")
    if lifecycle == "prepared":
        if row["claimed_at"] is not None or row["terminal_at"] is not None:
            add("contradictory_transaction_row")
        if expires is not None and now >= expires:
            add("prepared_already_expired", structural=False)
    elif lifecycle == "expired":
        if row["claimed_at"] is not None or terminal is None:
            add("terminal_missing_chronology")
        if terminal is not None and expires is not None and terminal < expires:
            add("invalid_terminal_chronology")
    elif lifecycle == "consumed":
        if claimed is None or terminal is None:
            add("terminal_missing_chronology")
        elif claimed != terminal:
            add("invalid_terminal_chronology")
        if claimed is not None and created is not None and claimed < created:
            add("invalid_claimed_chronology")
        if claimed is not None and expires is not None and claimed >= expires:
            add("invalid_claimed_chronology")
    elif lifecycle == "invalidated":
        if row["claimed_at"] is not None or terminal is None:
            add("terminal_missing_chronology")
        if terminal is not None and created is not None and terminal < created:
            add("invalid_terminal_chronology")

    if (
        type(row["protection_envelope_version"]) is not int
        or row["protection_envelope_version"] != 1
    ):
        add("unsupported_protection_envelope_version")
    if row["protection_key_version"] not in protection_versions:
        add("unknown_protection_key_version", structural=False)
    if not _exact_projected_bytes(
        row["protection_nonce"],
        storage["protection_nonce"],
        lengths["protection_nonce"],
        12,
    ):
        add("malformed_protection_nonce")
    material = row["protected_material"]
    if (
        storage["protected_material"] != "blob"
        or type(material) is not bytes
        or type(lengths["protected_material"]) is not int
        or lengths["protected_material"] != len(material)
        or not 17 <= lengths["protected_material"] <= 528
    ):
        add("malformed_protected_material")
    return structurally_valid


def _scan_reuse_groups(rows, collector, budget):
    state_groups = {}
    nonce_groups = {}
    material_groups = {}
    nonce_material_groups = {}
    for _ordinal, row_key, values, storage, lengths in rows:
        state_digest = values["state_lookup_digest"]
        if (
            type(values["lookup_key_version"]) is int
            and storage["state_lookup_digest"] == "blob"
            and type(state_digest) is bytes
            and lengths["state_lookup_digest"] == 32
            and len(state_digest) == 32
        ):
            key = (
                _canonical_scalar(values["lookup_key_version"])
                + _canonical_scalar(state_digest)
            )
            _observe_group(
                state_groups,
                key,
                _reuse_metadata(
                    values,
                    storage,
                    lengths,
                    excluded=frozenset({"state_lookup_digest"}),
                ),
                row_key,
                budget,
            )
        nonce = values["protection_nonce"]
        material = values["protected_material"]
        nonce_valid = (
            storage["protection_nonce"] == "blob"
            and type(nonce) is bytes
            and lengths["protection_nonce"] == 12
            and len(nonce) == 12
        )
        material_valid = (
            storage["protected_material"] == "blob"
            and type(material) is bytes
            and type(lengths["protected_material"]) is int
            and lengths["protected_material"] == len(material)
            and 17 <= len(material) <= 528
        )
        if nonce_valid:
            _observe_group(
                nonce_groups,
                nonce,
                _reuse_metadata(
                    values,
                    storage,
                    lengths,
                    excluded=frozenset({"protection_nonce"}),
                ),
                row_key,
                budget,
            )
        if material_valid:
            _observe_group(
                material_groups,
                material,
                _reuse_metadata(
                    values,
                    storage,
                    lengths,
                    excluded=frozenset({"protected_material"}),
                ),
                row_key,
                budget,
            )
        if nonce_valid and material_valid:
            pair = (
                len(nonce).to_bytes(2, "big")
                + nonce
                + len(material).to_bytes(2, "big")
                + material
            )
            _observe_group(
                nonce_material_groups,
                pair,
                _reuse_metadata(
                    values,
                    storage,
                    lengths,
                    excluded=frozenset(
                        {"protection_nonce", "protected_material"}
                    ),
                ),
                row_key,
                budget,
            )

    _emit_group_findings(
        state_groups,
        collector,
        "duplicate_state_lookup",
    )
    _emit_group_findings(
        nonce_groups,
        collector,
        "duplicate_protection_nonce",
        conflict_code="protection_nonce_metadata_conflict",
    )
    _emit_group_findings(
        material_groups,
        collector,
        "protected_material_reuse",
        conflict_code="protected_material_metadata_conflict",
    )
    _emit_group_findings(
        nonce_material_groups,
        collector,
        "nonce_protected_material_reuse",
    )


def _reuse_metadata(values, storage, lengths, *, excluded):
    parts = [b"wahojobs-google-oidc-reconciliation-reuse-metadata-v1"]
    for name in _COLUMNS:
        if name in excluded:
            continue
        parts.append(name.encode("ascii"))
        parts.append(_canonical_scalar(storage[name]))
        parts.append(_canonical_scalar(lengths[name]))
        parts.append(_canonical_scalar(values[name]))
    for name in sorted(excluded):
        parts.extend(
            (
                f"{name}_storage".encode("ascii"),
                _canonical_scalar(storage[name]),
                f"{name}_length".encode("ascii"),
                _canonical_scalar(lengths[name]),
            )
        )
    return b"".join(
        len(part).to_bytes(4, "big") + part for part in parts
    )


def _observe_group(groups, key, metadata, row_key, budget):
    budget.consume_result()
    state = groups.get(key)
    if state is None:
        groups[key] = [1, metadata, row_key, False]
        return
    state[0] += 1
    if metadata != state[1]:
        state[3] = True
    if row_key < state[2]:
        state[2] = row_key


def _emit_group_findings(groups, collector, code, *, conflict_code=None):
    for key in sorted(groups):
        count, _metadata, first_row_key, conflict = groups[key]
        if count <= 1:
            continue
        private_key = (
            _canonical_scalar(key)
            + _canonical_scalar(first_row_key)
        )
        collector.add(code, private_key=private_key)
        if conflict_code is not None and conflict:
            collector.add(conflict_code, private_key=private_key)


def _build_report(
    collector,
    *,
    inventory,
    lifecycle_counts,
    lookup_versions,
    protection_versions,
    displayed_limit,
    rows_observed,
    rows_inspected,
    rows_structurally_valid,
    rows_invalid,
    row_scan_truncated,
    finding_total_exact,
    incomplete_reason,
):
    retained = collector.retained(displayed_limit)
    retention_truncated = len(retained) < collector.total
    complete = (
        not row_scan_truncated
        and finding_total_exact
        and not retention_truncated
    )
    if complete:
        status = "clean" if collector.total == 0 else "findings"
    else:
        status = "incomplete"
        if incomplete_reason is None:
            incomplete_reason = "finding_retention_limit_exceeded"
    reuse_codes = {
        "duplicate_protection_nonce",
        "protected_material_reuse",
        "protected_material_metadata_conflict",
        "nonce_protected_material_reuse",
        "protection_nonce_metadata_conflict",
    }
    semantic_integrity = (
        "contradictory"
        if any(collector.counts.get(code, 0) for code in reuse_codes)
        else "unverified"
    )
    report = GoogleOidcAuthorizationTransactionReconciliationReport(
        report_version=REPORT_VERSION,
        migration_version=MIGRATION_VERSION,
        status=status,
        complete=complete,
        blocking=status != "clean",
        row_scan_limit=MAX_RECONCILIATION_ROWS,
        rows_observed=rows_observed,
        rows_inspected=rows_inspected,
        rows_structurally_valid=rows_structurally_valid,
        rows_invalid=rows_invalid,
        rows_omitted=None if row_scan_truncated else 0,
        rows_known_remaining=1 if row_scan_truncated else 0,
        row_total_exact=not row_scan_truncated,
        row_scan_truncated=row_scan_truncated,
        total_findings=collector.total if finding_total_exact else None,
        findings_observed=collector.total,
        findings_retained=len(retained),
        findings_omitted=(
            collector.total - len(retained)
            if finding_total_exact
            else None
        ),
        findings_known_omitted=collector.total - len(retained),
        finding_total_exact=finding_total_exact,
        finding_retention_truncated=retention_truncated,
        output_rendering_truncated=False,
        finding_counts_by_code=tuple(
            (code, collector.counts[code])
            for code in _FINDING_CODE_ORDER
            if collector.counts.get(code, 0)
        ),
        inventory=tuple(sorted(inventory.items())),
        lifecycle_counts=tuple(
            (name, lifecycle_counts.get(name, 0))
            for name in _LIFECYCLES
        ),
        accepted_lookup_key_versions=lookup_versions,
        accepted_protection_key_versions=protection_versions,
        operation_budget_contract=_operation_budget_projection(
            collector._budget.contract
        ),
        findings=retained,
        semantic_integrity=semantic_integrity,
        incomplete_reason=incomplete_reason,
    )
    return _apply_output_budget(report)


def _apply_output_budget(report, maximum=None):
    if maximum is None:
        maximum = report.operation_budget["output_bytes"]
    if type(maximum) is not int or maximum < 1:
        raise ValueError("reconciliation_output_budget_invalid")
    if not _report_exceeds_output_budget(report, maximum):
        return report
    fallback = _minimal_output_report(report)
    if _report_exceeds_output_budget(fallback, maximum):
        raise ValueError("reconciliation_output_budget_too_small")
    return fallback


def _report_exceeds_output_budget(report, maximum):
    return (
        len(_render_json_bytes(report)) > maximum
        or len(_render_human_bytes(report)) > maximum
    )


def _minimal_output_report(report):
    return replace(
        report,
        status="incomplete",
        complete=False,
        blocking=True,
        findings_retained=0,
        findings_omitted=(
            report.findings_observed
            if report.finding_total_exact
            else None
        ),
        findings_known_omitted=report.findings_observed,
        finding_retention_truncated=report.findings_observed > 0,
        output_rendering_truncated=True,
        findings=(),
        unavailable_reason=None,
        incomplete_reason="output_byte_limit_exceeded",
    )


def _render_json_bytes(report):
    return (
        json.dumps(
            report.to_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _render_human_bytes(report):
    scalar_lines = (
        ("report_version", report.report_version),
        ("migration_version", report.migration_version),
        ("status", report.status),
        ("complete", _human_value(report.complete)),
        ("blocking", _human_value(report.blocking)),
        ("integrity_scope", report.integrity_scope),
        (
            "cryptographic_authenticity_verified",
            _human_value(report.cryptographic_authenticity_verified),
        ),
        ("semantic_integrity", report.semantic_integrity),
        (
            "runtime_safety_established",
            _human_value(report.runtime_safety_established),
        ),
        ("row_scan_limit", report.row_scan_limit),
        ("rows_observed", report.rows_observed),
        ("rows_inspected", report.rows_inspected),
        ("rows_structurally_valid", report.rows_structurally_valid),
        ("rows_invalid", report.rows_invalid),
        ("rows_omitted", _human_value(report.rows_omitted)),
        ("rows_known_remaining", report.rows_known_remaining),
        ("row_total_exact", _human_value(report.row_total_exact)),
        ("row_scan_truncated", _human_value(report.row_scan_truncated)),
        ("total_findings", _human_value(report.total_findings)),
        ("findings_observed", report.findings_observed),
        ("findings_retained", report.findings_retained),
        ("findings_omitted", _human_value(report.findings_omitted)),
        ("findings_known_omitted", report.findings_known_omitted),
        (
            "finding_total_exact",
            _human_value(report.finding_total_exact),
        ),
        (
            "finding_retention_truncated",
            _human_value(report.finding_retention_truncated),
        ),
        ("findings_truncated", _human_value(report.findings_truncated)),
        (
            "output_rendering_truncated",
            _human_value(report.output_rendering_truncated),
        ),
        *(
            (f"operation_budget.{name}", value)
            for name, value in sorted(report.operation_budget.items())
        ),
    )
    lines = [
        "Google OIDC Authorization Transaction Reconciliation",
        "====================================================",
        *(f"{key}: {value}" for key, value in scalar_lines),
        *(
            f"accepted_lookup_key_version: {value}"
            for value in report.accepted_lookup_key_versions
        ),
        *(
            f"accepted_protection_key_version: {value}"
            for value in report.accepted_protection_key_versions
        ),
        *(f"{key}: {_human_value(value)}" for key, value in report.inventory),
        *(
            f"lifecycle.{name}: {count}"
            for name, count in report.lifecycle_counts
        ),
        *(
            f"finding.{code}: {count}"
            for code, count in report.finding_counts_by_code
        ),
        *(
            (
                f"{finding.severity}: {finding.code}"
                + (
                    f" transaction={finding.transaction_ordinal}"
                    if finding.transaction_ordinal is not None
                    else ""
                )
            )
            for finding in report.findings
        ),
    ]
    if report.unavailable_reason is not None:
        lines.append(f"unavailable_reason: {report.unavailable_reason}")
    if report.incomplete_reason is not None:
        lines.append(f"incomplete_reason: {report.incomplete_reason}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _human_value(value):
    if value is None:
        return "unknown"
    if type(value) is bool:
        return str(value).lower()
    return value


def _unavailable_report(
    reason,
    lookup_versions,
    protection_versions,
    *,
    budget=None,
):
    if reason not in _ERROR_REASONS - {"invalid_reconciliation_request"}:
        reason = "internal_consistency_failure"
    report = GoogleOidcAuthorizationTransactionReconciliationReport(
        report_version=REPORT_VERSION,
        migration_version=MIGRATION_VERSION,
        status="unavailable",
        complete=False,
        blocking=True,
        row_scan_limit=MAX_RECONCILIATION_ROWS,
        rows_observed=0,
        rows_inspected=0,
        rows_structurally_valid=0,
        rows_invalid=0,
        rows_omitted=None,
        rows_known_remaining=0,
        row_total_exact=False,
        row_scan_truncated=False,
        total_findings=None,
        findings_observed=0,
        findings_retained=0,
        findings_omitted=None,
        findings_known_omitted=0,
        finding_total_exact=False,
        finding_retention_truncated=False,
        output_rendering_truncated=False,
        finding_counts_by_code=(),
        inventory=(
            ("foreign_key_violation_count", None),
            ("transaction_count", None),
        ),
        lifecycle_counts=tuple((name, 0) for name in _LIFECYCLES),
        accepted_lookup_key_versions=lookup_versions,
        accepted_protection_key_versions=protection_versions,
        operation_budget_contract=_operation_budget_projection(
            None if budget is None else budget.contract
        ),
        findings=(),
        unavailable_reason=reason,
    )
    return _apply_output_budget(report)


def _incomplete_report(
    reason,
    lookup_versions,
    protection_versions,
    *,
    budget=None,
):
    if reason not in _INCOMPLETE_REASONS:
        reason = "operation_budget_exceeded"
    if budget is None:
        rows_observed = 0
        rows_inspected = 0
        rows_structurally_valid = 0
        rows_invalid = 0
        rows_omitted = None
        rows_known_remaining = 0
        row_total_exact = False
        row_scan_truncated = False
        findings_observed = 0
        finding_counts = ()
        lifecycle_counts = tuple((name, 0) for name in _LIFECYCLES)
        transaction_count = None
        operation_contract = _operation_budget_projection()
    else:
        rows_observed = budget.transaction_rows_observed
        rows_inspected = budget.rows_inspected
        rows_structurally_valid = budget.rows_structurally_valid
        rows_invalid = budget.rows_invalid
        row_total_exact = (
            budget.transaction_scan_finished
            and not budget.transaction_scan_has_more
        )
        if row_total_exact:
            rows_omitted = rows_observed - rows_inspected
            rows_known_remaining = 0
            transaction_count = rows_observed
        else:
            rows_omitted = None
            rows_known_remaining = (
                1
                if (
                    budget.transaction_scan_has_more
                    or rows_observed > MAX_RECONCILIATION_ROWS
                )
                else 0
            )
            transaction_count = None
        row_scan_truncated = (
            budget.transaction_scan_started
            and (
                not row_total_exact
                or rows_inspected < rows_observed
            )
        )
        findings_observed = budget.findings_observed
        finding_counts = tuple(
            (code, budget.finding_counts[code])
            for code in _FINDING_CODE_ORDER
            if budget.finding_counts.get(code, 0)
        )
        lifecycle_counts = tuple(
            (name, budget.lifecycle_counts.get(name, 0))
            for name in _LIFECYCLES
        )
        operation_contract = _operation_budget_projection(
            budget.contract
        )
    reuse_codes = {
        "duplicate_protection_nonce",
        "protected_material_reuse",
        "protected_material_metadata_conflict",
        "nonce_protected_material_reuse",
        "protection_nonce_metadata_conflict",
    }
    semantic_integrity = (
        "contradictory"
        if any(dict(finding_counts).get(code, 0) for code in reuse_codes)
        else "unverified"
    )
    report = GoogleOidcAuthorizationTransactionReconciliationReport(
        report_version=REPORT_VERSION,
        migration_version=MIGRATION_VERSION,
        status="incomplete",
        complete=False,
        blocking=True,
        row_scan_limit=MAX_RECONCILIATION_ROWS,
        rows_observed=rows_observed,
        rows_inspected=rows_inspected,
        rows_structurally_valid=rows_structurally_valid,
        rows_invalid=rows_invalid,
        rows_omitted=rows_omitted,
        rows_known_remaining=rows_known_remaining,
        row_total_exact=row_total_exact,
        row_scan_truncated=row_scan_truncated,
        total_findings=None,
        findings_observed=findings_observed,
        findings_retained=0,
        findings_omitted=None,
        findings_known_omitted=findings_observed,
        finding_total_exact=False,
        finding_retention_truncated=findings_observed > 0,
        output_rendering_truncated=False,
        finding_counts_by_code=finding_counts,
        inventory=(
            ("foreign_key_violation_count", None),
            ("transaction_count", transaction_count),
        ),
        lifecycle_counts=lifecycle_counts,
        accepted_lookup_key_versions=lookup_versions,
        accepted_protection_key_versions=protection_versions,
        operation_budget_contract=operation_contract,
        findings=(),
        semantic_integrity=semantic_integrity,
        incomplete_reason=reason,
    )
    return _apply_output_budget(report)


def _validated_key_versions(value):
    if type(value) not in {tuple, list} or not 1 <= len(value) <= 3:
        return None
    if any(
        type(version) is not int
        or not 1 <= version <= MAX_KEY_VERSION
        for version in value
    ):
        return None
    versions = tuple(sorted(value))
    if len(set(versions)) != len(versions):
        return None
    return versions


def _parse_timestamp(value):
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
        or parsed.microsecond != 0
        or parsed.astimezone(timezone.utc).isoformat() != value
    ):
        return None
    return parsed


def _parse_nullable_timestamp(value):
    return None if value is None else _parse_timestamp(value)


def _exact_projected_bytes(value, storage, length, expected):
    return (
        storage == "blob"
        and type(value) is bytes
        and type(length) is int
        and length == expected
        and len(value) == expected
    )


def _sqlite_reason(error_code, *, boundary=False):
    if type(error_code) is int and (error_code & 0xFF) in _BUSY_CODES:
        return "temporary_contention"
    return (
        "inspection_boundary_unavailable"
        if boundary
        else "internal_consistency_failure"
    )


def _clock_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


__all__ = (
    "DEFAULT_MAX_FINDINGS",
    "INTEGRITY_SCOPE",
    "MAX_FINDINGS",
    "MAX_OUTPUT_BYTES",
    "MAX_RECONCILIATION_ROWS",
    "REPORT_VERSION",
    "GoogleOidcAuthorizationTransactionFinding",
    "GoogleOidcAuthorizationTransactionReconciliationError",
    "GoogleOidcAuthorizationTransactionReconciliationReport",
    "reconcile_google_oidc_authorization_transactions",
)
