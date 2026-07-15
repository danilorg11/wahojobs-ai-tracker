from copy import deepcopy
from datetime import datetime, timezone
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import local_product_app as app
import profile_match_digest as matcher
import profile_to_matches_preview as preview
from wahojobs.matching.fit_evidence import build_profile_fit_evidence
from wahojobs.matching.specializations import specialization_evidence
from wahojobs.profiles.canonical import canonical_to_matcher_profile
from wahojobs.profiles.normalizer import BaselineHeuristicProfileNormalizer


EVALUATED_AT = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

SOFTWARE_PROFILE = """I live in Brazil and I am fluent in Portuguese and English. I am a software engineer with 8 years of professional experience. I have strong skills in Python, JavaScript, SQL, APIs, backend development, debugging, code review, software testing, and technical documentation. I have a bachelor's degree in Computer Science. I am interested in remote AI training, coding evaluation, code generation review, software engineering, technical annotation, and programming-related AI projects."""

BIOLOGY_PROFILE = """I live in Brazil and I am fluent in Portuguese and English. I have a master's degree in Biology and academic research experience in molecular biology, genetics, ecology, scientific literature review, data analysis, and scientific writing. I have experience reading and evaluating research papers and explaining complex biological concepts. I am interested in remote AI training, biology subject-matter expert work, scientific content evaluation, research, fact-checking, and data annotation."""

BEGINNER_PROFILE = """I live in Brazil. Portuguese is my native language and I am fluent in English. I have a generalist background and I am interested in remote AI training, data annotation, content evaluation, search evaluation, and language-data work. I do not have a university degree, specialized technical experience, scientific credentials, or professional certifications. I am looking for entry-level, flexible, non-phone work that does not require previous experience."""


def matcher_profile(raw_text):
    normalized = BaselineHeuristicProfileNormalizer().normalize(
        raw_text,
        "long_paragraph",
        {"profile_id": "test_profile", "display_name": "Test Profile"},
    )
    profile = canonical_to_matcher_profile(normalized.canonical_profile)
    profile["language_locale_keys"] = preview.canonical_language_locale_keys(
        normalized.canonical_profile
    )
    return profile


def opportunity(title, job_id, expertise="General", required_languages=None):
    observed_at = EVALUATED_AT.isoformat()
    return {
        "job_id": job_id,
        "title": title,
        "canonical_title": None,
        "source": "Synthetic",
        "source_slug": "synthetic",
        "source_tier": "core",
        "location": "Remote",
        "url": f"https://example.test/jobs/{job_id}",
        "department": expertise,
        "expertise": expertise,
        "source_category": expertise,
        "commitment": "Freelance",
        "opportunity_kind": "live_posting",
        "availability_basis": "api_feed",
        "inventory_model": "live_feed",
        "market_count_policy": "count_live",
        "include_in_live_market_estimate": 1,
        "canonical_opportunity_id": job_id,
        "canonical_is_active": True,
        "job_is_active": True,
        "job_last_seen_at": observed_at,
        "latest_successful_source_run_at": observed_at,
        "source_run_started_at": observed_at,
        "source_run_id": 1,
        "source_run_qualifies": True,
        "language": None,
        "language_locale": None,
        "required_languages": required_languages,
    }


def build_ranked(raw_profile, rows):
    profile = matcher_profile(raw_profile)
    specializations = specialization_evidence(profile)
    fit_evidence = build_profile_fit_evidence(profile)
    matches = []
    for row in rows:
        match = matcher.score_opportunity(profile, row)
        matches.append(
            preview.apply_preview_guardrails(
                profile,
                row,
                match,
                supported_specializations=specializations,
                profile_fit_evidence=fit_evidence,
                evaluated_at=EVALUATED_AT,
            )
        )
    matches = preview.ensure_safe_do_these_first(preview.dedupe_matches(matches), profile)
    context = {"matches": {section: [] for section in preview.SECTION_ORDER}}
    for match in sorted(matches, key=preview.match_sort_key):
        context["matches"][match["preview_section"]].append(match)
    return profile, context, app.build_ranked_presentation_matches(context)


def by_title(context):
    return {
        match["display_title"]: match
        for section in preview.SECTION_ORDER
        for match in context["matches"][section]
    }


