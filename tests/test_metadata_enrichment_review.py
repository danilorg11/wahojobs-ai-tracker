import csv
import json
import tempfile
import unittest
from pathlib import Path

import scripts.matching_metadata_enrichment_review as enrichment


class MetadataEnrichmentReviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.summary = enrichment.build_review_batch()
        cls.by_case_id = {row["case_id"]: row for row in cls.rows}

    def test_includes_sparse_human_reviewed_fixture_snapshots(self):
        focused = set(enrichment.FOCUSED_CASE_ORDER)

        self.assertTrue(focused.issubset(self.by_case_id))
        self.assertGreaterEqual(len(self.rows), len(focused))
        self.assertTrue(all(row["case_id"] for row in self.rows))

    def test_dataannotation_bilingual_cases_flag_evergreen_metadata_inconsistency(self):
        for case_id in {
            "portuguese_english_reviewer_010",
            "multilingual_translator_010",
            "beginner_bilingual_no_degree_005",
        }:
            row = self.by_case_id[case_id]
            self.assertEqual(row["fidelity_classification"], "fixture_missing_structured_metadata")
            self.assertEqual(row["primary_issue"], "dataannotation_bilingual_evergreen_archetype_gap")
            self.assertIn("opportunity_kind", row["suspicious_fields"])
            self.assertIn("availability_basis", row["suspicious_fields"])
            self.assertIn("language", row["suspicious_fields"])
            self.assertIn("evergreen_application", row["internal_inconsistencies"])

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
        self.assertEqual(row["source_category"], "Unknown")
        self.assertEqual(row["expertise"], "Unknown")
        self.assertEqual(row["department"], "Unknown")
        self.assertEqual(row["proposed_source_category"], "")

    def test_location_case_does_not_parse_title_location_into_structured_location(self):
        row = self.by_case_id["phd_history_researcher_013"]

        self.assertEqual(row["primary_issue"], "location_actionability_metadata_gap")
        self.assertEqual(row["location"], "Remote")
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

    def test_baseline_metrics_remain_unchanged(self):
        baseline = self.summary["baseline"]
        self.assertEqual(
            (
                baseline["label_agreement"],
                baseline["section_agreement"],
                baseline["full_agreement"],
                baseline["total"],
            ),
            (9, 17, 8, 30),
        )


if __name__ == "__main__":
    unittest.main()
