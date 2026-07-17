from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlparse

from wahojobs.crawler.providers.greenhouse import GreenhouseBoardConfig
from wahojobs.matching.languages import CANONICAL_LANGUAGES
from wahojobs.matching.taxonomy import OCCUPATIONAL_FAMILIES
from wahojobs.profiles.countries import CANONICAL_COUNTRIES


REGISTRY_PATH = Path(__file__).with_name("source_registry.json")
REGISTRY_VERSION = 2
ATS_PROVIDERS = frozenset({"greenhouse"})
SOURCE_FAMILIES = frozenset({"public_ats"})
PRIORITY_TIERS = frozenset({"control", "pilot", "experimental", "deferred"})
TERMS_REVIEW_STATUSES = frozenset(
    {
        "greenhouse_public_surface_verified_company_terms_pending",
        "further_review_required",
        "approved",
        "rejected",
    }
)
ACCEPTANCE_REVIEW_STATUSES = frozenset({"pending", "approved", "rejected"})
VALIDATION_STATUSES = frozenset({"pending", "passed", "failed"})
SUPPORTED_PARSER_VERSIONS = frozenset(
    {"greenhouse-job-board-v1", "greenhouse-legacy"}
)
READINESS_OUTCOMES = frozenset(
    {"complete", "partial", "anomalous", "failed", "contract_invalid"}
)
TARGET_COUNTRY_TOKENS = frozenset(
    {
        "global",
        "country_specific_remote",
        "Americas",
        "AMER",
        "EMEA",
        "APAC",
        "United_States",
    }
)
TARGET_LANGUAGE_TOKENS = frozenset({"multilingual", "unspecified"})
GREENHOUSE_HOSTS = frozenset(
    {"job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"}
)
ENTRY_FIELDS = {
    "registry_id",
    "company_id",
    "company_name",
    "source_family",
    "ats_provider",
    "board_identifier",
    "careers_url",
    "allowed_job_hosts",
    "connector_enabled_for_dry_run",
    "product_enabled",
    "production_crawl_enabled",
    "priority_tier",
    "target_families",
    "target_countries",
    "target_languages",
    "crawl_cadence_hours",
    "freshness_sla_hours",
    "parser_version",
    "terms_review_status",
    "acceptance_review_status",
    "readiness_observations",
    "count_drop_policy",
    "temporary_closure_status",
    "persona_coverage_status",
    "notes",
    "root_department_id",
}
REQUIRED_ENTRY_FIELDS = ENTRY_FIELDS - {"root_department_id"}
READINESS_FIELDS = {
    "run_id",
    "observed_at",
    "outcome",
    "parser_version",
    "accepted_record_count",
}
COUNT_DROP_POLICY_FIELDS = {
    "minimum_previous_count",
    "minimum_retained_fraction",
}
IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
HOST_PATTERN = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))+$"
)
MANAGED_SOURCE_ALIASES = {
    "invisibletech": "invisible",
    "invisible-technologies": "invisible",
    "invisible_technologies": "invisible",
}


@dataclass(frozen=True)
class CountDropPolicy:
    minimum_previous_count: int
    minimum_retained_fraction: float


@dataclass(frozen=True)
class ReadinessObservation:
    run_id: str
    observed_at: str
    outcome: str
    parser_version: str
    accepted_record_count: int


@dataclass(frozen=True)
class SourceRegistryEntry:
    registry_id: str
    company_id: str
    company_name: str
    source_family: str
    ats_provider: str
    board_identifier: str
    careers_url: str
    allowed_job_hosts: tuple[str, ...]
    connector_enabled_for_dry_run: bool
    product_enabled: bool
    production_crawl_enabled: bool
    priority_tier: str
    target_families: tuple[str, ...]
    target_countries: tuple[str, ...]
    target_languages: tuple[str, ...]
    crawl_cadence_hours: int
    freshness_sla_hours: int
    parser_version: str
    terms_review_status: str
    acceptance_review_status: str
    readiness_observations: tuple[ReadinessObservation, ...]
    count_drop_policy: CountDropPolicy
    temporary_closure_status: str
    persona_coverage_status: str
    notes: str
    root_department_id: int | None = None

    @property
    def is_pilot(self):
        return self.priority_tier == "pilot"

    @property
    def terms_approved(self):
        return self.terms_review_status == "approved"

    @property
    def consecutive_complete_snapshots(self):
        return readiness_streak(self)

    @property
    def last_accepted_complete_count(self):
        for observation in reversed(self.readiness_observations):
            if (
                observation.outcome == "complete"
                and observation.parser_version == self.parser_version
            ):
                return observation.accepted_record_count
        return None

    def greenhouse_config(self):
        if self.ats_provider != "greenhouse":
            raise ValueError(f"{self.registry_id} is not a Greenhouse source.")
        if not self.connector_enabled_for_dry_run:
            raise PermissionError(
                f"{self.registry_id} is not enabled for connector dry runs."
            )
        return GreenhouseBoardConfig(
            source_name=self.company_name,
            company_id=self.company_id,
            board_token=self.board_identifier,
            allowed_job_hosts=self.allowed_job_hosts,
            root_department_id=self.root_department_id,
        )


