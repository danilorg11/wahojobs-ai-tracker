"""Authenticated durable finalization boundary for AI profile intake.

The browser never receives the authority objects in this module.  Every
database is supplied by the composed runtime, M011 is only attested (never
installed), and the Slice 4A service remains the sole mutation authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
import sqlite3

from wahojobs.ai_profile_import import (
    AIProfileImportError,
    AIProfileIntakeCheckpointAuthority,
    AIProfileImportReservationAuthority,
    AIProfileImportService,
    AIProfileImportSourceMetadata,
    AI_PROFILE_IMPORT_RESERVATION_GENERATION_MAX,
    ConfirmedAIProfileImport,
    prepare_confirmed_ai_profile_import,
)
from wahojobs.profile_intake.contracts import ProfileIntakeError
from wahojobs.profile_intake.runtime import (
    EditableProfileReview,
    PROFILE_INTAKE_DEFAULT_REVIEW_STEP,
    SafeDocumentBundleMetadata,
    SafeModelDiagnostics,
    TrustedProfileIntakeGrant,
)


MIN_SAFE_REVIEW_SECONDS = 60
RESERVATION_SAFETY_MARGIN_SECONDS = 30
_BOUND_ISSUER = object()


@dataclass(frozen=True, slots=True, repr=False, init=False)
class BoundAIProfileImportAuthority:
    """Server-only reservation and safe provenance held by one vault record."""

    lease_expires_at: str
    checkpoint_expires_at: str
    checkpoint_version: int
    _checkpoint: AIProfileIntakeCheckpointAuthority = field(repr=False)
    _reservation: AIProfileImportReservationAuthority = field(repr=False)
    _source_metadata: AIProfileImportSourceMetadata = field(repr=False)
    _issuer: object = field(repr=False, compare=False)

    def __init__(self, *_args, **_kwargs):
        raise ProfileIntakeError("invalid_durable_intake_authority")

    @classmethod
    def _issue(cls, reservation, source_metadata, checkpoint):
        if (
            type(reservation) is not AIProfileImportReservationAuthority
            or type(source_metadata) is not AIProfileImportSourceMetadata
            or type(checkpoint) is not AIProfileIntakeCheckpointAuthority
        ):
            raise ProfileIntakeError("invalid_durable_intake_authority")
        instance = object.__new__(cls)
        object.__setattr__(instance, "lease_expires_at", reservation.lease_expires_at)
        object.__setattr__(instance, "checkpoint_expires_at", checkpoint.expires_at)
        object.__setattr__(instance, "checkpoint_version", checkpoint.row_version)
        object.__setattr__(instance, "_checkpoint", checkpoint)
        object.__setattr__(instance, "_reservation", reservation)
        object.__setattr__(instance, "_source_metadata", source_metadata)
        object.__setattr__(instance, "_issuer", _BOUND_ISSUER)
        return instance

    def reservation_for_service(self):
        if getattr(self, "_issuer", None) is not _BOUND_ISSUER:
            raise ProfileIntakeError("invalid_durable_intake_authority")
        return self._reservation

    def source_metadata_for_service(self):
        if getattr(self, "_issuer", None) is not _BOUND_ISSUER:
            raise ProfileIntakeError("invalid_durable_intake_authority")
        return self._source_metadata

    def checkpoint_for_service(self):
        if getattr(self, "_issuer", None) is not _BOUND_ISSUER:
            raise ProfileIntakeError("invalid_durable_intake_authority")
        return self._checkpoint

    def __repr__(self):
        return "BoundAIProfileImportAuthority(<redacted>)"

    def __reduce_ex__(self, _protocol):
        raise TypeError("bound_ai_profile_import_authority_not_serializable")


class ProfileIntakeFinalizationService:
    """Use Slice 4A through explicit read-only and writable connection scopes."""

    __slots__ = (
        "_clock",
        "_read_connection_provider",
        "_service",
        "_write_connection_provider",
    )

    def __init__(
        self,
        *,
        read_connection_provider,
        write_connection_provider,
        clock,
        import_service=None,
    ):
        service = import_service or AIProfileImportService()
        if (
            not callable(read_connection_provider)
            or not callable(write_connection_provider)
            or not callable(clock)
            or type(service) is not AIProfileImportService
        ):
            raise ValueError("invalid_profile_intake_finalization_configuration")
        self._read_connection_provider = read_connection_provider
        self._write_connection_provider = write_connection_provider
        self._clock = clock
        self._service = service

    def preflight(self, grant):
        _require_grant(grant)
        now = _clock(self._clock)
        try:
            with self._read_connection_provider() as connection:
                result = self._service.preflight(connection, grant, now=now)
            return result.state
        except AIProfileImportError as exc:
            raise ProfileIntakeError(_browser_code(exc.code)) from None
        except (sqlite3.Error, TypeError, ValueError):
            raise ProfileIntakeError("ai_import_schema_unavailable") from None

    def checkpoint_summary(self, grant):
        _require_grant(grant)
        try:
            with self._read_connection_provider() as connection:
                return self._service.inspect_checkpoint(
                    connection, grant, now=_clock(self._clock)
                )
        except AIProfileImportError as exc:
            raise ProfileIntakeError(_browser_code(exc.code)) from None
        except (sqlite3.Error, TypeError, ValueError):
            raise ProfileIntakeError("ai_import_schema_unavailable") from None

    def reserve(self, grant, review, document, diagnostics):
        _require_grant(grant)
        if (
            type(review) is not EditableProfileReview
            or type(document) is not SafeDocumentBundleMetadata
            or diagnostics is not None
            and (
                type(diagnostics) is not tuple
                or any(type(item) is not SafeModelDiagnostics for item in diagnostics)
            )
        ):
            raise ProfileIntakeError("invalid_durable_intake_authority")
        try:
            metadata = AIProfileImportSourceMetadata.from_runtime(document, diagnostics)
            now = _clock(self._clock)
            with self._write_connection_provider() as connection:
                result = self._service.create_checkpoint(
                    connection,
                    grant,
                    review,
                    metadata,
                    now=now,
                )
            bound = BoundAIProfileImportAuthority._issue(
                result.reservation,
                result.source_metadata,
                result.checkpoint,
            )
            lease = datetime.fromisoformat(bound.lease_expires_at)
            lease_seconds = (lease - now).total_seconds()
            review_seconds = (
                AI_PROFILE_IMPORT_RESERVATION_GENERATION_MAX.total_seconds()
                - RESERVATION_SAFETY_MARGIN_SECONDS
            )
            if not math.isfinite(lease_seconds) or lease_seconds < MIN_SAFE_REVIEW_SECONDS:
                self.discard_checkpoint(grant, bound)
                raise ProfileIntakeError("ai_import_lease_unavailable")
            return bound, review_seconds
        except ProfileIntakeError:
            raise
        except AIProfileImportError as exc:
            raise ProfileIntakeError(_browser_code(exc.code)) from None
        except (sqlite3.Error, TypeError, ValueError, OverflowError):
            raise ProfileIntakeError("durable_intake_unavailable") from None

    def release(self, grant, bound, *, outcome_code):
        _require_bound(grant, bound)
        try:
            with self._write_connection_provider() as connection:
                return self._service.release(
                    connection,
                    grant,
                    bound.reservation_for_service(),
                    outcome_code=outcome_code,
                    now=_clock(self._clock),
                )
        except AIProfileImportError as exc:
            raise ProfileIntakeError(_browser_code(exc.code)) from None
        except (sqlite3.Error, TypeError, ValueError):
            raise ProfileIntakeError("durable_intake_unavailable") from None

    def renew(self, grant, bound):
        _require_bound(grant, bound)
        try:
            with self._write_connection_provider() as connection:
                result = self._service.renew_checkpoint_reservation(
                    connection,
                    grant,
                    bound.checkpoint_for_service(),
                    bound.reservation_for_service(),
                    now=_clock(self._clock),
                )
            return BoundAIProfileImportAuthority._issue(
                result.authority,
                bound.source_metadata_for_service(),
                bound.checkpoint_for_service(),
            )
        except AIProfileImportError as exc:
            raise ProfileIntakeError(_browser_code(exc.code)) from None
        except (sqlite3.Error, TypeError, ValueError):
            raise ProfileIntakeError("durable_intake_unavailable") from None

    def resume(self, grant, checkpoint_id, *, expected_version):
        _require_grant(grant)
        try:
            with self._write_connection_provider() as connection:
                result = self._service.resume_checkpoint(
                    connection,
                    grant,
                    checkpoint_id,
                    expected_version=expected_version,
                    now=_clock(self._clock),
                )
            bound = BoundAIProfileImportAuthority._issue(
                result.reservation,
                result.source_metadata,
                result.checkpoint,
            )
            review_seconds = (
                AI_PROFILE_IMPORT_RESERVATION_GENERATION_MAX.total_seconds()
                - RESERVATION_SAFETY_MARGIN_SECONDS
            )
            return result.review, bound, review_seconds, result.review_step
        except AIProfileImportError as exc:
            raise ProfileIntakeError(_browser_code(exc.code)) from None
        except (sqlite3.Error, TypeError, ValueError):
            raise ProfileIntakeError("durable_intake_unavailable") from None

    def save_checkpoint(
        self,
        grant,
        bound,
        review,
        *,
        review_step=PROFILE_INTAKE_DEFAULT_REVIEW_STEP,
    ):
        _require_bound(grant, bound)
        if type(review) is not EditableProfileReview:
            raise ProfileIntakeError("invalid_review_submission")
        try:
            with self._write_connection_provider() as connection:
                checkpoint = self._service.update_checkpoint(
                    connection,
                    grant,
                    bound.checkpoint_for_service(),
                    review,
                    review_step=review_step,
                    now=_clock(self._clock),
                )
            return BoundAIProfileImportAuthority._issue(
                bound.reservation_for_service(),
                bound.source_metadata_for_service(),
                checkpoint,
            )
        except AIProfileImportError as exc:
            raise ProfileIntakeError(_browser_code(exc.code)) from None
        except (sqlite3.Error, TypeError, ValueError):
            raise ProfileIntakeError("durable_intake_unavailable") from None

    def discard_checkpoint(self, grant, bound):
        _require_bound(grant, bound)
        try:
            with self._write_connection_provider() as connection:
                return self._service.discard_checkpoint(
                    connection,
                    grant,
                    bound.checkpoint_for_service(),
                    now=_clock(self._clock),
                )
        except AIProfileImportError as exc:
            raise ProfileIntakeError(_browser_code(exc.code)) from None
        except (sqlite3.Error, TypeError, ValueError):
            raise ProfileIntakeError("durable_intake_unavailable") from None

    def discard_saved_checkpoint(self, grant, checkpoint):
        _require_grant(grant)
        if type(checkpoint) is not AIProfileIntakeCheckpointAuthority:
            raise ProfileIntakeError("invalid_durable_intake_authority")
        try:
            with self._write_connection_provider() as connection:
                return self._service.discard_checkpoint(
                    connection,
                    grant,
                    checkpoint,
                    now=_clock(self._clock),
                )
        except AIProfileImportError as exc:
            raise ProfileIntakeError(_browser_code(exc.code)) from None
        except (sqlite3.Error, TypeError, ValueError):
            raise ProfileIntakeError("durable_intake_unavailable") from None

    def prepare(self, review, bound):
        if type(review) is not EditableProfileReview:
            raise ProfileIntakeError("invalid_review_submission")
        _require_bound(None, bound)
        try:
            return prepare_confirmed_ai_profile_import(
                review,
                bound.source_metadata_for_service(),
            )
        except AIProfileImportError as exc:
            raise ProfileIntakeError(_browser_code(exc.code)) from None

    def commit(self, grant, bound, confirmed):
        _require_bound(grant, bound)
        if type(confirmed) is not ConfirmedAIProfileImport:
            raise ProfileIntakeError("invalid_review_submission")
        try:
            with self._write_connection_provider() as connection:
                return self._service.commit_confirmed_ai_profile_import(
                    connection,
                    grant,
                    bound.reservation_for_service(),
                    confirmed,
                    checkpoint=bound.checkpoint_for_service(),
                    now=_clock(self._clock),
                )
        except AIProfileImportError as exc:
            raise ProfileIntakeError(_browser_code(exc.code)) from None
        except (sqlite3.Error, TypeError, ValueError):
            raise ProfileIntakeError("durable_intake_unavailable") from None


def _require_grant(grant):
    if type(grant) is not TrustedProfileIntakeGrant:
        raise ProfileIntakeError("ownership_stale")


def _require_bound(grant, bound):
    if type(bound) is not BoundAIProfileImportAuthority or getattr(
        bound, "_issuer", None
    ) is not _BOUND_ISSUER:
        raise ProfileIntakeError("invalid_durable_intake_authority")
    if grant is not None:
        _require_grant(grant)


def _clock(callback):
    value = callback()
    if type(value) is not datetime or value.tzinfo is None:
        raise ProfileIntakeError("durable_intake_unavailable")
    return value


def _browser_code(code):
    return {
        "schema_unavailable": "ai_import_schema_unavailable",
        "entitlement_reserved": "ai_import_entitlement_reserved",
        "entitlement_consumed": "ai_import_entitlement_consumed",
        "profile_already_exists": "ai_import_profile_exists",
        "reservation_expired": "ai_import_reservation_expired",
        "reservation_mismatch": "ai_import_reservation_mismatch",
        "ownership_stale": "ai_import_ownership_stale",
        "review_unresolved": "ai_import_review_unresolved",
        "idempotency_conflict": "ai_import_idempotency_conflict",
        "temporary_contention": "ai_import_temporary_contention",
        "attempt_released": "ai_import_reservation_expired",
        "attempt_failed": "ai_import_reservation_expired",
        "content_rejected": "ai_import_review_invalid",
        "checkpoint_exists": "ai_import_checkpoint_available",
        "checkpoint_expired": "ai_import_checkpoint_expired",
        "checkpoint_missing": "ai_import_checkpoint_expired",
        "checkpoint_stale": "stale_review",
        "checkpoint_tampered": "ai_import_review_invalid",
    }.get(code, "durable_intake_unavailable")


__all__ = (
    "BoundAIProfileImportAuthority",
    "MIN_SAFE_REVIEW_SECONDS",
    "ProfileIntakeFinalizationService",
    "RESERVATION_SAFETY_MARGIN_SECONDS",
)
