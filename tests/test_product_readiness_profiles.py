import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import profile_matching_coverage as coverage
from wahojobs.matching.languages import language_eligibility
from wahojobs.profiles.canonical import (
    canonical_to_matcher_profile,
    validate_canonical_profile,
)
from wahojobs.profiles.normalizer import BaselineHeuristicProfileNormalizer
from wahojobs.profiles.review import apply_reviewed_profile


SUITE_PATH = ROOT / "tests" / "fixtures" / "product_readiness_personas_v1.json"
EVALUATED_AT = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


class ProductReadinessProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = coverage.load_persona_suite(SUITE_PATH)

    def test_suite_has_broad_materially_different_personas(self):
        personas = self.suite["personas"]
        self.assertGreaterEqual(len(personas), 24)
        ids = {persona["persona_id"] for persona in personas}
        required_archetypes = {
            "beginner_bilingual_generalist",
            "monolingual_entry_level",
            "multilingual_language_specialist",
            "virtual_assistant_admin",
            "customer_support_worker",
            "non_phone_preference",
            "transcription_data_entry",
            "writer_editor",
            "teacher_tutor",
            "software_engineer",
            "qa_software_tester",
            "data_analyst",
            "finance_accounting",
            "legal_professional",
            "healthcare_credentialed",
            "healthcare_interested_uncredentialed",
            "biology_researcher",
            "chemistry_scientist",
            "marketing_social_media",
            "graphic_ux_designer",
            "project_operations_manager",
            "student_no_experience",
            "experienced_career_changer",
            "degree_without_relevant_experience",
            "restrictive_location_non_us",
            "uneven_multilingual_proficiency",
        }
        self.assertTrue(required_archetypes <= ids)
        self.assertEqual(len(ids), len(personas))
        countries = {persona["review"].get("country") for persona in personas}
        languages = {
            language["language"]
            for persona in personas
            for language in persona["review"].get("languages", [])
        }
        self.assertGreaterEqual(len(countries - {None, ""}), 12)
        self.assertGreaterEqual(len(languages), 10)
        scopes = {persona["launch_scope"] for persona in personas}
        self.assertEqual(
            scopes,
            {"core", "adjacent", "outside_initial_launch_scope"},
        )
        self.assertEqual(
            [
                persona["persona_id"]
                for persona in personas
                if persona["launch_scope"] == "outside_initial_launch_scope"
            ],
            ["security_cleared_analyst"],
        )

    def test_every_persona_builds_a_valid_reviewed_canonical_profile(self):
        for persona in self.suite["personas"]:
            with self.subTest(persona=persona["persona_id"]):
                canonical = coverage.canonical_profile_for_persona(persona)
                self.assertTrue(validate_canonical_profile(canonical))
                self.assertTrue(canonical["provenance"]["reviewed"])
                self.assertEqual(
                    canonical["provenance"]["original_text"],
                    persona["raw_input"],
                )
                self.assertEqual(
                    canonical["provenance"]["field_sources"]["location.country"]["explicit"],
                    True,
                )
                self.assertIn(
                    canonical["provenance"]["field_sources"]["location.country"]["source"],
                    {"user_confirmation", "user_correction"},
                )
                if canonical["languages"]:
                    self.assertTrue(
                        canonical["provenance"]["field_sources"]["languages[0].language"]["explicit"]
                    )

    def test_expected_matching_principles_are_defined_for_every_persona(self):
        for persona in self.suite["personas"]:
            with self.subTest(persona=persona["persona_id"]):
                self.assertTrue(persona["expected_strong_families"])
                self.assertTrue(persona["excluded_specialist_families"])
                self.assertTrue(persona["language_requirements"])
                self.assertTrue(persona["location_constraints"])
                self.assertTrue(persona["explanation_expectations"])
                self.assertIn(
                    persona["launch_scope"],
                    {"core", "adjacent", "outside_initial_launch_scope"},
                )
                self.assertIsInstance(persona["credential_requirements"], list)
                self.assertTrue(persona["review"].get("country"))
                self.assertTrue(persona["review"].get("languages"))
                self.assertIn(
                    persona["review"].get("credential_status"),
                    {"absent", "explicit", "in_progress"},
                )

    def test_reviewed_corrections_replace_conflicting_parser_evidence(self):
        raw = (
            "I live in Brazil, speak Spanish, and use Python. "
            "I want software work."
        )
        parsed = BaselineHeuristicProfileNormalizer().normalize(
            raw,
            "short_paragraph",
        ).canonical_profile
        reviewed = apply_reviewed_profile(
            parsed,
            {
                "country": "Portugal",
                "languages": [
                    {"language": "Portuguese", "proficiency": "native"}
                ],
                "education_level": "bachelor",
                "education_fields": ["English"],
                "credential_status": "absent",
                "professional_domains": ["writing"],
                "skills": ["editing"],
                "target_opportunity_types": ["writing evaluation"],
                "remote": True,
            },
        )
        projected = canonical_to_matcher_profile(reviewed)

        self.assertEqual(projected["country"], "Portugal")
        self.assertEqual(projected["languages"], ["Portuguese"])
        self.assertEqual(projected["skills"], ["editing"])
        self.assertNotIn("Spanish", projected["summary"])
        self.assertNotIn("Python", projected["summary"])
        self.assertNotIn("software", projected["summary"].casefold())
        self.assertEqual(reviewed["provenance"]["original_text"], raw)
        self.assertNotIn(
            "skills.normalized[1]",
            reviewed["provenance"]["field_sources"],
        )

    def test_declared_languages_are_eligible_and_unsupported_language_is_not(self):
        known_role_languages = {
            "Arabic",
            "English",
            "French",
            "German",
            "Hindi",
            "Portuguese",
            "Romanian",
            "Spanish",
            "Tagalog",
        }
        checked = 0
        for persona in self.suite["personas"]:
            profile = canonical_to_matcher_profile(
                coverage.canonical_profile_for_persona(persona)
            )
            for language in profile["languages"]:
                if language not in known_role_languages:
                    continue
                with self.subTest(persona=persona["persona_id"], language=language):
                    eligibility = language_eligibility(
                        profile,
                        f"{language} Language Specialist",
                    )
                    self.assertTrue(eligibility.eligible_for_personalized)
                    checked += 1
            if "Thai" not in profile["languages"]:
                self.assertFalse(
                    language_eligibility(
                        profile,
                        "Thai Language Specialist",
                    ).eligible_for_personalized
                )
        self.assertGreaterEqual(checked, 28)

    def test_synthetic_coverage_report_is_machine_readable_and_title_agnostic(self):
        rows = [
            synthetic_row(1, "English Language Data Contributor", "Language"),
            synthetic_row(2, "Portuguese Language Reviewer", "Language"),
            synthetic_row(3, "Python Software Engineer", "Software Engineering"),
            synthetic_row(4, "Biology Research Evaluator", "Biology"),
            synthetic_row(5, "Medical Specialist", "Healthcare"),
        ]
        report = coverage.evaluate_suite(
            self.suite,
            rows,
            evaluated_at=EVALUATED_AT,
            limit=10,
            desired_matches=3,
            inventory={"active_rows": len(rows), "database_mode": "synthetic"},
        )
        rendered = json.loads(json.dumps(report, sort_keys=True))

        self.assertEqual(rendered["persona_count"], len(self.suite["personas"]))
        self.assertEqual(rendered["inventory"]["database_mode"], "synthetic")
        for persona in rendered["personas"]:
            self.assertIn("top_occupational_families", persona)
            self.assertIn("specialist_mismatches", persona)
            self.assertIn("unsupported_language_leaks", persona)
            self.assertIn("location_leaks", persona)
            self.assertIn("credential_leaks", persona)
            self.assertIn("explanation_quality_findings", persona)
            self.assertIn("fallback_usage", persona)
            self.assertIn("coverage_funnel", persona)
            self.assertIn("coverage_causes", persona)
            self.assertIn("launch_scope", persona)
            self.assertIn("synthetic_strong_family_contract", persona)
            self.assertIn("live_inventory_strong_family_coverage", persona)
            self.assertIn("fallback_only", persona)
            self.assertIn("explanation_contract_violations", persona)
            self.assertIn(
                persona["coverage_diagnosis"],
                {
                    "adequate",
                    "inventory_shortage",
                    "language_shortage",
                    "location_shortage",
                    "credential_constraint",
                    "source_freshness_shortage",
                    "genuine_profile_constraint",
                    "matcher_or_ranking_gap",
                    "outside_launch_scope",
                },
            )

    def test_coverage_diagnosis_identifies_each_funnel_stage(self):
        persona = {"review": {}}
        base = {
            "professional_domain_hard_gate_applied": False,
            "eligible_for_personalized": True,
            "unsupported_languages": [],
            "location_eligibility_status": "compatible",
            "actionability_cap_reasons": [],
            "opportunity_trust_status": "trusted",
            "primary_recommendation_eligible": True,
        }

        scenarios = {
            "inventory_shortage": [],
            "language_shortage": [
                {**base, "eligible_for_personalized": False}
                for _ in range(3)
            ],
            "location_shortage": [
                {**base, "location_eligibility_status": "incompatible"}
                for _ in range(3)
            ],
            "credential_constraint": [
                {**base, "actionability_cap_reasons": ["explicit_credential_incompatibility"]}
                for _ in range(3)
            ],
            "source_freshness_shortage": [
                {**base, "opportunity_trust_status": "stale_source"}
                for _ in range(3)
            ],
            "matcher_or_ranking_gap": [
                {**base, "primary_recommendation_eligible": False}
                for _ in range(3)
            ],
        }
        for expected, matches in scenarios.items():
            with self.subTest(expected=expected):
                funnel = coverage.coverage_funnel(
                    matches, total_inventory_candidates=len(matches)
                )
                diagnosis = coverage.coverage_diagnosis(
                    persona, funnel, 3
                )
                self.assertEqual(diagnosis["primary_cause"], expected)

        constrained = coverage.coverage_diagnosis(
            {"review": {"no_degree": True}},
            coverage.coverage_funnel([], total_inventory_candidates=0),
            3,
        )
        self.assertTrue(constrained["flags"]["genuine_profile_constraint"])

    def test_persona_family_contracts_use_supplied_synthetic_inventory(self):
        for persona in self.suite["personas"]:
            canonical = coverage.canonical_profile_for_persona(persona)
            contract = coverage.evaluate_synthetic_contract(
                persona,
                canonical,
                evaluated_at=EVALUATED_AT,
            )
            with self.subTest(persona=persona["persona_id"]):
                self.assertTrue(contract["contract_passed"], contract)
                self.assertEqual(contract["prohibited_admissions"], [])
                if persona["launch_scope"] == "outside_initial_launch_scope":
                    self.assertTrue(contract["outside_launch_scope"])
                    continue
                self.assertTrue(contract["strong_admitted"], contract)
                self.assertTrue(
                    set(contract["strong_sections"])
                    <= set(coverage.PERSONALIZED_SECTIONS)
                )
                self.assertTrue(contract["fallback_admitted"], contract)
                self.assertTrue(contract["strong_before_fallback"], contract)
                self.assertEqual(contract["explanation_violations"], [])

    def test_explanations_use_supported_profile_evidence_without_freshness_language(self):
        for persona in self.suite["personas"]:
            if persona["launch_scope"] == "outside_initial_launch_scope":
                continue
            canonical = coverage.canonical_profile_for_persona(persona)
            contract = coverage.evaluate_synthetic_contract(
                persona,
                canonical,
                evaluated_at=EVALUATED_AT,
            )
            with self.subTest(persona=persona["persona_id"]):
                self.assertEqual(contract["explanation_violations"], [])


