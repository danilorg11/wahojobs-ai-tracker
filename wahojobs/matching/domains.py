from dataclasses import dataclass
from functools import lru_cache
import re


CORE_DOMAIN_ALIGNMENT_BONUS = 12
ESSENTIAL_DOMAIN_MISMATCH_PENALTY = 28

DOMAIN_LABELS = {
    "technical": "software engineering",
    "finance": "finance",
    "legal": "legal",
    "biology": "biology",
    "medicine": "medical",
    "chemistry": "chemistry",
    "physics": "physics",
    "mathematics": "mathematics",
    "material_science": "materials science",
}

PROFILE_DOMAIN_ALIASES = {
    "technical": (
        "software engineering",
        "software engineer",
        "backend development",
        "backend developer",
        "frontend development",
        "full stack development",
        "programming",
        "coding evaluation",
        "code review",
        "software testing",
        "software evaluation",
        "machine learning engineering",
    ),
    "finance": (
        "finance",
        "financial analysis",
        "financial modeling",
        "accounting",
        "investment",
        "economics",
        "banking",
    ),
    "legal": ("law", "legal", "lawyer", "attorney", "litigation"),
    "biology": (
        "biology",
        "biologist",
        "molecular biology",
        "genetics",
        "ecology",
        "microbiology",
        "genomics",
        "life science",
    ),
    "medicine": (
        "medicine",
        "medical",
        "clinical",
        "healthcare",
        "dermatology",
        "pharmaceutical",
    ),
    "chemistry": ("chemistry", "chemical science"),
    "physics": ("physics", "physicist"),
    "mathematics": ("mathematics", "math", "statistics"),
    "material_science": ("material science", "materials science"),
}

ROLE_DOMAIN_ALIASES = {
    "technical": (
        "software engineer",
        "software developer",
        "backend engineer",
        "backend developer",
        "frontend engineer",
        "frontend developer",
        "full stack engineer",
        "full stack developer",
        "coding specialist",
        "coding expert",
        "coding evaluator",
        "programming specialist",
        "software evaluation",
        "python engineer",
        "python developer",
        "machine learning engineer",
        "ml engineer",
    ),
    "finance": (
        "finance",
        "financial",
        "accounting",
        "accountant",
        "investment",
        "economics",
        "economist",
        "banking",
        "banker",
        "underwriter",
    ),
    "legal": (
        "law",
        "legal",
        "lawyer",
        "attorney",
        "litigation",
        "ip expert",
        "patent expert",
        "trademark expert",
    ),
    "biology": (
        "biology",
        "biologist",
        "molecular biology",
        "genetics",
        "ecology",
        "microbiology",
        "genomics",
        "life science",
    ),
    "medicine": (
        "medicine",
        "medical",
        "physician",
        "clinical",
        "healthcare",
        "dermatology",
        "dermatologist",
        "pharmaceutical",
    ),
    "chemistry": ("chemistry", "chemical engineering"),
    "physics": ("physics", "physicist"),
    "mathematics": ("mathematics", "math expert", "statistics expert"),
    "material_science": ("material science", "materials science"),
}

ESSENTIAL_SPECIALIST_DOMAINS = frozenset(DOMAIN_LABELS) - {"technical"}
DECISIVE_PROFESSIONAL_HARD_GATE_DOMAINS = frozenset({"finance", "legal"})


@dataclass(frozen=True)
class DomainAlignment:
    profile_domains: frozenset[str]
    role_domains: frozenset[str]
    matched_domains: frozenset[str]
    missing_essential_domains: frozenset[str]
    alignment_score: int
    mismatch_penalty: int
    hard_gate: bool
    reason: str

    @property
    def ranking_delta(self):
        return self.alignment_score - self.mismatch_penalty

    def as_dict(self):
        return {
            "profile_domains": sorted(self.profile_domains),
            "role_domains": sorted(self.role_domains),
            "matched_domains": sorted(self.matched_domains),
            "missing_essential_domains": sorted(self.missing_essential_domains),
            "alignment_score": self.alignment_score,
            "mismatch_penalty": self.mismatch_penalty,
            "hard_gate": self.hard_gate,
            "reason": self.reason,
        }


def assess_domain_alignment(profile, row):
    profile_domains = detect_profile_domains(profile)
    role_domains = detect_role_domains(row)
    matched = profile_domains & role_domains
    missing = (role_domains & ESSENTIAL_SPECIALIST_DOMAINS) - profile_domains
    alignment_score = CORE_DOMAIN_ALIGNMENT_BONUS if matched and not missing else 0
    mismatch_penalty = ESSENTIAL_DOMAIN_MISMATCH_PENALTY if missing else 0
    hard_gate = bool(missing & DECISIVE_PROFESSIONAL_HARD_GATE_DOMAINS)

    if missing:
        labels = ", ".join(DOMAIN_LABELS[item] for item in sorted(missing))
        reason = f"Essential {labels} domain is not supported by this profile."
    elif matched:
        labels = ", ".join(DOMAIN_LABELS[item] for item in sorted(matched))
        reason = f"Core {labels} domain alignment."
    else:
        reason = "No decisive professional-domain signal."

    return DomainAlignment(
        profile_domains=frozenset(profile_domains),
        role_domains=frozenset(role_domains),
        matched_domains=frozenset(matched),
        missing_essential_domains=frozenset(missing),
        alignment_score=alignment_score,
        mismatch_penalty=mismatch_penalty,
        hard_gate=hard_gate,
        reason=reason,
    )


def detect_profile_domains(profile):
    values = []
    for field in ("degrees_or_domains", "skills", "target_opportunity_types"):
        values.extend(str(value) for value in profile.get(field) or [])
    return set(detect_profile_domains_cached(tuple(values)))


@lru_cache(maxsize=256)
def detect_profile_domains_cached(values):
    return frozenset(detect_domains(" ".join(values), PROFILE_DOMAIN_ALIASES))


def detect_role_domains(row):
    title_text = " ".join(
        str(row.get(field) or "") for field in ("title", "canonical_title")
    )
    structured_text = " ".join(
        str(row.get(field) or "")
        for field in ("expertise", "department", "source_category")
    )
    return set(detect_role_domains_cached(title_text, structured_text))


@lru_cache(maxsize=16384)
def detect_role_domains_cached(title_text, structured_text):
    title_domains = detect_domains(title_text, ROLE_DOMAIN_ALIASES)
    if title_domains:
        return frozenset(title_domains)
    return frozenset(detect_domains(structured_text, ROLE_DOMAIN_ALIASES))


def detect_domains(text, aliases_by_domain):
    normalized = normalize_text(text)
    domains = set()
    for domain, aliases in aliases_by_domain.items():
        if any(contains_alias(normalized, alias) for alias in aliases):
            domains.add(domain)
    return domains


def contains_alias(text, alias):
    pattern = alias_pattern(alias)
    return pattern.search(text) is not None if pattern else False


@lru_cache(maxsize=256)
def alias_pattern(alias):
    normalized_alias = normalize_text(alias)
    if not normalized_alias:
        return None
    parts = re.findall(r"[a-z0-9+#.]+", normalized_alias)
    if not parts:
        return re.compile(re.escape(normalized_alias))
    separator = r"[\s\-/&()+.,:]+"
    pattern = separator.join(re.escape(part) for part in parts)
    return re.compile(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])")


def normalize_text(value):
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()