def load_source_registry(path=REGISTRY_PATH):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if type(payload) is not dict or set(payload) != {"registry_version", "sources"}:
        raise ValueError("Source registry must contain only registry_version and sources.")
    if type(payload["registry_version"]) is not int:
        raise ValueError("Source registry registry_version must be an integer.")
    if payload["registry_version"] != REGISTRY_VERSION:
        raise ValueError(
            f"Unsupported source registry version: {payload['registry_version']!r}."
        )
    if type(payload["sources"]) is not list or not payload["sources"]:
        raise ValueError("Source registry sources must be a non-empty list.")

    entries = tuple(
        parse_registry_entry(raw, index)
        for index, raw in enumerate(payload["sources"])
    )
    require_unique(entries, "registry_id")
    require_unique(entries, "company_id")
    require_unique(entries, "board_identifier")
    identities = [
        (entry.ats_provider, entry.company_id, entry.board_identifier)
        for entry in entries
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("Source registry contains a duplicate company/provider/board identity.")
    return entries


def parse_registry_entry(raw, index):
    if type(raw) is not dict:
        raise ValueError(f"Registry source {index} must be an object.")
    missing = REQUIRED_ENTRY_FIELDS - set(raw)
    unknown = set(raw) - ENTRY_FIELDS
    if missing:
        raise ValueError(f"Registry source {index} is missing fields: {sorted(missing)}.")
    if unknown:
        raise ValueError(f"Registry source {index} has unknown fields: {sorted(unknown)}.")

    for field in ("registry_id", "company_id", "board_identifier"):
        value = raw[field]
        if type(value) is not str or not IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError(f"Registry source {index} has invalid {field}: {value!r}.")
    for field in ("company_name", "notes"):
        require_nonempty_string(raw[field], index, field)
    require_enum(raw["ats_provider"], ATS_PROVIDERS, index, "ats_provider")
    require_enum(raw["source_family"], SOURCE_FAMILIES, index, "source_family")
    require_enum(raw["priority_tier"], PRIORITY_TIERS, index, "priority_tier")
    require_enum(raw["parser_version"], SUPPORTED_PARSER_VERSIONS, index, "parser_version")
    require_enum(raw["terms_review_status"], TERMS_REVIEW_STATUSES, index, "terms_review_status")
    require_enum(
        raw["acceptance_review_status"],
        ACCEPTANCE_REVIEW_STATUSES,
        index,
        "acceptance_review_status",
    )
    require_enum(
        raw["temporary_closure_status"],
        VALIDATION_STATUSES,
        index,
        "temporary_closure_status",
    )
    require_enum(
        raw["persona_coverage_status"],
        VALIDATION_STATUSES,
        index,
        "persona_coverage_status",
    )
    for field in (
        "connector_enabled_for_dry_run",
        "product_enabled",
        "production_crawl_enabled",
    ):
        if type(raw[field]) is not bool:
            raise ValueError(f"Registry source {index} field {field} must be boolean.")
    for field in ("crawl_cadence_hours", "freshness_sla_hours"):
        require_positive_integer(raw[field], index, field)

    target_families = validate_closed_string_list(
        raw["target_families"], index, "target_families", OCCUPATIONAL_FAMILIES
    )
    target_countries = validate_closed_string_list(
        raw["target_countries"],
        index,
        "target_countries",
        CANONICAL_COUNTRIES | TARGET_COUNTRY_TOKENS,
        allow_empty=True,
    )
    target_languages = validate_closed_string_list(
        raw["target_languages"],
        index,
        "target_languages",
        CANONICAL_LANGUAGES | TARGET_LANGUAGE_TOKENS,
        allow_empty=True,
    )
    allowed_hosts = validate_allowed_hosts(raw["allowed_job_hosts"], index)
    observations = validate_readiness_observations(
        raw["readiness_observations"], index
    )
    count_drop_policy = validate_count_drop_policy(raw["count_drop_policy"], index)

    root_department_id = raw.get("root_department_id")
    if root_department_id is not None:
        require_positive_integer(root_department_id, index, "root_department_id")
    validate_careers_url(
        raw["careers_url"], raw["board_identifier"], allowed_hosts, index
    )

    entry = SourceRegistryEntry(
        **{
            **raw,
            "allowed_job_hosts": allowed_hosts,
            "target_families": target_families,
            "target_countries": target_countries,
            "target_languages": target_languages,
            "readiness_observations": observations,
            "count_drop_policy": count_drop_policy,
        }
    )
    validate_enablement_boundary(entry)
    return entry


def validate_enablement_boundary(entry):
    if entry.product_enabled and not entry.connector_enabled_for_dry_run:
        raise ValueError(
            f"{entry.registry_id} cannot enable product inventory without connector validation."
        )
    if entry.production_crawl_enabled and not entry.product_enabled:
        raise ValueError(
            f"{entry.registry_id} cannot crawl production while product inventory is disabled."
        )
    if entry.product_enabled or entry.production_crawl_enabled:
        if not entry.terms_approved:
            raise ValueError(
                f"{entry.registry_id} cannot be production-enabled before terms approval."
            )
        if entry.acceptance_review_status != "approved":
            raise ValueError(
                f"{entry.registry_id} cannot be production-enabled before acceptance approval."
            )
        if entry.consecutive_complete_snapshots < 3:
            raise ValueError(
                f"{entry.registry_id} cannot be production-enabled before a valid three-snapshot history."
            )
        if entry.temporary_closure_status != "passed":
            raise ValueError(
                f"{entry.registry_id} cannot be production-enabled before closure validation."
            )
        if entry.persona_coverage_status != "passed":
            raise ValueError(
                f"{entry.registry_id} cannot be production-enabled before persona validation."
            )
    if entry.is_pilot and (entry.product_enabled or entry.production_crawl_enabled):
        raise ValueError(f"Pilot source {entry.registry_id} must remain non-production-enabled.")
    if entry.company_id == "invisible" and (
        entry.connector_enabled_for_dry_run
        or entry.product_enabled
        or entry.production_crawl_enabled
    ):
        raise ValueError("Invisible must remain disabled and experimental.")


def validate_careers_url(value, board_identifier, allowed_hosts, index):
    if type(value) is not str or any(ord(char) < 32 for char in value):
        raise ValueError(f"Registry source {index} has invalid careers_url.")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.hostname not in allowed_hosts
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"Registry source {index} has an unsafe careers_url.")
    if parsed.path.rstrip("/") != f"/{board_identifier}":
        raise ValueError(
            f"Registry source {index} careers_url does not match board_identifier."
        )


