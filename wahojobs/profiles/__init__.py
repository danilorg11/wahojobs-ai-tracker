"""Profile normalization contracts for Wahojobs."""

from wahojobs.profiles.canonical import (
    SCHEMA_VERSION,
    canonical_profile_debug_summary,
    canonical_profile_errors,
    canonical_to_matcher_profile,
    matcher_profile_to_canonical,
    validate_canonical_profile,
)
from wahojobs.profiles.normalizer import (
    BaselineHeuristicProfileNormalizer,
    FixtureExpectedProfileNormalizer,
    NormalizationResult,
    ProfileNormalizer,
    compare_canonical_profiles,
    normalize_profile_input,
)

__all__ = [
    "SCHEMA_VERSION",
    "BaselineHeuristicProfileNormalizer",
    "FixtureExpectedProfileNormalizer",
    "NormalizationResult",
    "ProfileNormalizer",
    "canonical_profile_debug_summary",
    "canonical_profile_errors",
    "canonical_to_matcher_profile",
    "compare_canonical_profiles",
    "matcher_profile_to_canonical",
    "normalize_profile_input",
    "validate_canonical_profile",
]
