"""Portable contract tests: no v1 exports, human reviews, databases or API calls."""
import ast
import builtins
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import frozen_semantic_ranking_v1 as frozen
import semantic_development_core as core
import verify_semantic_method_freeze as verifier
from wahojobs.opportunity_semantic_authority import (
    build_semantic_matching_packet, canonical_sha256,
)

GOLD_PATH = ROOT / "tests/fixtures/opportunity_semantic_contract_v0.json"
GOLD = json.loads(GOLD_PATH.read_text(encoding="utf-8"))


def packet(index, *, empty=False):
    # Reuse an existing contract fixture; these synthetic IDs are NOT a v2 sample.
    case = next(x for x in GOLD["cases"]
                if x["id"] == "advanced_degree_or_professional_standing")
    contract = copy.deepcopy(case["contract"])
    evidence = copy.deepcopy(case["evidence_catalog"])
    if empty:
        contract["atoms"], contract["constraint_groups"], evidence = [], [], []
    variant = f"freeze_fixture_variant:{index}"
    bindings = [{"source_id": source["id"], "evidence_block_id": f"fixture:{i}",
                 "derivation": "server_authenticated_evidence_binding",
                 "variant_refs": [variant], "source_refs": [variant],
                 "authority_refs": ["fixture:freeze_contract"]}
                for i, source in enumerate(evidence)]
    return build_semantic_matching_packet(
        contract, evidence, canonical_ref=f"freeze_fixture_opportunity:{index}",
        known_variant_refs=[variant], semantic_input_version="freeze-contract-test-v1",
        semantic_input_sha256=canonical_sha256({"contract": contract, "evidence": evidence}),
        source_packet_sha256=canonical_sha256(evidence), variant_relationships=bindings)


def candidate(index, *, empty=False):
    p = packet(index, empty=empty)
    return {"opportunity_ref": p["opportunity_scope"]["canonical_ref"],
            "title": "Synthetic contract fixture", "packet": p}


def output(request, fits=None):
    fits = fits or {}
    assessments = {}
    profile = next(x["reference_id"] for x in request["provider_input"]["profile_reference_catalog"]
                   if x["state"] == "grounded")
    for item in request["provider_input"]["opportunities"]:
        catalog = {x["reference_id"]: x for x in item["reference_catalog"]}
        prop = next(x for x in catalog.values() if x["reference_type"] == "proposition")
        groups = prop["meaning"]["group_reference_ids"]
        provisional = prop["state"] != "grounded" or any(catalog[g]["state"] != "grounded" for g in groups)
        assessments[item["opportunity_id"]] = {
            "fit": fits.get(request["id_map"][item["opportunity_id"]], "contextual"),
            "profile_refs": [profile], "proposition_refs": [prop["reference_id"]],
            "group_refs": groups, "evidence_refs": prop["meaning"]["evidence_reference_ids"][:1],
            "evidence_state": "provisional" if provisional else "supported",
            "reason": "Synthetic contract test, not a relevance judgment.", "issues": []}
    return {"contract_version": core.SCHEMA_VERSION, "assessments": assessments}


class FrozenSemanticMethodTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.candidates = [candidate(i, empty=i == 2) for i in range(1, 5)]
        cls.profile = {"summary": "Synthetic stated background", "skills": ["analysis"]}

    def setUp(self):
        self.request = frozen.prepare_request(self.profile, self.candidates)
        self.legacy = [{"opportunity_ref": x["opportunity_ref"], "legacy_score": 10}
                       for x in self.candidates]
        self.output = output(self.request)

    def test_exact_aliases_and_selected_identity(self):
        self.assertEqual(frozen.FROZEN_METHOD_VERSION, "wahojobs_semantic_ranking_method_v1")
        self.assertEqual(frozen.IMPLEMENTATION_VERSION, "matching_semantic_development_v1")
        self.assertEqual(frozen.INTEGRATION, "ties_only")
        for name in ("prepare_request", "provider_body", "call_provider", "validate_output"):
            self.assertIs(getattr(frozen, name), getattr(core, name))
        with self.assertRaises(TypeError):
            frozen.integrate(self.legacy, self.request, self.output, mode="full")

    def test_manifest_verifies_without_benchmark_artifacts(self):
        result = verifier.verify_manifest()
        self.assertGreater(result["files_verified"], 5)

    def test_manifest_detects_file_tampering(self):
        manifest = json.loads((ROOT / verifier.MANIFEST_PATH).read_text())
        with tempfile.TemporaryDirectory(prefix="wahojobs-freeze-test-") as tmp:
            root = Path(tmp)
            names = [verifier.MANIFEST_PATH, *manifest["files"]]
            for name in names:
                dest = root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((ROOT / name).read_bytes())
            target = root / "scripts/semantic_development_core.py"
            target.write_bytes(target.read_bytes() + b"\n# test-only corruption\n")
            with self.assertRaisesRegex(ValueError, "frozen_file_changed"):
                verifier.verify_manifest(root=root)

    def test_hashes_allow_only_git_line_ending_normalization(self):
        self.assertEqual(verifier.text_digest(b"a\r\nb\r\n"), verifier.text_digest(b"a\nb\n"))
        self.assertNotEqual(verifier.text_digest(b"a \nb\n"), verifier.text_digest(b"a\nb\n"))

    def test_exact_configuration_and_schema_serialization(self):
        body = frozen.provider_body(self.request)
        self.assertEqual({k: body[k] for k in core.CONFIG}, {
            "model": "gpt-5.6-terra", "reasoning": {"effort": "low"},
            "store": False, "max_output_tokens": 9000})
        self.assertEqual(set(body), {*core.CONFIG, "input", "text"})
        self.assertNotIn("temperature", body)
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(body["text"]["format"]["schema"], core.output_schema(self.request))
        self.assertEqual(body["input"][0]["content"][0]["text"], core.PROMPT)
        self.assertEqual(json.loads(body["input"][1]["content"][0]["text"]), self.request["provider_input"])

    def test_runtime_has_no_review_or_evaluation_dependency(self):
        dirty = {**self.profile, "human_relevance": "SECRET_LABEL", "reviewer_notes": "SECRET_NOTE",
                 "benchmark_metrics": "SECRET_METRIC", "expected_order": "SECRET_ORDER"}
        with patch.object(Path, "open", side_effect=AssertionError("runtime file read")), \
             patch.object(builtins, "open", side_effect=AssertionError("runtime file read")):
            request = frozen.prepare_request(dirty, self.candidates)
            body = frozen.provider_body(request)
            actual = frozen.integrate(self.legacy, request, output(request))
        self.assertEqual(request, self.request)
        self.assertNotIn("SECRET_", core.canonical(body))
        self.assertEqual(len(actual), 4)
        for name in ("semantic_development_core.py", "frozen_semantic_ranking_v1.py"):
            source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertNotIn("exports/", source)
            self.assertNotIn("evaluate_semantic_development", source)
            self.assertNotIn("semantic_ranker_development", source)

    def test_provider_metadata_allowlist(self):
        for key in core.FORBIDDEN:
            with self.subTest(key=key):
                dirty = copy.deepcopy(self.request)
                dirty["provider_input"]["nested"] = {key: "forbidden"}
                with self.assertRaises(core.ContractError):
                    frozen.provider_body(dirty)

    def test_legacy_information_only_enters_local_integration(self):
        first = frozen.provider_body(self.request)
        changed = [{**x, "legacy_score": 500-i} for i, x in enumerate(self.legacy)]
        frozen.integrate(changed, self.request, self.output)
        self.assertEqual(first, frozen.provider_body(self.request))
        self.assertEqual(core.forbidden_paths(self.request["provider_input"]), [])

    def test_stable_ids_under_candidate_reordering(self):
        self.assertEqual(self.request, frozen.prepare_request(self.profile, list(reversed(self.candidates))))

    def test_ties_only_precedence_and_empty_positional_fallback(self):
        refs = [x["opportunity_ref"] for x in self.legacy]
        assessed = output(self.request, {refs[0]: "contextual", refs[2]: "direct", refs[3]: "adjacent"})
        rows = frozen.integrate(self.legacy, self.request, assessed)
        self.assertEqual([r["opportunity_ref"] for r in rows], [refs[2], refs[1], refs[3], refs[0]])
        self.assertEqual(rows[1]["fallback"], "packet_empty")
        self.assertEqual(rows[1]["final_rank"], rows[1]["legacy_rank"])

    def test_equal_classes_keep_legacy_order(self):
        rows = frozen.integrate(self.legacy, self.request, self.output)
        self.assertEqual([r["opportunity_ref"] for r in rows], [x["opportunity_ref"] for x in self.legacy])

    def test_non_tied_legacy_order_never_overridden(self):
        self.legacy[-1]["legacy_score"] = 1
        ref = self.legacy[-1]["opportunity_ref"]
        rows = frozen.integrate(self.legacy, self.request, output(self.request, {ref: "direct"}))
        self.assertEqual(rows[-1]["opportunity_ref"], ref)

    def test_provider_and_validation_failure_restore_whole_profile(self):
        for data, failure in [(None, "provider_timeout"), ({**self.output, "extra": 1}, None)]:
            rows = frozen.integrate(self.legacy, self.request, data, failure=failure)
            self.assertTrue(all(r["final_rank"] == r["legacy_rank"] and r["fallback"] for r in rows))

    def test_missing_invalid_and_all_empty_packets_retained(self):
        candidates = [{"opportunity_ref": "fixture:absent", "packet": None},
                      {"opportunity_ref": "fixture:invalid", "packet": {"invalid": True}},
                      candidate(99, empty=True)]
        request = frozen.prepare_request(self.profile, candidates)
        self.assertIsNone(request["provider_input"])
        legacy = [{"opportunity_ref": x["opportunity_ref"], "legacy_score": 1} for x in candidates]
        self.assertEqual(len(frozen.integrate(legacy, request, None)), 3)
        self.assertEqual(set(request["fallback"].values()), {"packet_unavailable", "packet_invalid", "packet_empty"})

    def test_strict_schema_and_grounding_validation(self):
        self.assertTrue(frozen.validate_output(self.output, self.request))
        for field, value in [("fit", 5), ("evidence_state", "unsupported_value"),
                             ("evidence_refs", []), ("group_refs", [])]:
            bad = copy.deepcopy(self.output)
            next(iter(bad["assessments"].values()))[field] = value
            with self.assertRaises(core.ContractError):
                frozen.validate_output(bad, self.request)
        bad = copy.deepcopy(self.output)
        del bad["assessments"][next(iter(bad["assessments"]))]
        with self.assertRaises(core.ContractError):
            frozen.validate_output(bad, self.request)

    def test_no_hard_exclusion_and_exact_replay(self):
        rows = frozen.integrate(self.legacy, self.request, self.output)
        self.assertEqual(rows, frozen.integrate(self.legacy, self.request, json.loads(json.dumps(self.output))))
        self.assertEqual(rows, core.integrate(self.legacy, self.request, self.output, mode="ties_only"))
        self.assertTrue(all(r["authority"] == "semantic_non_exclusionary" and not r["hard_exclusion"] for r in rows))

    def test_60_synthetic_records_and_52_shared_packets(self):
        candidates = [candidate(i, empty=i in {9, 19, 29}) for i in range(52)]
        population = candidates + candidates[:8]
        seen = []
        packet_hashes = {}
        fallbacks = 0
        for i in range(6):
            group = population[i*10:(i+1)*10]
            request = frozen.prepare_request(self.profile, group)
            legacy = [{"opportunity_ref": x["opportunity_ref"], "legacy_score": 10} for x in group]
            rows = frozen.integrate(legacy, request, output(request))
            self.assertEqual(len(rows), 10)
            self.assertEqual(len({x["opportunity_ref"] for x in rows}), 10)
            fallbacks += sum(x["fallback"] == "packet_empty" for x in rows)
            seen.extend(x["opportunity_ref"] for x in rows)
            for record in request["local_catalog"]["opportunities"].values():
                prior = packet_hashes.setdefault(record["internal_opportunity_ref"], record["authenticated_packet_sha256"])
                self.assertEqual(prior, record["authenticated_packet_sha256"])
        self.assertEqual((len(seen), len(set(seen)), fallbacks), (60, 52, 3))

    def test_mocked_provider_endpoint_and_no_retry(self):
        request, assessed = self.request, self.output
        class Response:
            status_code = 200
            def json(self):
                return {"status": "completed", "output": [{"content": [
                    {"type": "output_text", "text": json.dumps(assessed)}]}]}
        class Session:
            def __init__(self, fail=False): self.calls, self.fail = [], fail
            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if self.fail: raise requests.Timeout("synthetic failure")
                return Response()
        for fail in (False, True):
            session = Session(fail)
            result = frozen.call_provider(request, "synthetic-test-key", session=session)
            self.assertEqual(len(session.calls), 1)
            url, args = session.calls[0]
            self.assertEqual(url, "https://api.openai.com/v1/responses")
            self.assertFalse(args["allow_redirects"])
            self.assertEqual(args["timeout"], (10, 180))
            self.assertEqual(args["json"], frozen.provider_body(request))
            self.assertEqual(result["success"], not fail)

    def test_no_live_production_imports(self):
        for path in (ROOT / "wahojobs").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for name in ("frozen_semantic_ranking_v1", "semantic_development_core"):
                self.assertNotIn(name, source, str(path))


if __name__ == "__main__":
    unittest.main()
