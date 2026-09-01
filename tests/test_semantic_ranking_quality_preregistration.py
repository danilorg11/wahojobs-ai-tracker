import importlib.util
import ast
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "semantic_ranking_quality_preregistration.py"
FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "semantic_ranking_quality_benchmark_v1_preregistration.json"
)
SPEC = importlib.util.spec_from_file_location("ranking_preregistration", SCRIPT)
preregistration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preregistration)


class SemanticRankingQualityPreregistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_fixture_validates_and_reproduces(self):
        preregistration.validate_preregistration(self.fixture)
        rebuilt = preregistration.build_preregistration(
            self.fixture["captured_at"]
        )
        self.assertEqual(rebuilt, self.fixture)

    def test_complete_corpus_and_quality_cohort_are_frozen(self):
        population = self.fixture["population_selection"]
        self.assertEqual(population["case_count"], 30)
        self.assertEqual(population["profile_count"], 9)
        self.assertEqual(population["ranking_candidate_judgment_count"], 24)
        self.assertEqual(population["comparative_profile_count"], 4)
        self.assertEqual(population["ranking_quality_judgment_count"], 14)
        self.assertEqual(population["evaluable_ranking_quality_pair_count"], 14)
        self.assertEqual(population["safety_only_cross_label_pair_count"], 16)

    def test_same_label_pairs_are_not_quality_wins_or_losses(self):
        pairs = self.fixture["human_ground_truth"]["cross_label_pairs"]
        self.assertEqual(len(pairs), 30)
        self.assertFalse(any(item["same_label_pair"] for item in pairs))

    def test_provider_templates_are_blind(self):
        provider = self.fixture["provider_blinding"]
        self.assertEqual(
            preregistration.forbidden_provider_paths(
                provider["provider_request_templates"]
            ),
            [],
        )
        serialized = preregistration.canonical_json(
            provider["provider_request_templates"]
        )
        for forbidden in (
            "human_relevance",
            "legacy_rank",
            "legacy_score",
            "expected_ordering",
            "known_legacy_inversion",
            "metric_outcomes",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_no_semantic_output_or_packet_is_claimed(self):
        self.assertFalse(self.fixture["repository"]["semantic_outputs_in_artifact"])
        readiness = self.fixture["packet_readiness"]
        self.assertEqual(readiness["semantic_packet_available_judgments"], 0)
        self.assertFalse(readiness["packet_provisioning_performed"])
        validation = self.fixture["validation"]
        self.assertEqual(validation["provider_or_network_calls"], 0)
        self.assertEqual(validation["semantic_ranking_calls"], 0)
        self.assertEqual(validation["semantic_packet_provisioning_calls"], 0)

    def test_generator_has_no_network_provider_or_semantic_ranking_import(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        forbidden_prefixes = (
            "http",
            "requests",
            "urllib",
            "openai",
            "wahojobs.matching.semantic_shadow",
            "wahojobs.opportunity_semantic_shadow",
        )
        self.assertFalse(
            sorted(
                module
                for module in imported
                if module.startswith(forbidden_prefixes)
            )
        )


if __name__ == "__main__":
    unittest.main()