def validate_allowed_hosts(value, index):
    hosts = validate_string_list(value, index, "allowed_job_hosts")
    normalized = []
    for host in hosts:
        if host != host.casefold() or not HOST_PATTERN.fullmatch(host):
            raise ValueError(f"Registry source {index} has invalid allowed job host {host!r}.")
        normalized.append(host)
    if not set(normalized).intersection(GREENHOUSE_HOSTS):
        raise ValueError(
            f"Registry source {index} must explicitly allow a supported Greenhouse host."
        )
    return tuple(normalized)


def validate_count_drop_policy(value, index):
    if type(value) is not dict or set(value) != COUNT_DROP_POLICY_FIELDS:
        raise ValueError(
            f"Registry source {index} count_drop_policy must contain exactly {sorted(COUNT_DROP_POLICY_FIELDS)}."
        )
    minimum = value["minimum_previous_count"]
    fraction = value["minimum_retained_fraction"]
    require_positive_integer(minimum, index, "count_drop_policy.minimum_previous_count")
    if minimum < 2:
        raise ValueError("Count-drop minimum_previous_count must be at least 2.")
    if type(fraction) is not float or not 0.1 <= fraction <= 1.0:
        raise ValueError(
            "Count-drop minimum_retained_fraction must be a float between 0.1 and 1.0."
        )
    return CountDropPolicy(minimum, fraction)


