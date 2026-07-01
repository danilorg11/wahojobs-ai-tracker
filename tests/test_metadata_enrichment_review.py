import csv
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import scripts.matching_metadata_enrichment_review as enrichment
import matching_quality_report as benchmark


class MetadataEnrichmentReviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.summary = enrichment.build_review_batch()
        cls.by_case_id = {row["case_id"]: row for row in cls.rows}
        cls.review_rows = enrichment.read_review_csv(enrichment.CSV_PATH)
        cls.fixture = benchmark.load_fixture()
        cls.profiles = benchmark.load_benchmark_profiles(cls.fixture)
        cls.apply_plan = enrichment.build_apply_plan(cls.review_rows, cls.fixture, cls.profiles)

    def test_includes_sparse_human_reviewed_fixture_snapshots(self):
        focused = set(enrichment.FOCUSED_CASE_ORDER)

        self.assertTrue(focused.issubset(self.by_case_id))
        self.assertGreaterEqual(len(self.rows), len(focused))
        self.assertTrue(all(row["case_id"] for row in self.rows))

    def test_dataannotation_bilingual_cases_keep_only_unresolved_language_gap(self):
        for case_id in {
            "portuguese_english_reviewer_010",
            "multilingual_translator_010",
            "beginner_bilingual_no_degree_005",
        }:
            row = self.by_case_id[case_id]
            self.assertEqual(row["fidelity_classification"], "fixture_missing_structured_metadata")
            self.assertEqual(row["primary_issue"], "dataannotation_bilingual_evergreen_archetype_gap")
            self.assertEqual(row["source_category"], "Bilingual")
            self.assertEqual(row["expertise"], "Bilingual")
            self.assertEqual(row["department"], "Bilingual")
            self.assertEqual(row["opportunity_kind"], "evergreen_application")
            self.assertEqual(row["availability_basis"], "evergreen_application")
            self.assertIn("language", row["suspicious_fields"])
            self.assertNotIn("opportunity_kind", row["suspicious_fields"])
            self.assertNotIn("availability_basis", row["suspicious_fields"])
            self.assertEqual(row["proposed_source_category"], "")

    def test_unsafe_title_only_candidates_are_diagnostic_only(self):
        row = self.by_case_id["generalist_no_degree_013"]

        self.assertEqual(row["fidelity_classification"], "ambiguous_identity")
        self.assertEqual(row["primary_issue"], "unsafe_meridial_pci_title_variants")
        self.assertEqual(row["unsafe_title_or_similarity_candidates"], "yes")
        self.assertGreater(int(row["diagnostic_candidate_count"]), 0)
        for field in enrichment.PROPOSED_FIELDS:
            self.assertEqual(row[field], "")

    def test_generalist_pci_case_is_not_tied_to_live_row(self):
        row = self.by_case_id["generalist_no_degree_013"]

        self.assertEqual(row["review_decision"], "")
        self.assertEqual(row["external_id"], "")
        self.assertEqual(row["source_hash"], "")
        self.assertEqual(row["exact_stable_live_match_available"], "no")
        self.assertIn("diagnostic only", row["ambiguity_reason"])

    def test_lawyer_ip_case_is_legal_gap_without_auto_taxonomy(self):
        row = self.by_case_id["lawyer_002"]

        self.assertEqual(row["primary_issue"], "legal_ip_metadata_gap")
        self.assertEqual(row["source_category"], "Law")
        self.assertEqual(row["expertise"], "Intellectual Property")
        self.assertEqual(row["department"], "Law")
        self.assertEqual(row["proposed_source_category"], "")

    def test_location_case_does_not_parse_title_location_into_structured_location(self):
        row = self.by_case_id["phd_history_researcher_013"]

        self.assertEqual(row["primary_issue"], "location_actionability_metadata_gap")
        self.assertEqual(row["location"], "Remote - Morocco")
        self.assertEqual(row["proposed_location"], "")
        self.assertIn("Morocco", row["internal_inconsistencies"])

    def test_product_policy_case_is_included_without_decision_change(self):
        row = self.by_case_id["phd_history_researcher_011"]

        self.assertEqual(row["fidelity_classification"], "product_policy_not_represented")
        self.assertEqual(row["human_label"], "false_positive")
        self.assertEqual(row["human_expected_section"], "exclude")
        self.assertEqual(row["matching_changes_blocked_until_review"], "yes")

    def test_generated_csv_has_reviewer_fields_and_does_not_mutate_fixture(self):
        fixture_path = Path("tests/fixtures/matching_golden_set.json")
        before = fixture_path.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "review.csv"
            html_path = Path(temp_dir) / "review.html"
            summary_path = Path(temp_dir) / "summary.md"
            enrichment.write_csv(self.rows, csv_path)
            enrichment.write_html(self.rows, html_path)
            enrichment.write_summary(self.rows, self.summary, summary_path)

            with csv_path.open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                generated_rows = list(reader)
            self.assertIn("review_decision", reader.fieldnames)
            self.assertIn("proposed_source_category", reader.fieldnames)
            self.assertIn("proposed_enrichment_notes", reader.fieldnames)
            self.assertEqual(len(generated_rows), len(self.rows))

        after = fixture_path.read_text(encoding="utf-8")
        self.assertEqual(json.loads(before), json.loads(after))

    def test_baseline_metrics_reflect_applied_metadata_enrichment(self):
        baseline = self.summary["baseline"]
        self.assertEqual(
            (
                baseline["label_agreement"],
                baseline["section_agreement"],
                baseline["full_agreement"],
                baseline["total"],
            ),
            (26, 29, 26, 30),
        )

    def test_apply_dry_run_reads_reviewed_csv_counts(self):
        self.assertEqual(self.apply_plan["rows_read"], 22)
        self.assertEqual(len(self.apply_plan["approved_rows"]), 20)
        self.assertEqual(len(self.apply_plan["non_apply_rows"]), 2)
        self.assertEqual(len(self.apply_plan["rows_with_proposed_metadata_fields"]), 20)
        self.assertEqual(self.apply_plan["changes_by_case"], {})
        self.assertEqual(self.apply_plan["changed_predictions"], [])
        self.assertEqual(self.apply_plan["before_metrics"], self.apply_plan["after_metrics"])
        self.assertEqual(self.apply_plan["validation_errors"], [])
        self.assertIn("phd_history_researcher_011", self.apply_plan["non_apply_rows"])
        self.assertIn("generalist_no_degree_013", self.apply_plan["non_apply_rows"])

    def test_non_approving_rows_do_not_mutate_snapshot_fields(self):
        original = {case["case_id"]: case for case in self.fixture["cases"]}
        updated = {case["case_id"]: case for case in self.apply_plan["updated_fixture"]["cases"]}
        for case_id in {"phd_history_researcher_011", "generalist_no_degree_013"}:
            self.assertEqual(
                updated[case_id]["matcher_input_snapshot"]["matcher_input"],
                original[case_id]["matcher_input_snapshot"]["matcher_input"],
            )
            self.assertEqual(
                updated[case_id]["matcher_input_snapshot"].get("snapshot_metadata"),
                original[case_id]["matcher_input_snapshot"].get("snapshot_metadata"),
            )

    def test_approved_proposed_metadata_maps_to_matcher_input(self):
        updated = {case["case_id"]: case for case in self.apply_plan["updated_fixture"]["cases"]}
        lawyer = updated["lawyer_002"]["matcher_input_snapshot"]["matcher_input"]
        self.assertEqual(lawyer["source_category"], "Law")
        self.assertEqual(lawyer["expertise"], "Intellectual Property")
        self.assertEqual(lawyer["department"], "Law")

        bilingual = updated["portuguese_english_reviewer_010"]["matcher_input_snapshot"]["matcher_input"]
        self.assertEqual(bilingual["opportunity_kind"], "evergreen_application")
        self.assertEqual(bilingual["market_count_policy"], "report_separately")
        self.assertEqual(bilingual["availability_basis"], "evergreen_application")
        self.assertIsNone(bilingual["required_languages"])

        metadata = updated["lawyer_002"]["matcher_input_snapshot"]["snapshot_metadata"]
        self.assertIs(metadata["metadata_enrichment_reviewed"], True)
        self.assertEqual(metadata["metadata_enrichment_decision"], "approve_metadata_update")
        self.assertEqual(metadata["metadata_enrichment_source"], "exports/matching_metadata_enrichment_review.csv")

    def test_stable_id_fields_are_not_applied_from_approve_metadata_update(self):
        rows = deepcopy(self.review_rows)
        rows[0]["proposed_url"] = "https://example.test/not-safe"
        plan = enrichment.build_apply_plan(rows, self.fixture, self.profiles)

        self.assertTrue(
            any("proposes stable IDs under approve_metadata_update" in error for error in plan["validation_errors"])
        )
        updated = {case["case_id"]: case for case in plan["updated_fixture"]["cases"]}
        self.assertEqual(
            updated[rows[0]["case_id"]]["matcher_input_snapshot"]["matcher_input"]["url"],
            "",
        )

    def test_apply_plan_preserves_human_decision_fields(self):
        original = {case["case_id"]: case for case in self.fixture["cases"]}
        updated = {case["case_id"]: case for case in self.apply_plan["updated_fixture"]["cases"]}
        for case_id in self.apply_plan["approved_rows"]:
            self.assertEqual(
                enrichment.decision_fields(updated[case_id]),
                enrichment.decision_fields(original[case_id]),
            )

    def test_missing_case_id_fails_conservatively(self):
        rows = deepcopy(self.review_rows)
        rows[0]["case_id"] = "missing_case_id"
        plan = enrichment.build_apply_plan(rows, self.fixture, self.profiles)

        self.assertIn("missing_case_id", plan["fixture_cases_missing"])
        self.assertTrue(any("missing_case_id is not present" in error for error in plan["validation_errors"]))

    def test_invalid_review_decision_fails_validation(self):
        rows = deepcopy(self.review_rows)
        rows[0]["review_decision"] = "please_apply"
        plan = enrichment.build_apply_plan(rows, self.fixture, self.profiles)

        self.assertTrue(any("invalid review_decision" in error for error in plan["validation_errors"]))

    def test_non_apply_decision_with_metadata_field_is_rejected(self):
        rows = deepcopy(self.review_rows)
        row = next(item for item in rows if item["case_id"] == "phd_history_researcher_011")
        row["proposed_source_category"] = "Healthcare & Medical"
        plan = enrichment.build_apply_plan(rows, self.fixture, self.profiles)

        self.assertTrue(
            any(
                "phd_history_researcher_011 has proposed metadata fields under non-apply decision"
                in error
                for error in plan["validation_errors"]
            )
        )

    def test_apply_dry_run_leaves_fixture_file_unchanged(self):
        fixture_path = Path("tests/fixtures/matching_golden_set.json")
        before = fixture_path.read_text(encoding="utf-8")
        enrichment.build_apply_plan(self.review_rows, self.fixture, self.profiles)
        after = fixture_path.read_text(encoding="utf-8")

        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
