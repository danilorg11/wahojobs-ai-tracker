"""Explicit dormant routing gate for controlled public-job identity canaries."""

from __future__ import annotations

from collections.abc import Iterable
import sqlite3

from wahojobs import public_job_identity


MAX_CANARY_PUBLIC_JOB_IDS = 64


class PublicJobCanaryRoutingGate:
    """Allow registry routing only for an explicit set of exact public IDs."""

    __slots__ = ("_public_job_ids",)

    def __init__(self, public_job_ids: Iterable[str] = ()):
        if isinstance(public_job_ids, (str, bytes)) or not isinstance(
            public_job_ids, Iterable
        ):
            raise ValueError("invalid_public_job_canary_gate")
        try:
            values = tuple(public_job_ids)
        except (TypeError, ValueError):
            raise ValueError("invalid_public_job_canary_gate") from None
        if len(values) > MAX_CANARY_PUBLIC_JOB_IDS:
            raise ValueError("invalid_public_job_canary_gate")
        normalized = tuple(
            public_job_identity.require_public_job_id(value) for value in values
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("invalid_public_job_canary_gate")
        self._public_job_ids = frozenset(normalized)

    @classmethod
    def disabled(cls) -> PublicJobCanaryRoutingGate:
        return cls(())

    @property
    def enabled(self) -> bool:
        return bool(self._public_job_ids)

    @property
    def public_job_ids(self) -> frozenset[str]:
        return self._public_job_ids

    def owns_candidate_path(self, path: object) -> bool:
        if not self.enabled:
            return False
        try:
            public_job_identity.validate_public_job_path(path)
        except public_job_identity.InvalidPublicJobIdentity:
            return False
        return True

    def resolve_registered_path(
        self,
        connection: sqlite3.Connection,
        path: object,
    ) -> public_job_identity.PublicJobRouteDecision | None:
        if not self.enabled:
            return None
        decision = public_job_identity.resolve_public_job_path(connection, path)
        return decision if self._allows(decision) else None

    def resolve_canonical(
        self,
        connection: sqlite3.Connection,
        canonical_opportunity_id: object,
    ) -> public_job_identity.PublicJobRouteDecision | None:
        if not self.enabled:
            return None
        decision = public_job_identity.resolve_public_job_canonical(
            connection,
            canonical_opportunity_id,
        )
        return decision if self._allows(decision) else None

    def _allows(self, decision) -> bool:
        return bool(
            type(decision) is public_job_identity.PublicJobRouteDecision
            and decision.kind in {"serve", "redirect", "gone"}
            and decision.public_job_id in self._public_job_ids
            and (
                decision.target_public_job_id is None
                or decision.target_public_job_id in self._public_job_ids
            )
        )

    def __repr__(self) -> str:
        state = "disabled" if not self.enabled else f"{len(self._public_job_ids)} enabled"
        return f"PublicJobCanaryRoutingGate(<{state}>)"


__all__ = [
    "MAX_CANARY_PUBLIC_JOB_IDS",
    "PublicJobCanaryRoutingGate",
]
