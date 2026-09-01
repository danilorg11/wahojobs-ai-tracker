import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import matching_foundation_report as report


class MatchingFoundationReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.golden = report.load_json(report.DEFAULT_GOLDEN_PATH)
        cls.evaluation = report.load_json(report.DEFAULT_EVALUATION_PATH)
        cls.inventory = report.load_json(report.DEFAULT_INVENTORY_SNAPSHOT_PATH)

    def test_manifest_separates_approved_truth_from_derived_regressions(self):
        summary = report.validate_evaluation_set(self.golden, self.evaluation)

        self.assertEqual(summary["golden_cases_total"], 160)
        self.assertEqual(summary["approved_relevance_cases"], 30)
        self.assertEqual(summary["draft_relevance_cases_excluded"], 130)
        self.assertEqual(summary["approved_self_contained_snapshots"], 30)
        self.assertEqual(summary["derived_eligibility_regressions"], 6)
        self.assertEqual(summary["human_approved_eligibility_interpretations"], 3)
        self.assertEqual(summary["human_approved_relationship_judgments"], 3)
        self.assertEqual(summary["approved_judgments_total"], 36)
        self.assertTrue(summary["draft_exclusion_verified"])
        self.assertTrue(summary["approval_metadata_verified"])

    def test_manifest_refuses_draft_promotion_or_missing_approval(self):
        promoted = copy.deepcopy(self.evaluation)
        promoted["authoritative_relevance_case_ids"].append(
            "generalist_no_degree_001"
        )
        with self.assertRaises(report.EvaluationHarnessError):
            report.validate_evaluation_set(self.golden, promoted)

        unapproved = copy.deepcopy(self.golden)
        human = next(
            item
            for item in unapproved["cases"]
            if item.get("label_source") == "human_reviewed"
        )
        human.pop("approved_at")
        with self.assertRaises(report.EvaluationHarnessError):
            report.validate_evaluation_set(unapproved, self.evaluation)

    def test_inventory_snapshot_is_closed_and_fingerprint_bound(self):
        verified = report.validate_inventory_snapshot(self.inventory)
        self.assertTrue(verified["snapshot_fingerprint_verified"])
        self.assertTrue(verified["observation_fingerprint_verified"])

        tampered = copy.deepcopy(self.inventory)
        tampered["observations"]["legacy_shortlist"]["active_inventory_rows"] += 1
        with self.assertRaises(report.EvaluationHarnessError):
            report.validate_inventory_snapshot(tampered)

    def test_relationship_classifier_requires_stable_identity_for_exact_duplicate(self):
        base = {
            "canonical_opportunity_id": 1,
            "company": "Example",
            "title": "Biology Expert",
            "location": "Remote",
            "department": "Biology",
            "expertise": "Biology",
            "commitment": "$100/hr; PhD",
            "source_category": "Biology",
            "language": None,
            "language_locale": None,
            "opportunity_kind": "live_posting",
            "external_id": "posting-1",
            "source_hash": "source-1",
            "url": "https://example.test/posting-1",
        }
        same_visible_different_identity = {
            **base,
            "external_id": "posting-2",
            "source_hash": "source-2",
            "url": "https://example.test/posting-2",
        }
        exact_duplicate = dict(base)
        location_variant = {**base, "location": "United Kingdom"}
        material = {
            **base,
            "canonical_opportunity_id": 2,
            "department": "Plant Biology",
            "expertise": "Plant Biology",
            "commitment": "$55/hr; Master's",
            "source_category": "Plant Biology",
        }

        self.assertEqual(
            report.classify_relationship(base, same_visible_different_identity),
            ("unresolved_related", []),
        )
        self.assertEqual(
            report.classify_relationship(base, exact_duplicate),
            ("exact_duplicate", []),
        )
        self.assertEqual(
            report.classify_relationship(base, location_variant),
            ("eligibility_variant", ["location"]),
        )
        relationship, differences = report.classify_relationship(base, material)
        self.assertEqual(relationship, "material_variant")
        self.assertEqual(
            differences,
            ["commitment", "department", "expertise", "source_category"],
        )

    def test_default_report_is_hermetic_and_uses_no_workspace_database(self):
        with patch.object(
            report,
            "open_immutable_database",
            side_effect=AssertionError("default report must not open SQLite"),
        ):
            data = report.build_report_data(
                golden_path=report.DEFAULT_GOLDEN_PATH,
                evaluation_path=report.DEFAULT_EVALUATION_PATH,
                inventory_snapshot_path=report.DEFAULT_INVENTORY_SNAPSHOT_PATH,
            )

        self.assertEqual(data["authority"]["approved_relevance_cases"], 30)
        self.assertEqual(data["authority"]["draft_relevance_cases_excluded"], 130)
        self.assertEqual(data["eligibility"]["derived_regression_accuracy"], 1.0)
        self.assertEqual(data["eligibility"]["human_approved_accuracy"], 1.0)
        self.assertEqual(
            data["eligibility"]["human_approved_unknown_preservation"],
            1.0,
        )
        self.assertEqual(data["relationships"]["human_approved_accuracy"], 1.0)
        self.assertTrue(
            data["acceptance"]["foundation_measurement_authority_ready"]
        )
        self.assertFalse(
            data["acceptance"]["sample_limited_quality_targets_passed"]
        )
        self.assertEqual(
            data["acceptance"]["semantic_reranker_experiment_entry"],
            "not_assessed",
        )
        self.assertEqual(data["contracts"]["score_or_bucket_fields"], 0)
        self.assertEqual(
            data["contracts"]["opportunity_semantic_input"],
            "opportunity_semantic_input_v3",
        )
        self.assertEqual(
            data["contracts"]["opportunity_enrichment_derivation"],
            "opportunity_enrichment_derivation_v8",
        )

    def test_freshness_is_explicit_and_stale_fields_do_not_count_as_ready(self):
        data = report.build_report_data(
            golden_path=report.DEFAULT_GOLDEN_PATH,
            evaluation_path=report.DEFAULT_EVALUATION_PATH,
            inventory_snapshot_path=report.DEFAULT_INVENTORY_SNAPSHOT_PATH,
        )
        enrichment = data["enrichment"]

        self.assertEqual(enrichment["active_canonical_opportunities"], 2530)
        self.assertEqual(
            enrichment["freshness_counts"],
            {"current": 572, "stale": 1958, "missing": 0},
        )
        self.assertEqual(
            enrichment["freshness_reason_counts"],
            {
                "current": 572,
                "contract_recipe_stale": 1958,
                "source_changed": 0,
                "missing": 0,
            },
        )
        self.assertEqual(
            enrichment["source_input_status_counts"],
            {
                "current": 2530,
                "changed": 0,
                "not_comparable": 0,
                "missing": 0,
            },
        )
        self.assertEqual(
            enrichment["derivation_status_counts"],
            {
                "current": 0,
                "legacy_compatible": 572,
                "changed": 1958,
                "unknown": 0,
                "missing": 0,
            },
        )
        self.assertAlmostEqual(
            enrichment["current_enrichment_coverage"],
            572 / 2530,
        )
        self.assertEqual(enrichment["source_identity_ready_canonicals"], 2530)
        self.assertEqual(enrichment["rich_source_canonicals"], 531)
        self.assertEqual(enrichment["minimum_semantic_packet_ready_canonicals"], 0)
        self.assertEqual(enrichment["structured_requirement_signal_canonicals"], 413)

        role_family = next(
            item
            for item in enrichment["field_readiness"]
            if item["field_path"] == "attributes.role.role_family"
        )
        self.assertEqual(role_family["known_current"], 341)
        self.assertEqual(role_family["unknown_current"], 231)
        self.assertEqual(role_family["unavailable_stale"], 1958)
        self.assertEqual(role_family["unavailable_missing"], 0)

    def test_semantic_readiness_separates_mechanical_population_from_quality_approval(self):
        item = {
            "freshness": "current",
            "source_fact_flags": ["company_name", "canonical_title"],
            "known_fields": [
                report.CORE_SEMANTIC_IDENTITY_FIELDS[0],
                report.CORE_SEMANTIC_IDENTITY_FIELDS[1],
                report.CORE_SEMANTIC_CONTENT_FIELDS[0],
            ],
            "semantic_quality_status": "unreviewed",
        }

        self.assertTrue(report.mechanically_complete_semantic_packet(item))
        self.assertFalse(report.semantic_packet_ready(item))
        item["semantic_quality_status"] = "approved"
        self.assertTrue(report.semantic_packet_ready(item))

    def test_legacy_recall_is_reproduced_from_versioned_ordering(self):
        data = report.build_report_data(
            golden_path=report.DEFAULT_GOLDEN_PATH,
            evaluation_path=report.DEFAULT_EVALUATION_PATH,
            inventory_snapshot_path=report.DEFAULT_INVENTORY_SNAPSHOT_PATH,
            shortlist_size=32,
        )
        shortlist = data["shortlist"]
        self.assertEqual(shortlist["strong_cases"], 10)
        self.assertEqual(shortlist["strong_recalled"], 6)
        self.assertEqual(shortlist["strong_or_plausible_cases"], 17)
        self.assertEqual(shortlist["strong_or_plausible_recalled"], 7)
        self.assertAlmostEqual(shortlist["strong_recall_at_k"], 0.6)
        self.assertAlmostEqual(
            shortlist["strong_or_plausible_recall_at_k"],
            7 / 17,
        )

    def test_rendered_report_is_byte_deterministic_and_fingerprinted(self):
        first_data = report.build_report_data(
            golden_path=report.DEFAULT_GOLDEN_PATH,
            evaluation_path=report.DEFAULT_EVALUATION_PATH,
            inventory_snapshot_path=report.DEFAULT_INVENTORY_SNAPSHOT_PATH,
        )
        second_data = report.build_report_data(
            golden_path=report.DEFAULT_GOLDEN_PATH,
            evaluation_path=report.DEFAULT_EVALUATION_PATH,
            inventory_snapshot_path=report.DEFAULT_INVENTORY_SNAPSHOT_PATH,
        )
        first = report.render_markdown(first_data)
        second = report.render_markdown(second_data)

        self.assertEqual(first_data, second_data)
        self.assertEqual(first, second)
        self.assertNotIn("Generated:", first)
        self.assertIn("Combined input fingerprint:", first)
        self.assertIn("Foundation measurement authority ready: **yes**", first)
        self.assertIn("unresolved_related", first)


if __name__ == "__main__":
    unittest.main()
