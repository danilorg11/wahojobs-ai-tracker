import unittest

from scripts.semantic_matching_shadow_report import build_report


class SemanticMatchingShadowReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = build_report()

    def test_offline_architecture_acceptance(self):
        self.assertTrue(self.report["acceptance"]["passed"])
        self.assertTrue(all(self.report["acceptance"].values()))
        self.assertEqual(
            self.report["verdict"], "SEMANTIC MATCHING SHADOW SEAM READY"
        )
        self.assertEqual(
            self.report["invariant_proof"]["input_survivor_count"],
            self.report["invariant_proof"]["output_ranked_count"],
        )
        self.assertEqual(
            self.report["invariant_proof"]["semantic_exclusion_count"], 0
        )
        self.assertEqual(
            self.report["invariant_proof"][
                "semantic_negative_ranking_factor_count"
            ],
            0,
        )

    def test_report_is_local_only_and_live_evaluation_remains_unexecuted(self):
        self.assertEqual(
            set(self.report["offline_controls"].values()), {0}
        )
        self.assertEqual(self.report["runtime_shadow_imports"], [])
        proposal = self.report["bounded_live_evaluation_proposal"]
        self.assertFalse(proposal["executed"])
        self.assertEqual(proposal["maximum_profile_opportunity_comparisons"], 36)
        self.assertTrue(proposal["explicit_profile_data_authorization_required"])


if __name__ == "__main__":
    unittest.main()