def validate_readiness_observations(value, index):
    if type(value) is not list:
        raise ValueError(f"Registry source {index} readiness_observations must be a list.")
    parsed = []
    for observation_index, raw in enumerate(value):
        label = f"Registry source {index} readiness observation {observation_index}"
        if type(raw) is not dict or set(raw) != READINESS_FIELDS:
            raise ValueError(f"{label} has an invalid field contract.")
        require_nonempty_string(raw["run_id"], index, "readiness_observations.run_id")
        require_nonempty_string(raw["observed_at"], index, "readiness_observations.observed_at")
        require_enum(raw["outcome"], READINESS_OUTCOMES, index, "readiness_observations.outcome")
        require_enum(
            raw["parser_version"],
            SUPPORTED_PARSER_VERSIONS,
            index,
            "readiness_observations.parser_version",
        )
        if type(raw["accepted_record_count"]) is not int or raw["accepted_record_count"] < 0:
            raise ValueError(f"{label} has invalid accepted_record_count.")
        parse_observed_at(raw["observed_at"], label)
        parsed.append(ReadinessObservation(**raw))
    run_ids = [item.run_id for item in parsed]
    timestamps = [item.observed_at for item in parsed]
    if len(run_ids) != len(set(run_ids)) or len(timestamps) != len(set(timestamps)):
        raise ValueError(f"Registry source {index} readiness observations must have distinct runs and timestamps.")
    if timestamps != sorted(timestamps, key=lambda item: parse_observed_at(item, "observation")):
        raise ValueError(f"Registry source {index} readiness observations must be chronological.")
    return tuple(parsed)


def readiness_streak(entry):
    trailing = []
    for observation in reversed(entry.readiness_observations):
        if (
            observation.outcome != "complete"
            or observation.parser_version != entry.parser_version
        ):
            break
        trailing.append(observation)
    trailing.reverse()
    if len(trailing) < 3:
        return len(trailing)
    first = parse_observed_at(trailing[0].observed_at, "observation")
    last = parse_observed_at(trailing[-1].observed_at, "observation")
    return len(trailing) if (last - first).total_seconds() >= 24 * 60 * 60 else 0


def validate_closed_string_list(value, index, field, allowed, *, allow_empty=False):
    values = validate_string_list(value, index, field, allow_empty=allow_empty)
    unsupported = sorted(set(values) - set(allowed))
    if unsupported:
        raise ValueError(f"Registry source {index} field {field} has unsupported values: {unsupported}.")
    return tuple(values)


def validate_string_list(value, index, field, *, allow_empty=False):
    if type(value) is not list or (not value and not allow_empty):
        raise ValueError(f"Registry source {index} field {field} must be a non-empty string list.")
    if any(type(item) is not str or not item.strip() or item != item.strip() for item in value):
        raise ValueError(f"Registry source {index} field {field} must be a string list.")
    if len(value) != len(set(value)):
        raise ValueError(f"Registry source {index} field {field} contains duplicates.")
    return tuple(value)


def require_enum(value, allowed, index, field):
    if type(value) is not str or value not in allowed:
        raise ValueError(f"Registry source {index} has unsupported {field}: {value!r}.")


def require_nonempty_string(value, index, field):
    if type(value) is not str or not value.strip() or value != value.strip():
        raise ValueError(f"Registry source {index} has invalid {field}.")


def require_positive_integer(value, index, field):
    if type(value) is not int or value < 1:
        raise ValueError(f"Registry source {index} field {field} must be a positive integer.")


def parse_observed_at(value, label):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} has an invalid observed_at timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} observed_at must include a timezone.")
    return parsed.astimezone(timezone.utc)


def require_unique(entries, field):
    values = [getattr(entry, field) for entry in entries]
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"Source registry has duplicate {field} values: {duplicates}.")


def registry_by_id(entries=None):
    values = load_source_registry() if entries is None else tuple(entries)
    return {entry.registry_id: entry for entry in values}


def registry_entry_for_company(company_slug, entries=None):
    values = load_source_registry() if entries is None else tuple(entries)
    normalized = str(company_slug or "").strip().casefold()
    normalized = MANAGED_SOURCE_ALIASES.get(normalized, normalized)
    for entry in values:
        if normalized in {entry.company_id, entry.board_identifier, entry.registry_id}:
            return entry
    return None


def assert_production_dispatch_allowed(company_slug, entries=None):
    entry = registry_entry_for_company(company_slug, entries)
    if entry is not None and not entry.production_crawl_enabled:
        raise PermissionError(
            f"Source {entry.company_id!r} is registry-managed and not enabled for ordinary production crawling."
        )
    return entry


def production_dispatch_allowed(company_slug, entries=None):
    try:
        assert_production_dispatch_allowed(company_slug, entries)
    except PermissionError:
        return False
    return True


def dry_run_entries(entries=None, *, include_control=True):
    values = load_source_registry() if entries is None else tuple(entries)
    priorities = {"pilot", "control"} if include_control else {"pilot"}
    return tuple(
        entry
        for entry in values
        if entry.connector_enabled_for_dry_run and entry.priority_tier in priorities
    )


def production_entries(entries=None):
    values = load_source_registry() if entries is None else tuple(entries)
    return tuple(entry for entry in values if entry.production_crawl_enabled)
