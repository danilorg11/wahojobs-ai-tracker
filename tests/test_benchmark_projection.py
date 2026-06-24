import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import matching_quality_report as benchmark


def scored(**overrides):
    payload = {
        "eligible_for_personalized": True,
        "language_requirement_mode": "none",
        "detected_languages": [],
        "unsupported_languages": [],
        "language_eligibility_reason": "No explicit language requirement detected.",
        "reasons": [],
    }
    payload.update(overrides)
    return payload


class BenchmarkProjectionTests(unittest.TestCase):
    def test_unsupported_required_language_projects_to_false_positive(self):
        projection = benchmark.project_benchmark_prediction(
            34,
            "Strong",
            "explore_only",
            scored(
                eligible_for_personalized=False,
                language_requirement_mode="single",
                detected_languages=["catalan"],
                unsupported_languages=["catalan"],
                language_eligibility_reason="Explicit language requirement is not listed on profile.",
            ),
        )

        self.assertEqual(projection.evaluation_label, "false_positive")
        self.assertEqual(projection.evaluation_section, "exclude")
        self.assertEqual(projection.raw_score, 34)
        self.assertEqual(projection.raw_match_label, "Strong")
        self.assertEqual(projection.raw_section, "explore_only")
        self.assertEqual(projection.hard_gate_type, "language")

    def test_decisive_hard_domain_gate_projects_to_false_positive(self):
        projection = benchmark.project_benchmark_prediction(
            28,
            "Medium",
            "best_matches",
            scored(
                hard_gates=[
                    {
                        "type": "domain",
                        "status": "failed",
                        "decisive": True,
                        "reason": "Specialized medical credential is required.",
                    }
                ],
            ),
        )

        self.assertEqual(projection.evaluation_label, "false_positive")
        self.assertEqual(projection.evaluation_section, "exclude")
        self.assertEqual(projection.hard_gate_type, "domain")
        self.assertEqual(projection.hard_gate_reason, "Specialized medical credential is required.")

    def test_low_score_without_hard_gate_does_not_project_to_false_positive(self):
        projection = benchmark.project_benchmark_prediction(
            9,
            "Possible",
            "explore_only",
            scored(),
        )

        self.assertEqual(projection.evaluation_label, "weak")
        self.assertEqual(projection.evaluation_section, "explore_only")
        self.assertEqual(projection.hard_gate_type, "")

    def test_uncertain_gate_does_not_project_to_false_positive(self):
        projection = benchmark.project_benchmark_prediction(
            20,
            "Possible",
            "explore_only",
            scored(
                hard_gates=[
                    {
                        "type": "location",
                        "status": "unknown",
                        "decisive": True,
                        "reason": "Profile location is unknown.",
                    }
                ],
            ),
        )

        self.assertEqual(projection.evaluation_label, "weak")
        self.assertEqual(projection.evaluation_section, "explore_only")

    def test_ambiguous_language_gate_does_not_project_to_false_positive(self):
        projection = benchmark.project_benchmark_prediction(
            20,
            "Possible",
            "explore_only",
            scored(
                eligible_for_personalized=False,
                language_requirement_mode="ambiguous",
                detected_languages=["french", "german"],
                unsupported_languages=["french", "german"],
                language_eligibility_reason="Ambiguous multi-language opportunity has no profile language match.",
            ),
        )

        self.assertEqual(projection.evaluation_label, "weak")
        self.assertEqual(projection.evaluation_section, "explore_only")

    def test_normal_eligible_case_keeps_existing_mapping(self):
        projection = benchmark.project_benchmark_prediction(
            24,
            "Medium",
            "best_matches",
            scored(),
        )

        self.assertEqual(projection.evaluation_label, "plausible")
        self.assertEqual(projection.evaluation_section, "best_matches")
        self.assertEqual(projection.raw_match_label, "Medium")
        self.assertEqual(projection.raw_section, "best_matches")

    def test_fixture_validation_accepts_draft_and_human_reviewed(self):
        base = {
            "case_id": "case",
            "profile_id": "profile",
            "source": "Source",
            "title": "Title",
            "location": "Remote",
            "department": "General",
            "expertise": "General",
            "commitment": "",
            "opportunity_kind": "live_posting",
            "expected_label": "weak",
            "label_source": "codex_draft",
            "rationale": "Draft rationale.",
            "review_required": False,
            "expected_section": "explore_only",
        }

        benchmark.validate_case(dict(base), 1)
        reviewed = dict(base)
        reviewed["label_source"] = "human_reviewed"
        reviewed["human_notes"] = "Reviewed note."
        benchmark.validate_case(reviewed, 2)

    def test_human_reviewed_requires_notes_and_not_review_required(self):
        case = {
            "case_id": "case",
            "profile_id": "profile",
            "source": "Source",
            "title": "Title",
            "location": "Remote",
            "department": "General",
            "expertise": "General",
            "commitment": "",
            "opportunity_kind": "live_posting",
            "expected_label": "weak",
            "label_source": "human_reviewed",
            "rationale": "Reviewed rationale.",
            "review_required": False,
            "expected_section": "explore_only",
        }

        with self.assertRaises(SystemExit):
            benchmark.validate_case(dict(case), 1)
        case["human_notes"] = "Reviewed note."
        case["review_required"] = True
        with self.assertRaises(SystemExit):
            benchmark.validate_case(dict(case), 2)


if __name__ == "__main__":
    unittest.main()