def synthetic_row(job_id, title, expertise):
    observed = EVALUATED_AT.isoformat()
    return {
        "job_id": job_id,
        "title": title,
        "canonical_title": title,
        "location": "Remote",
        "url": f"https://example.test/jobs/{job_id}",
        "department": expertise,
        "expertise": expertise,
        "commitment": "Freelance",
        "source_category": expertise,
        "source": "Synthetic Inventory",
        "source_slug": "synthetic-inventory",
        "source_tier": "core",
        "inventory_model": "live_feed",
        "market_count_policy": "count_live",
        "opportunity_kind": "live_posting",
        "availability_basis": "api_feed",
        "include_in_live_market_estimate": 1,
        "canonical_opportunity_id": job_id,
        "job_is_active": True,
        "canonical_is_active": True,
        "job_last_seen_at": observed,
        "latest_successful_source_run_at": observed,
        "source_run_started_at": observed,
        "source_run_id": job_id,
        "source_run_qualifies": True,
        "language": None,
        "language_locale": None,
        "required_languages": None,
        "description": "",
    }


def prohibited_specialist_title(family):
    titles = {
        "advanced science": "Advanced Chemistry Scientist",
        "coding": "Software Coding Specialist",
        "licensed clinical medicine": "Licensed Clinical Medicine Specialist",
        "licensed healthcare": "Licensed Healthcare Physician",
        "licensed work": "Licensed Healthcare Specialist",
        "non-English language": "Thai Language Specialist",
        "phone support": "Phone Customer Support Specialist",
        "unsupported language": "Thai Language Specialist",
        "us-only": "United States Only Specialist",
    }
    return titles.get(family, f"{family.title()} Specialist")


if __name__ == "__main__":
    unittest.main()
