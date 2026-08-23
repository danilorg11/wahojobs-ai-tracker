"""Shared closed vocabularies used by matching-adjacent configuration."""


OCCUPATIONAL_FAMILIES = frozenset(
    {
        "accounting_finance",
        "administrative_support",
        "ai_training",
        "content_moderation",
        "customer_support",
        "data_analysis",
        "data_annotation",
        "design",
        "digital_operations",
        "expert_review",
        "healthcare",
        "language_data",
        "legal",
        "operations",
        "project_management",
        "quality_assurance",
        "sales_marketing",
        "science_research",
        "search_evaluation",
        "software_engineering",
        "software_testing",
        "technical",
        "translation_localization",
        "writing_editing",
    }
)


# Shared by opportunity enrichment and the candidate's accepted target levels.
# Candidate seniority remains a separate profile fact.
CAREER_LEVELS = frozenset(
    {"internship", "entry", "mid", "senior", "lead", "principal", "manager"}
)
