"""Typed profile preference contract and pure legacy compatibility adapters.

This module does no I/O.  ``profile_preferences_v1`` is an optional Canonical
Profile V2 subdocument.  AI-assisted onboarding is its first writer; manual
profile writers and the current matcher remain on the legacy preference
fields until later, separately approved slices.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re

from wahojobs.matching.taxonomy import CAREER_LEVELS, OCCUPATIONAL_FAMILIES
from wahojobs.profiles.canonical import (
    UNKNOWN,
    validate_preferences,
)
from wahojobs.profiles.seniority_presentation import (
    target_career_level_display_choices,
)


SCHEMA_VERSION = "profile_preferences_v1"

EMPLOYMENT_RELATIONSHIPS = frozenset({"employee", "independent_contractor"})
WORKLOADS = frozenset({"full_time", "part_time"})
ENGAGEMENT_TERMS = frozenset(
    {"permanent", "fixed_term", "temporary", "seasonal", "internship"}
)
SCHEDULE_FLEXIBILITY_MODES = frozenset({"fixed", "flexible"})
SCHEDULE_COORDINATION_MODES = frozenset({"synchronous", "asynchronous"})
SCHEDULE_TIME_WINDOWS = frozenset(
    {"business_hours", "weekdays", "evenings", "weekends"}
)
PHONE_VOICE_MODES = frozenset({"phone", "non_phone"})
JOB_INTEREST_CODES = frozenset(OCCUPATIONAL_FAMILIES)
ACCEPTED_CAREER_LEVELS = frozenset(CAREER_LEVELS)
SOFT_PREFERENCE_DIMENSIONS = frozenset({"job_interests"})
COMPENSATION_MINIMUM_KINDS = frozenset({"none", "preferred", "strict"})
COMPENSATION_PERIODS = frozenset({"hour", "month", "year"})

# V1 deliberately accepts ordinary circulating currencies, not ISO fund,
# precious-metal, testing, or reserved X-codes.  The set is closed so a
# three-letter typo cannot silently become durable profile state.
ISO_4217_CURRENCIES = frozenset(
    {
        "AED", "AFN", "ALL", "AMD", "AOA", "ARS", "AUD", "AWG", "AZN",
        "BAM", "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB",
        "BRL", "BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHF",
        "CLP", "CNY", "COP", "CRC", "CUP", "CVE", "CZK", "DJF", "DKK",
        "DOP", "DZD", "EGP", "ERN", "ETB", "EUR", "FJD", "FKP", "GBP",
        "GEL", "GHS", "GIP", "GMD", "GNF", "GTQ", "GYD", "HKD", "HNL",
        "HTG", "HUF", "IDR", "ILS", "INR", "IQD", "IRR", "ISK", "JMD",
        "JOD", "JPY", "KES", "KGS", "KHR", "KMF", "KPW", "KRW", "KWD",
        "KYD", "KZT", "LAK", "LBP", "LKR", "LRD", "LSL", "LYD", "MAD",
        "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRU", "MUR", "MVR",
        "MWK", "MXN", "MYR", "MZN", "NAD", "NGN", "NIO", "NOK", "NPR",
        "NZD", "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN", "PYG",
        "QAR", "RON", "RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG",
        "SEK", "SGD", "SHP", "SLE", "SOS", "SRD", "SSP", "STN", "SVC",
        "SYP", "SZL", "THB", "TJS", "TMT", "TND", "TOP", "TRY", "TTD",
        "TWD", "TZS", "UAH", "UGX", "USD", "UYU", "UZS", "VED", "VES",
        "VND", "VUV", "WST", "XAF", "XCD", "XCG", "XOF", "XPF", "YER",
        "ZAR", "ZMW", "ZWG",
    }
)

_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "employment_relationships",
        "workloads",
        "engagement_terms",
        "schedule",
        "accepted_phone_voice_modes",
        "job_interests",
        "accepted_career_levels",
        "compensation",
    }
)
_SCHEDULE_FIELDS = frozenset(
    {"flexibility_modes", "coordination_modes", "time_windows"}
)
_COMPENSATION_FIELDS = frozenset(
    {"minimum_kind", "amount", "currency", "period"}
)
PREFERENCE_ENUM_LIST_PATHS = (
    ("employment_relationships",),
    ("workloads",),
    ("engagement_terms",),
    ("schedule", "flexibility_modes"),
    ("schedule", "coordination_modes"),
    ("schedule", "time_windows"),
    ("accepted_phone_voice_modes",),
    ("job_interests",),
    ("accepted_career_levels",),
)

_CONTROL_COPY = {
    "employment_relationships": (
        "Employment relationship",
        "Select every relationship you would consider.",
    ),
    "workloads": (
        "Workload",
        "Full-time and part-time can both be acceptable.",
    ),
    "engagement_terms": (
        "Engagement term",
        "Select every duration or engagement type you would consider.",
    ),
    "schedule.flexibility_modes": (
        "Schedule flexibility",
        "Select fixed schedules, flexible schedules, or both.",
    ),
    "schedule.coordination_modes": (
        "Team coordination",
        "Choose whether you can work synchronously, asynchronously, or both.",
    ),
    "schedule.time_windows": (
        "Working time windows",
        "Select every time window you would consider.",
    ),
    "accepted_phone_voice_modes": (
        "Phone and voice work",
        "Select the kinds of communication work you would accept.",
    ),
    "job_interests": (
        "Job interests",
        "Select every area you would like to see in your matches.",
    ),
    "accepted_career_levels": (
        "Target career levels",
        "These are levels you would accept in a new role, not your current seniority.",
    ),
}

_CHOICE_LABELS = {
    "accounting_finance": "Accounting & finance",
    "employee": "Employee",
    "independent_contractor": "Independent contractor / freelance",
    "full_time": "Full-time",
    "part_time": "Part-time",
    "fixed_term": "Fixed-term / contract",
    "non_phone": "Non-phone / non-voice",
    "business_hours": "Business hours",
    "quality_assurance": "Quality assurance",
    "ai_training": "AI training",
    "sales_marketing": "Sales & marketing",
    "science_research": "Science & research",
    "translation_localization": "Translation & localization",
    "writing_editing": "Writing & editing",
}

_CHOICE_DESCRIPTIONS = {
    "employee": "You are employed by the organization offering the role.",
    "independent_contractor": "You provide services independently, including freelance work.",
    "full_time": "A full working load as defined by the employer or client.",
    "part_time": "A working load below the organization's full-time schedule.",
    "permanent": "An ongoing role with no planned end date.",
    "fixed_term": "A contract or role with a defined duration or end date.",
    "temporary": "Short-term work for a limited need.",
    "seasonal": "Work tied to a recurring season or peak period.",
    "internship": "A structured learning or early-career placement.",
    "fixed": "Working times are set in advance.",
    "flexible": "Working times can vary within agreed expectations.",
    "synchronous": "You can overlap with teammates or customers in real time.",
    "asynchronous": "Work can be completed without continuous real-time overlap.",
    "business_hours": "Work during the organization's regular daytime hours.",
    "weekdays": "Work Monday through Friday.",
    "evenings": "Work during evening hours.",
    "weekends": "Work on Saturday or Sunday.",
    "phone": "The role may include calls or other live voice communication.",
    "non_phone": "The role can be completed without phone or live voice work.",
}

_COMPENSATION_KIND_COPY = {
    "none": (
        "No minimum",
        "Do not use pay as a limit.",
    ),
    "preferred": (
        "Preferred minimum",
        "Your target. You may choose to lower it later to see more opportunities.",
    ),
    "strict": (
        "Strict minimum",
        "Your firm floor. Wahojobs will not suggest lowering it.",
    ),
}
_LEGACY_PREFERENCE_FIELDS = frozenset(
    {
        "remote",
        "flexible",
        "employment_types",
        "synchronous_preference",
        "phone_preference",
        "schedule",
        "availability",
        "rate_pay_preference",
        "target_opportunity_types",
        "preferred_task_types",
        "work_preferences",
    }
)
_DECIMAL_PATTERN = re.compile(r"^[0-9]{1,18}(?:\.[0-9]{1,2})?$")


class ProfilePreferenceModelError(ValueError):
    """A bounded preference-contract failure that never includes user values."""

    def __init__(self, *reason_codes: str):
        self.reason_codes = tuple(sorted(set(reason_codes or ("invalid_model",))))[:32]
        super().__init__(
            "profile_preferences_v1 rejected; reason_codes="
            + ",".join(self.reason_codes)
        )


def empty_profile_preferences_v1() -> dict:
    """Return the exact unrestricted V1 preference model."""
    return {
        "schema_version": SCHEMA_VERSION,
        "employment_relationships": [],
        "workloads": [],
        "engagement_terms": [],
        "schedule": {
            "flexibility_modes": [],
            "coordination_modes": [],
            "time_windows": [],
        },
        "accepted_phone_voice_modes": [],
        "job_interests": [],
        "accepted_career_levels": [],
        "compensation": {
            "minimum_kind": "none",
            "amount": None,
            "currency": None,
            "period": None,
        },
    }


def profile_preference_control_catalog_v1() -> dict:
    """Return UI copy whose values are always derived from contract enums."""

    allowed_by_path = {
        ("employment_relationships",): EMPLOYMENT_RELATIONSHIPS,
        ("workloads",): WORKLOADS,
        ("engagement_terms",): ENGAGEMENT_TERMS,
        ("schedule", "flexibility_modes"): SCHEDULE_FLEXIBILITY_MODES,
        ("schedule", "coordination_modes"): SCHEDULE_COORDINATION_MODES,
        ("schedule", "time_windows"): SCHEDULE_TIME_WINDOWS,
        ("accepted_phone_voice_modes",): PHONE_VOICE_MODES,
        ("job_interests",): JOB_INTEREST_CODES,
        ("accepted_career_levels",): ACCEPTED_CAREER_LEVELS,
    }
    dimensions = []
    for path in PREFERENCE_ENUM_LIST_PATHS:
        identifier = ".".join(path)
        title, help_text = _CONTROL_COPY[identifier]
        if path == ("accepted_career_levels",):
            choices = list(target_career_level_display_choices())
        else:
            choices = []
            for code in sorted(allowed_by_path[path]):
                label = _CHOICE_LABELS.get(code, code.replace("_", " ").title())
                description = _CHOICE_DESCRIPTIONS.get(
                    code,
                    f"Include {label.casefold()} opportunities.",
                )
                choices.append(
                    {"code": code, "label": label, "description": description}
                )
        dimensions.append(
            {
                "id": identifier,
                "path": path,
                "title": title,
                "help": help_text,
                "choices": tuple(choices),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "dimensions": tuple(dimensions),
        "compensation": {
            "minimum_kinds": tuple(
                {
                    "code": code,
                    "label": _COMPENSATION_KIND_COPY[code][0],
                    "description": _COMPENSATION_KIND_COPY[code][1],
                }
                for code in ("none", "preferred", "strict")
            ),
            "currencies": tuple(sorted(ISO_4217_CURRENCIES)),
            "periods": tuple(
                {"code": code, "label": f"Per {code}"}
                for code in ("hour", "month", "year")
            ),
        },
    }


def canonical_decimal_string(value: str) -> str:
    """Return one positive decimal representation with at most two places."""
    if type(value) is not str or _DECIMAL_PATTERN.fullmatch(value) is None:
        raise ProfilePreferenceModelError("invalid_compensation_amount")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ProfilePreferenceModelError("invalid_compensation_amount") from exc
    if amount <= 0:
        raise ProfilePreferenceModelError("invalid_compensation_amount")
    return format(amount.normalize(), "f")


def canonicalize_profile_preferences_v1(value: dict) -> dict:
    """Validate and return the deterministic authoritative subdocument."""
    if type(value) is not dict:
        raise ProfilePreferenceModelError("preference_model_not_object")
    candidate = deepcopy(value)
    errors: list[str] = []
    if set(candidate) != _ROOT_FIELDS:
        errors.append("invalid_preference_model_fields")
    if candidate.get("schema_version") != SCHEMA_VERSION:
        errors.append("invalid_preference_model_version")

    _canonical_enum_list(
        candidate, "employment_relationships", EMPLOYMENT_RELATIONSHIPS, errors
    )
    _canonical_enum_list(candidate, "workloads", WORKLOADS, errors)
    _canonical_enum_list(candidate, "engagement_terms", ENGAGEMENT_TERMS, errors)
    _canonical_enum_list(
        candidate, "accepted_phone_voice_modes", PHONE_VOICE_MODES, errors
    )
    _canonical_enum_list(candidate, "job_interests", JOB_INTEREST_CODES, errors)
    _canonical_enum_list(
        candidate, "accepted_career_levels", ACCEPTED_CAREER_LEVELS, errors
    )

    schedule = candidate.get("schedule")
    if type(schedule) is not dict or set(schedule) != _SCHEDULE_FIELDS:
        errors.append("invalid_preference_schedule")
    else:
        _canonical_enum_list(
            schedule, "flexibility_modes", SCHEDULE_FLEXIBILITY_MODES, errors
        )
        _canonical_enum_list(
            schedule, "coordination_modes", SCHEDULE_COORDINATION_MODES, errors
        )
        _canonical_enum_list(schedule, "time_windows", SCHEDULE_TIME_WINDOWS, errors)

    compensation = candidate.get("compensation")
    if type(compensation) is not dict or set(compensation) != _COMPENSATION_FIELDS:
        errors.append("invalid_preference_compensation")
    else:
        kind = compensation.get("minimum_kind")
        if kind not in COMPENSATION_MINIMUM_KINDS:
            errors.append("invalid_compensation_minimum_kind")
        currency = compensation.get("currency")
        if type(currency) is str:
            currency = currency.upper()
            compensation["currency"] = currency
        if kind == "none":
            if any(compensation.get(field) is not None for field in ("amount", "currency", "period")):
                errors.append("compensation_none_has_values")
        elif kind in {"preferred", "strict"}:
            try:
                compensation["amount"] = canonical_decimal_string(
                    compensation.get("amount")
                )
            except ProfilePreferenceModelError as exc:
                errors.extend(exc.reason_codes)
            if currency not in ISO_4217_CURRENCIES:
                errors.append("invalid_compensation_currency")
            if compensation.get("period") not in COMPENSATION_PERIODS:
                errors.append("invalid_compensation_period")

    if errors:
        raise ProfilePreferenceModelError(*errors)
    return candidate


def validate_profile_preferences_v1(value: dict) -> dict:
    """Alias emphasizing that canonicalization is part of validation."""
    return canonicalize_profile_preferences_v1(value)


def legacy_preferences_to_preference_draft(legacy_preferences: dict) -> dict:
    """Conservatively map legacy fields and report every lossy ambiguity."""
    legacy = _validated_legacy_preferences(legacy_preferences, allow_partial=True)
    model = empty_profile_preferences_v1()
    ambiguities: set[tuple[str, str]] = set()

    employment = set(legacy.get("employment_types") or [])
    schedule = set(legacy.get("schedule") or [])
    work_preferences = set(legacy.get("work_preferences") or [])
    employment_choices = employment | work_preferences

    if "freelance" in employment_choices:
        model["employment_relationships"].append("independent_contractor")
    if "full-time" in employment_choices or "full-time" in schedule:
        model["workloads"].append("full_time")
    if "part-time" in employment_choices or "part-time" in schedule:
        model["workloads"].append("part_time")
    for legacy_value, new_value in (
        ("temporary", "temporary"),
        ("seasonal", "seasonal"),
        ("internship", "internship"),
    ):
        if legacy_value in employment_choices:
            model["engagement_terms"].append(new_value)
    if "entry-level" in employment_choices:
        model["accepted_career_levels"].append("entry")
    if "contract" in employment_choices:
        ambiguities.add(
            ("legacy_contract_requires_confirmation", "preferences.employment_types")
        )

    if (
        legacy.get("flexible") is True
        or legacy.get("synchronous_preference") == "flexible"
        or "flexible" in employment_choices
        or "flexible" in schedule
    ):
        model["schedule"]["flexibility_modes"].append("flexible")
    for legacy_value, new_value in (
        ("synchronous", "synchronous"),
        ("asynchronous", "asynchronous"),
    ):
        if (
            legacy.get("synchronous_preference") == legacy_value
            or legacy_value in schedule
        ):
            model["schedule"]["coordination_modes"].append(new_value)
    for legacy_value, new_value in (
        ("business hours", "business_hours"),
        ("weekdays", "weekdays"),
        ("evenings", "evenings"),
        ("weekends", "weekends"),
    ):
        if legacy_value in schedule:
            model["schedule"]["time_windows"].append(new_value)

    phone = legacy.get("phone_preference", UNKNOWN)
    if phone == "phone acceptable":
        model["accepted_phone_voice_modes"].extend(("phone", "non_phone"))
    elif phone == "non-phone required":
        model["accepted_phone_voice_modes"].append("non_phone")
    elif phone in {"phone preferred", "non-phone preferred"}:
        model["accepted_phone_voice_modes"].extend(("phone", "non_phone"))
        ambiguities.add(
            ("legacy_phone_strength_requires_confirmation", "preferences.phone_preference")
        )

    interest_fields = (
        "target_opportunity_types",
        "preferred_task_types",
    )
    for field in interest_fields:
        values = legacy.get(field) or []
        model["job_interests"].extend(
            item for item in values if item in JOB_INTEREST_CODES
        )
        if any(item not in JOB_INTEREST_CODES for item in values):
            ambiguities.add(
                ("legacy_job_interest_requires_confirmation", f"preferences.{field}")
            )

    if legacy.get("rate_pay_preference"):
        ambiguities.add(
            ("legacy_compensation_requires_confirmation", "preferences.rate_pay_preference")
        )

    canonical = canonicalize_profile_preferences_v1(model)
    return {
        "preference_model": canonical,
        "ambiguities": [
            {"code": code, "field_path": path}
            for code, path in sorted(ambiguities)
        ],
    }


def preference_model_to_legacy_preferences(
    preference_model: dict,
    *,
    legacy_base: dict | None = None,
) -> dict:
    """Project the typed model into one valid, intentionally lossy V1 mirror."""
    model = canonicalize_profile_preferences_v1(preference_model)
    legacy = _validated_legacy_preferences(legacy_base or {}, allow_partial=True)
    result = _legacy_defaults()
    for field in ("remote", "availability"):
        if field in legacy:
            result[field] = deepcopy(legacy[field])

    employment: set[str] = set()
    if "independent_contractor" in model["employment_relationships"]:
        employment.add("freelance")
    employment.update(
        {"full-time" if value == "full_time" else "part-time" for value in model["workloads"]}
    )
    term_mapping = {
        "fixed_term": "contract",
        "temporary": "temporary",
        "seasonal": "seasonal",
        "internship": "internship",
    }
    employment.update(
        term_mapping[value]
        for value in model["engagement_terms"]
        if value in term_mapping
    )
    if "entry" in model["accepted_career_levels"]:
        employment.add("entry-level")
    if "flexible" in model["schedule"]["flexibility_modes"]:
        employment.add("flexible")
    result["employment_types"] = sorted(employment)

    result["flexible"] = "flexible" in model["schedule"]["flexibility_modes"]
    coordination = model["schedule"]["coordination_modes"]
    result["synchronous_preference"] = (
        coordination[0] if len(coordination) == 1 else "no preference"
    )
    phone_modes = set(model["accepted_phone_voice_modes"])
    result["phone_preference"] = {
        frozenset(): "no preference",
        frozenset({"phone", "non_phone"}): "phone acceptable",
        frozenset({"non_phone"}): "non-phone required",
        frozenset({"phone"}): "phone preferred",
    }[frozenset(phone_modes)]

    projected_schedule = {
        "full-time" if value == "full_time" else "part-time"
        for value in model["workloads"]
    }
    projected_schedule.update(model["schedule"]["coordination_modes"])
    projected_schedule.update(
        value.replace("_", " ") for value in model["schedule"]["time_windows"]
    )
    if result["flexible"]:
        projected_schedule.add("flexible")
    result["schedule"] = sorted(projected_schedule)

    result["target_opportunity_types"] = list(model["job_interests"])
    result["preferred_task_types"] = []
    work_preferences = set(result["employment_types"])
    if result["remote"]:
        work_preferences.add("remote")
    if result["flexible"]:
        work_preferences.add("flexible")
    result["work_preferences"] = sorted(work_preferences)

    compensation = model["compensation"]
    if compensation["minimum_kind"] == "none":
        result["rate_pay_preference"] = ""
    else:
        result["rate_pay_preference"] = (
            f"{compensation['minimum_kind']} minimum: "
            f"{compensation['currency']} {compensation['amount']} per "
            f"{compensation['period']}"
        )
    _validated_legacy_preferences(result, allow_partial=False)
    return result


def _canonical_enum_list(container, field, allowed, errors):
    value = container.get(field)
    if type(value) is not list or len(value) > len(allowed):
        errors.append("invalid_preference_enum_list")
        return
    if any(type(item) is not str or item not in allowed for item in value):
        errors.append("invalid_preference_enum_value")
        return
    if len(value) != len(set(value)):
        errors.append("duplicate_preference_enum_value")
        return
    container[field] = sorted(value)


def _legacy_defaults() -> dict:
    return {
        "remote": False,
        "flexible": False,
        "employment_types": [],
        "synchronous_preference": "no preference",
        "phone_preference": "no preference",
        "schedule": [],
        "availability": UNKNOWN,
        "rate_pay_preference": "",
        "target_opportunity_types": [],
        "preferred_task_types": [],
        "work_preferences": [],
    }


def _validated_legacy_preferences(value, *, allow_partial):
    if type(value) is not dict or set(value) - _LEGACY_PREFERENCE_FIELDS:
        raise ProfilePreferenceModelError("invalid_legacy_preferences")
    if not allow_partial and set(value) != _LEGACY_PREFERENCE_FIELDS:
        raise ProfilePreferenceModelError("invalid_legacy_preferences")
    result = _legacy_defaults()
    result.update(deepcopy(value))
    errors: list[str] = []
    validate_preferences(result, errors)
    if errors:
        raise ProfilePreferenceModelError("invalid_legacy_preferences")
    return result
