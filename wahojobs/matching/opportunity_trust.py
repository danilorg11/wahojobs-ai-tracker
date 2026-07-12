from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from wahojobs.classification import (
    INVENTORY_MODEL_LIVE_FEED,
    MARKET_COUNT_POLICY_COUNT_LIVE,
)
from wahojobs.matching.locations import LOCATION_INCOMPATIBLE


TRUSTED = "trusted"
INACTIVE = "inactive"
STALE_SOURCE = "stale_source"
UNVERIFIED_SOURCE = "unverified_source"
INCOMPATIBLE_LOCATION = "incompatible_location"
NO_COMPATIBLE_LIVE_VARIANT = "no_compatible_live_variant"

LIVE_FEED_MAX_AGE_HOURS = 72
FRESHNESS_MAX_AGE_HOURS_BY_INVENTORY_MODEL = {
    # Live-feed rows represent current availability and require recent verification.
    INVENTORY_MODEL_LIVE_FEED: LIVE_FEED_MAX_AGE_HOURS,
    # Evergreen, public, mixed, and report-separately inventory are intentionally
    # not assigned a live-feed freshness deadline in this product admission layer.
}


@dataclass(frozen=True)
class OpportunityTrustAssessment:
    status: str
    reasons: tuple[str, ...]
    job_is_active: bool
    canonical_is_active: bool | None
    job_last_seen_at: str
    latest_successful_source_run_at: str
    source_age_hours: float | None
    inventory_model: str
    market_count_policy: str
    freshness_max_age_hours: int | None
    source_run_id: int | None
    source_run_qualifies: bool
    selected_variant_id: int | None

    def as_dict(self) -> dict:
        return asdict(self)


def assess_opportunity_trust(
    row: dict,
    location_status: str,
    *,
    now: datetime | None = None,
) -> OpportunityTrustAssessment:
    now = ensure_utc(now or datetime.now(timezone.utc))
    job_is_active = bool(value(row, "job_is_active", True))
    canonical_raw = value(row, "canonical_is_active")
    canonical_is_active = None if canonical_raw is None else bool(canonical_raw)
    job_last_seen_text = clean(value(row, "job_last_seen_at"))
    run_at_text = clean(value(row, "latest_successful_source_run_at"))
    run_started_text = clean(value(row, "source_run_started_at"))
    source_run_id = integer_or_none(value(row, "source_run_id"))
    source_run_qualifies = bool(value(row, "source_run_qualifies", source_run_id is not None))
    inventory_model = clean(value(row, "inventory_model"))
    market_count_policy = clean(value(row, "market_count_policy"))
    source_age_hours = age_hours(run_at_text, now)
    reasons = []

    if not job_is_active:
        return assessment(
            INACTIVE,
            ["The stored job variant is inactive."],
            row,
            job_is_active,
            canonical_is_active,
            source_age_hours,
            source_run_id,
            source_run_qualifies,
        )
    if canonical_is_active is False:
        return assessment(
            INACTIVE,
            ["The canonical opportunity is inactive."],
            row,
            job_is_active,
            canonical_is_active,
            source_age_hours,
            source_run_id,
            source_run_qualifies,
        )
    if location_status == LOCATION_INCOMPATIBLE:
        return assessment(
            INCOMPATIBLE_LOCATION,
            ["The stored opportunity location is incompatible with the profile location."],
            row,
            job_is_active,
            canonical_is_active,
            source_age_hours,
            source_run_id,
            source_run_qualifies,
        )

    max_age_hours = freshness_max_age_hours(inventory_model, market_count_policy)
    if max_age_hours is not None:
        if not source_run_qualifies or source_run_id is None or not run_at_text:
            reasons.append("No qualifying successful non-sample source run is available.")
            return assessment(
                UNVERIFIED_SOURCE,
                reasons,
                row,
                job_is_active,
                canonical_is_active,
                source_age_hours,
                source_run_id,
                source_run_qualifies,
            )
        if source_age_hours is None:
            reasons.append("The latest qualifying source-run time could not be parsed.")
            return assessment(
                UNVERIFIED_SOURCE,
                reasons,
                row,
                job_is_active,
                canonical_is_active,
                source_age_hours,
                source_run_id,
                source_run_qualifies,
            )
        if source_age_hours > max_age_hours:
            reasons.append(
                f"The latest qualifying source verification is older than {max_age_hours} hours."
            )
            return assessment(
                STALE_SOURCE,
                reasons,
                row,
                job_is_active,
                canonical_is_active,
                source_age_hours,
                source_run_id,
                source_run_qualifies,
            )

        job_last_seen = parse_utc(job_last_seen_text)
        source_run_started = parse_utc(run_started_text or run_at_text)
        if job_last_seen is None or source_run_started is None:
            reasons.append("The job could not be tied to the latest qualifying source snapshot.")
            return assessment(
                UNVERIFIED_SOURCE,
                reasons,
                row,
                job_is_active,
                canonical_is_active,
                source_age_hours,
                source_run_id,
                source_run_qualifies,
            )
        if job_last_seen < source_run_started:
            reasons.append("The job was not observed in the latest qualifying source snapshot.")
            return assessment(
                UNVERIFIED_SOURCE,
                reasons,
                row,
                job_is_active,
                canonical_is_active,
                source_age_hours,
                source_run_id,
                source_run_qualifies,
            )

    return assessment(
        TRUSTED,
        [],
        row,
        job_is_active,
        canonical_is_active,
        source_age_hours,
        source_run_id,
        source_run_qualifies,
        selected_variant_id=integer_or_none(value(row, "job_id")),
    )


def freshness_max_age_hours(inventory_model: str, market_count_policy: str) -> int | None:
    if market_count_policy != MARKET_COUNT_POLICY_COUNT_LIVE:
        return None
    return FRESHNESS_MAX_AGE_HOURS_BY_INVENTORY_MODEL.get(inventory_model)


def assessment(
    status,
    reasons,
    row,
    job_is_active,
    canonical_is_active,
    source_age_hours,
    source_run_id,
    source_run_qualifies,
    selected_variant_id=None,
):
    return OpportunityTrustAssessment(
        status=status,
        reasons=tuple(reasons),
        job_is_active=job_is_active,
        canonical_is_active=canonical_is_active,
        job_last_seen_at=clean(value(row, "job_last_seen_at")),
        latest_successful_source_run_at=clean(value(row, "latest_successful_source_run_at")),
        source_age_hours=round(source_age_hours, 2) if source_age_hours is not None else None,
        inventory_model=clean(value(row, "inventory_model")),
        market_count_policy=clean(value(row, "market_count_policy")),
        freshness_max_age_hours=freshness_max_age_hours(
            clean(value(row, "inventory_model")),
            clean(value(row, "market_count_policy")),
        ),
        source_run_id=source_run_id,
        source_run_qualifies=source_run_qualifies,
        selected_variant_id=selected_variant_id,
    )


def age_hours(value_text: str, now: datetime) -> float | None:
    parsed = parse_utc(value_text)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 3600)


def parse_utc(value_text: str) -> datetime | None:
    if not value_text:
        return None
    try:
        parsed = datetime.fromisoformat(value_text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ensure_utc(parsed)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def value(row, key, default=None):
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def integer_or_none(raw):
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def clean(raw) -> str:
    return str(raw or "").strip()