class DomainSpecificRankingTests(unittest.TestCase):
    def test_software_roles_outrank_finance_roles_with_python_overlap(self):
        rows = [
            opportunity("Coding Specialist (PT-BR)", 1, "Engineering", "Portuguese"),
            opportunity("Software Engineer", 2, "Engineering"),
            opportunity("Backend Developer - Data Annotation Systems", 3, "Engineering"),
            opportunity("Software Engineer - Machine Learning", 4, "Engineering"),
            opportunity("Python Coding Specialist", 5, "Engineering"),
            opportunity("Finance & Python Expert", 6, "Finance"),
            opportunity("Financial Analyst (Python & Modeling)", 7, "Finance"),
            opportunity("Financial Modeling Expert (Python & GitHub)", 8, "Finance"),
        ]

        _, context, ranked = build_ranked(SOFTWARE_PROFILE, rows)
        titles = [match["display_title"] for match in ranked]
        details = by_title(context)

        self.assertEqual(set(titles[:5]), {row["title"] for row in rows[:5]})
        self.assertIn("Coding Specialist (PT-BR)", titles[:5])
        self.assertGreaterEqual(
            sum(any(term in title.lower() for term in ("software", "backend", "coding")) for title in titles[:5]),
            4,
        )
        self.assertEqual(details["Coding Specialist (PT-BR)"]["core_domain_score"], 12)
        for title in (row["title"] for row in rows[5:]):
            match = details[title]
            self.assertEqual(match["specialist_domain_penalty"], 28)
            self.assertTrue(match["professional_domain_hard_gate_applied"])
            self.assertFalse(match["primary_recommendation_eligible"])
            self.assertEqual(match["preview_section"], "excluded")
            self.assertIn("essential professional domain", preview.user_fit_reason(match))

    def test_biology_roles_outrank_generic_language_and_writing_roles(self):
        biology_titles = [
            "Biology Expert - Masters and PhDs",
            "Biology Specialist",
            "Biology Researcher",
            "Molecular Biology Expert",
            "Genetics and Ecology Biology Expert",
        ]
        rows = [opportunity(title, index, "Biology") for index, title in enumerate(biology_titles, 1)]
        rows.extend(
            [
                opportunity("English Writing Generalist", 20, "Writing", "English"),
                opportunity("Portuguese Language Data Contributor", 21, "Language", "Portuguese"),
            ]
        )

        _, context, ranked = build_ranked(BIOLOGY_PROFILE, rows)
        titles = [match["display_title"] for match in ranked]
        details = by_title(context)

        self.assertEqual(set(titles[:5]), set(biology_titles))
        self.assertLess(titles.index(biology_titles[0]), titles.index("English Writing Generalist"))
        for title in biology_titles:
            match = details[title]
            self.assertEqual(match["core_domain_score"], 12)
            self.assertGreater(match["ranking_score"], match["raw_matcher_score"])
            self.assertIn("biology", preview.user_fit_reason(match).lower())

    def test_beginner_generalist_keeps_language_data_and_rejects_specialists(self):
        rows = [
            opportunity("Portuguese Language Data Contributor", 1, "Language", "Portuguese"),
            opportunity("English Language Expert (Generalist)", 2, "Language", "English"),
            opportunity("Generalist AI Trainer", 3, "General"),
            opportunity("Software Engineer", 4, "Engineering"),
            opportunity("Financial Analyst", 5, "Finance"),
            opportunity("Biology Specialist", 6, "Biology"),
        ]

        _, context, ranked = build_ranked(BEGINNER_PROFILE, rows)
        titles = [match["display_title"] for match in ranked]
        details = by_title(context)

        self.assertIn("Portuguese Language Data Contributor", titles)
        self.assertIn("English Language Expert (Generalist)", titles)
        self.assertEqual(details["Portuguese Language Data Contributor"]["matched_languages"], ["portuguese"])
        for title in ("Software Engineer", "Financial Analyst", "Biology Specialist"):
            self.assertNotIn(title, titles)
            self.assertFalse(details[title]["primary_recommendation_eligible"])

    def test_pipeline_status_does_not_change_relevance_or_order(self):
        rows = [
            opportunity("Software Engineer", 1, "Engineering"),
            opportunity("Backend Developer", 2, "Engineering"),
            opportunity("Finance & Python Expert", 3, "Finance"),
        ]
        _, context, ranked = build_ranked(SOFTWARE_PROFILE, rows)
        baseline = [
            (match["display_title"], match["score"], match["ranking_score"])
            for match in ranked
        ]

        for status in (None, "saved", "applied", "assessment_started", "not_interested", "saved"):
            overlaid = deepcopy(context)
            for section in preview.SECTION_ORDER:
                for match in overlaid["matches"][section]:
                    match["pipeline_status"] = status
            reranked = app.build_ranked_presentation_matches(overlaid)
            self.assertEqual(
                [(match["display_title"], match["score"], match["ranking_score"]) for match in reranked],
                baseline,
            )


if __name__ == "__main__":
    unittest.main()
