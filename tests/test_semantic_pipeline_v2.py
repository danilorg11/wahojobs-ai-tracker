"""Synthetic complete-pipeline tests; no benchmark content or real HTTP allowed."""
import copy
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import semantic_pipeline_evidence as evidence
import semantic_pipeline_v2 as pipeline
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import install_base_schema, upsert_job_source_content
from wahojobs.crawler.types import JobCandidate
from wahojobs.source_capture import SourceCaptureContext

NOW = "2026-09-01T00:00:00+00:00"
QUOTE = "Fact-checking capability is required."
BODY = QUOTE + "\n\n" + (
    "This synthetic opportunity involves evaluating written material, documenting findings, "
    "and explaining conclusions using supplied sources. Work is completed in a local test "
    "environment. The following description exists only to exercise source acceptance and "
    "evidence binding. It is not a real opportunity or a quality benchmark. "
) * 4


def fixture_database(path, count=4):
    conn = get_connection(path)
    install_base_schema(conn)
    company = conn.execute("""INSERT INTO companies
        (name, slug, careers_url, source_tier, inventory_model, market_count_policy)
        VALUES ('Synthetic', 'synthetic', 'https://example.test', 'core', 'live_feed', 'count_live')""").lastrowid
    for i in range(1, count + 1):
        cid = conn.execute("""INSERT INTO canonical_opportunities
            (company_id, canonical_key, canonical_title, normalized_title, source_category,
             first_seen_at, last_seen_at, is_active, variant_count)
            VALUES (?, ?, 'Synthetic reviewer', 'synthetic reviewer', 'AI', ?, ?, 1, 1)""",
            (company, "synthetic:" + str(i), NOW, NOW)).lastrowid
        source_hash = "synthetic-source-" + str(i)
        url = "https://example.test/synthetic/" + str(i)
        jid = conn.execute("""INSERT INTO jobs
            (company_id, canonical_opportunity_id, external_id, title, location, department,
             expertise, commitment, url, source_hash, opportunity_kind, availability_basis,
             include_in_live_market_estimate, first_seen_at, last_seen_at, is_active)
            VALUES (?, ?, ?, 'Synthetic reviewer', 'Remote', 'AI', 'General', NULL, ?, ?,
                    'live_posting', 'api_feed', 1, ?, ?, 1)""",
            (company, cid, str(i), url, source_hash, NOW, NOW)).lastrowid
        candidate = JobCandidate(external_id=str(i), title="Synthetic reviewer", location="Remote",
            url=url, department="AI", expertise="General", commitment=None, source_hash=source_hash,
            source_body=BODY, source_body_format="text/plain", source_metadata={"fixture": "synthetic"},
            source_updated_at=NOW)
        upsert_job_source_content(conn, jid, "synthetic", "fixture", candidate, NOW,
            capture_context=SourceCaptureContext(crawl_run_id=None, provider_outcome="success",
                used_sample_data=False, snapshot_complete=True, pagination_complete=True,
                empty_snapshot_validated=False, raw_record_count=1, normalized_record_count=1,
                candidate_count=1, rejected_record_count=0, payload_shape="synthetic",
                schema_fingerprint="synthetic"))
    conn.commit()
    conn.close()


def extraction_fixture(record, *, empty=False):
    if empty:
        return {"extraction_version": pipeline.extraction.EXTRACTION_CONTRACT_VERSION,
                "atoms": [], "constraint_groups": []}
    alias = next(b["alias"] for b in evidence.accepted_bindings(record) if QUOTE in b["text"])
    return {"extraction_version": pipeline.extraction.EXTRACTION_CONTRACT_VERSION,
            "atoms": [{"id": "fact_checking", "kind": "capability",
                "typed_payload": {"capability": "fact_checking"}, "polarity": "affirmed",
                "temporal": "unspecified", "evidence": [{"alias": alias, "quote": QUOTE}]}],
            "constraint_groups": [{"modality": "required",
                "any_of": [{"all_of": ["fact_checking"]}]}]}


def assessment_fixture(request, fits=None):
    fits = fits or {}
    profile = next(x["reference_id"] for x in request["provider_input"]["profile_reference_catalog"]
                   if x["state"] == "grounded")
    output = {}
    for item in request["provider_input"]["opportunities"]:
        catalog = {x["reference_id"]: x for x in item["reference_catalog"]}
        prop = next(x for x in catalog.values() if x["reference_type"] == "proposition")
        groups = prop["meaning"]["group_reference_ids"]
        provisional = prop["state"] != "grounded" or any(catalog[g]["state"] != "grounded" for g in groups)
        output[item["opportunity_id"]] = {"fit": fits.get(request["id_map"][item["opportunity_id"]], "contextual"),
            "profile_refs": [profile], "proposition_refs": [prop["reference_id"]], "group_refs": groups,
            "evidence_refs": prop["meaning"]["evidence_reference_ids"][:1],
            "evidence_state": "provisional" if provisional else "supported",
            "reason": "Synthetic orchestration fixture, not a human relevance judgment.", "issues": []}
    return {"contract_version": pipeline.core.SCHEMA_VERSION, "assessments": output}


class MockResponse:
    status_code = 200
    def __init__(self, payload):
        self.payload = payload
    def json(self):
        return {"id": "resp_synthetic", "status": "completed", "model": "gpt-5.6-terra",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(self.payload)}]}],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}


class MockSession:
    def __init__(self, payload=None, *, fail=False):
        self.payload, self.fail, self.calls = payload, fail, []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.fail:
            raise requests.Timeout("synthetic timeout")
        return MockResponse(self.payload)


class CompleteSemanticPipelineTests(unittest.TestCase):
    def setUp(self):
        self.socket_guard = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="wahojobs-synthetic-pipeline-")
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.database = self.directory / "synthetic.sqlite"
        fixture_database(self.database)
        self.db_sha = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.sources = [evidence.capture_opportunity(self.database, i) for i in range(1, 5)]
        self.assertTrue(all(x["evidence"] and not x["failure"] for x in self.sources),
                        [(x["opportunity_ref"], x["failure"]) for x in self.sources])
        self.plan = pipeline.prepare_provisioning(self.sources)
        self.refs = [x["opportunity_ref"] for x in self.plan["items"]]
        self.raw = {x["opportunity_ref"]: {"raw_extraction": extraction_fixture(x["evidence"], empty=i == 1)}
                    for i, x in enumerate(self.plan["items"])}
        self.provisioned = pipeline.finish_provisioning(self.plan, self.raw)
        self.batch = {"profile_key": "synthetic-profile", "profile_facts": {"skills": ["fact checking"],
            "summary": "Synthetic stated background"},
            "candidates": [{"opportunity_ref": r, "title": "Synthetic reviewer"} for r in self.refs]}
        self.prepared = pipeline.prepare_assessments([self.batch], self.provisioned)
        self.legacy = {"synthetic-profile": [{"opportunity_ref": r, "legacy_score": 10} for r in self.refs]}

    def execute(self, stage, scope, prepared, session):
        authorization = {"endpoint": pipeline.ENDPOINT,
                         "requests": {prepared["request_id"]: pipeline.fingerprint(prepared["body"])}}
        return pipeline.execute_prepared(stage, scope, prepared, authorization=authorization,
            api_key="synthetic-test-not-a-credential", journal_directory=self.directory / "journal", session=session)

    def test_unseen_canonical_complete_path_and_exact_configuration(self):
        results = {}
        for i, item in enumerate(self.plan["items"]):
            session = MockSession(self.raw[item["opportunity_ref"]]["raw_extraction"])
            results[item["opportunity_ref"]] = self.execute("extraction", item["opportunity_ref"], item, session)
            self.assertIsNone(results[item["opportunity_ref"]]["failure"])
            self.assertEqual(len(session.calls), 1)
            url, args = session.calls[0]
            self.assertEqual(url, pipeline.ENDPOINT)
            self.assertEqual(args["json"], item["body"])
            self.assertEqual({k: args["json"][k] for k in pipeline.EXTRACTION_CONFIG}, pipeline.EXTRACTION_CONFIG)
            self.assertNotIn("temperature", args["json"])
            self.assertFalse(args["allow_redirects"])
            self.assertEqual(args["timeout"], (10, 120))
        packets = pipeline.finish_provisioning(self.plan, results, cache_directory=self.directory / "cache")
        self.assertEqual([x["status"] for x in packets.values()], ["nonempty", "empty", "nonempty", "nonempty"])
        prepared = pipeline.prepare_assessments([self.batch], packets)
        request = prepared["synthetic-profile"]
        session = MockSession(assessment_fixture(request["request"], {self.refs[2]: "direct"}))
        result = self.execute("assessment", "synthetic-profile", request, session)
        self.assertTrue(result["success"])
        self.assertEqual(session.calls[0][1]["json"], request["body"])
        self.assertEqual(request["body"]["max_output_tokens"], 9000)
        rows = pipeline.integrate_rankings(prepared, {"synthetic-profile": result}, self.legacy)["synthetic-profile"]
        self.assertEqual([x["opportunity_ref"] for x in rows], [self.refs[2], self.refs[1], self.refs[0], self.refs[3]])
        self.assertEqual(rows[1]["fallback"], "packet_empty")
        self.assertTrue(all(not x["hard_exclusion"] and x["authority"] == "semantic_non_exclusionary" for x in rows))
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), self.db_sha)

    def test_cache_hit_miss_empty_hit_and_retained_provenance(self):
        cache = self.directory / "cache"
        results = {}
        for item in self.plan["items"]:
            results[item["opportunity_ref"]] = self.execute("extraction", item["opportunity_ref"], item,
                MockSession(self.raw[item["opportunity_ref"]]["raw_extraction"]))
        original = pipeline.finish_provisioning(self.plan, results, cache_directory=cache)
        hit = pipeline.prepare_provisioning(self.sources, cache_directory=cache)
        self.assertEqual(hit["request_count"], 0)
        self.assertTrue(all(x["cache_state"] == "hit" for x in hit["items"]))
        replay = pipeline.finish_provisioning(hit, {}, cache_directory=cache)
        for ref in original:
            self.assertEqual(original[ref]["packet"], replay[ref]["packet"])
            self.assertEqual(original[ref]["provider_receipt"], replay[ref]["provider_receipt"])
        self.assertEqual(pipeline.prepare_provisioning(self.sources)["request_count"], 4)

    def test_incompatible_cache_and_fixture_without_receipt_never_reused(self):
        cache = self.directory / "cache"
        pipeline.finish_provisioning(self.plan, self.raw, cache_directory=cache)
        self.assertFalse(cache.exists())
        item = self.plan["items"][0]
        result = self.execute("extraction", item["opportunity_ref"], item, MockSession(self.raw[self.refs[0]]["raw_extraction"]))
        pipeline.finish_provisioning(self.plan, {self.refs[0]: result}, cache_directory=cache)
        path = cache / (item["cache_key"] + ".json")
        entry = json.loads(path.read_text())
        entry["identity"]["configuration"]["max_output_tokens"] = 99
        entry["integrity_sha256"] = pipeline.fingerprint({k: v for k, v in entry.items() if k != "integrity_sha256"})
        path.write_text(json.dumps(entry), encoding="utf-8")
        changed = pipeline.prepare_provisioning(self.sources, cache_directory=cache)
        self.assertEqual(changed["items"][0]["cache_state"], "incompatible")
        self.assertEqual(changed["request_count"], 4)
        record = copy.deepcopy(self.sources[0]["evidence"])
        record["authority"]["accepted_capture_bindings"][0]["last_confirmed_at"] = "later synthetic timestamp"
        record["evidence_sha256"] = evidence.fingerprint({k: v for k, v in record.items() if k != "evidence_sha256"})
        changed_source = {**self.sources[0], "evidence": record}
        changed_key = pipeline.prepare_provisioning([changed_source])["items"][0]["cache_key"]
        self.assertNotEqual(changed_key, item["cache_key"])

    def test_invalid_extraction_and_missing_response_retained(self):
        supplied = copy.deepcopy(self.raw)
        supplied[self.refs[0]]["raw_extraction"] = {"invalid": True}
        del supplied[self.refs[2]]
        supplied[self.refs[3]] = {"failure": "provider_failed"}
        packets = pipeline.finish_provisioning(self.plan, supplied)
        self.assertEqual(set(packets), set(self.refs))
        self.assertEqual([x["status"] for x in packets.values()], ["unavailable", "empty", "unavailable", "unavailable"])
        prepared = pipeline.prepare_assessments([self.batch], packets)
        self.assertIsNone(prepared["synthetic-profile"]["body"])
        rows = pipeline.integrate_rankings(prepared, {}, self.legacy)["synthetic-profile"]
        self.assertEqual([x["opportunity_ref"] for x in rows], self.refs)

    def test_extraction_transport_failure_retains_all_opportunities(self):
        item = self.plan["items"][0]
        session = MockSession(fail=True)
        result = self.execute("extraction", self.refs[0], item, session)
        self.assertIsNotNone(result["failure"])
        self.assertEqual(len(session.calls), 1)
        packets = pipeline.finish_provisioning(self.plan, {self.refs[0]: result})
        self.assertEqual(set(packets), set(self.refs))
        self.assertTrue(all(x["status"] == "unavailable" for x in packets.values()))
        self.execute("extraction", self.refs[0], item, session)
        self.assertEqual(len(session.calls), 1)

    def test_unauthenticated_quote_preserves_unavailable_fallback(self):
        raw = copy.deepcopy(self.raw)
        raw[self.refs[0]]["raw_extraction"]["atoms"][0]["evidence"][0]["quote"] = "Not present in the accepted source."
        packets = pipeline.finish_provisioning(self.plan, raw)
        self.assertEqual(packets[self.refs[0]]["status"], "unavailable")
        self.assertIsNone(packets[self.refs[0]]["packet"])
        prepared = pipeline.prepare_assessments([self.batch], packets)
        self.assertEqual(prepared["synthetic-profile"]["request"]["fallback"][self.refs[0]], "packet_unavailable")

    def test_duplicate_or_cross_joined_candidates_rejected(self):
        duplicate = copy.deepcopy(self.batch)
        duplicate["candidates"].append(duplicate["candidates"][0])
        with self.assertRaises(ValueError):
            pipeline.prepare_assessments([duplicate], self.provisioned)
        cross_joined = copy.deepcopy(self.provisioned)
        cross_joined[self.refs[0]]["packet"] = cross_joined[self.refs[2]]["packet"]
        with self.assertRaisesRegex(ValueError, "packet_identity_mismatch"):
            pipeline.prepare_assessments([self.batch], cross_joined)

    def test_readonly_binding_rejects_database_writes(self):
        conn = evidence.open_immutable_database(self.database)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE canonical_opportunities SET canonical_title = 'not allowed'")
        finally:
            conn.close()
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), self.db_sha)

    def test_unauthenticated_evidence_falls_back_without_call(self):
        bad = copy.deepcopy(self.sources[0])
        bad["evidence"]["frozen_semantic_input"]["rich_content"][0]["body"] = "unrelated changed evidence"
        plan = pipeline.prepare_provisioning([bad])
        self.assertEqual(plan["request_count"], 0)
        self.assertEqual(pipeline.finish_provisioning(plan, {})[self.refs[0]]["status"], "unavailable")
        missing = evidence.capture_opportunity(self.database, 99999)
        self.assertIsNone(missing["evidence"])

    def test_stable_ids_shared_packets_no_drops(self):
        plan = pipeline.prepare_provisioning(list(reversed(self.sources)) + self.sources)
        self.assertEqual(plan, self.plan)
        second = copy.deepcopy(self.batch)
        second["profile_key"] = "second-synthetic-profile"
        prepared = pipeline.prepare_assessments([self.batch, second], self.provisioned)
        self.assertEqual(prepared["synthetic-profile"]["request"], prepared["second-synthetic-profile"]["request"])
        self.assertNotEqual(prepared["synthetic-profile"]["request_id"], prepared["second-synthetic-profile"]["request_id"])
        legacy = {**self.legacy, "second-synthetic-profile": self.legacy["synthetic-profile"]}
        rows = pipeline.integrate_rankings(prepared, {}, legacy)
        self.assertEqual(sum(len(x) for x in rows.values()), 8)
        changed = copy.deepcopy(self.sources[0]); changed["failure"] = "inconsistent"
        with self.assertRaises(pipeline.PipelineError):
            pipeline.prepare_provisioning(self.sources + [changed])

    def test_ties_stability_non_tied_order_and_determinism(self):
        request = self.prepared["synthetic-profile"]["request"]
        assessed = {"synthetic-profile": {"output": assessment_fixture(request)}}
        first = pipeline.integrate_rankings(self.prepared, assessed, self.legacy)
        self.assertEqual([r["opportunity_ref"] for r in first["synthetic-profile"]], self.refs)
        self.assertEqual(first, pipeline.integrate_rankings(self.prepared, json.loads(json.dumps(assessed)), self.legacy))
        self.legacy["synthetic-profile"][-1]["legacy_score"] = 1
        assessed["synthetic-profile"]["output"] = assessment_fixture(request, {self.refs[-1]: "direct"})
        rows = pipeline.integrate_rankings(self.prepared, assessed, self.legacy)["synthetic-profile"]
        self.assertEqual(rows[-1]["opportunity_ref"], self.refs[-1])

    def test_provider_failure_invalid_output_whole_profile_fallback(self):
        prepared = self.prepared["synthetic-profile"]
        session = MockSession(fail=True)
        failed = self.execute("assessment", "synthetic-profile", prepared, session)
        self.assertEqual(len(session.calls), 1)
        invalid = {"output": {**assessment_fixture(prepared["request"]), "extra": True}}
        for result in [failed, invalid]:
            rows = pipeline.integrate_rankings(self.prepared, {"synthetic-profile": result}, self.legacy)["synthetic-profile"]
            self.assertTrue(all(r["legacy_rank"] == r["final_rank"] and r["fallback"] for r in rows))

    def test_exact_authorization_journal_replay_no_implicit_retry(self):
        item = self.plan["items"][0]
        session = MockSession(self.raw[self.refs[0]]["raw_extraction"])
        denied = pipeline.execute_prepared("extraction", self.refs[0], item, authorization={}, api_key="test",
            journal_directory=self.directory / "journal", session=session)
        self.assertEqual(denied["failure"], "authorization_required")
        self.assertEqual(session.calls, [])
        first = self.execute("extraction", self.refs[0], item, session)
        self.assertEqual(first, self.execute("extraction", self.refs[0], item, session))
        self.assertEqual(len(session.calls), 1)
        result_path = self.directory / "journal" / (item["request_id"] + ".result.json")
        result_path.write_text("{}", encoding="utf-8")
        self.assertEqual(self.execute("extraction", self.refs[0], item, session)["failure"], "uncertain_prior_result")
        result_path.unlink()
        self.assertEqual(self.execute("extraction", self.refs[0], item, session)["failure"], "uncertain_prior_attempt")
        self.assertEqual(len(session.calls), 1)

    def test_human_benchmark_and_legacy_leakage_boundaries(self):
        for forbidden in evidence.FORBIDDEN_METADATA:
            dirty = copy.deepcopy(self.sources[0]); dirty["evidence"][forbidden] = "SECRET_SENTINEL"
            self.assertEqual(pipeline.prepare_provisioning([dirty])["request_count"], 0)
            dirty_batch = copy.deepcopy(self.batch); dirty_batch["profile_facts"][forbidden] = "SECRET_SENTINEL"
            with self.assertRaises(pipeline.PipelineError):
                pipeline.prepare_assessments([dirty_batch], self.provisioned)
        dirty_batch = copy.deepcopy(self.batch)
        dirty_batch["candidates"][0]["legacy_score"] = 100
        with self.assertRaises(pipeline.PipelineError):
            pipeline.prepare_assessments([dirty_batch], self.provisioned)
        for body in [*(i["body"] for i in self.plan["items"]), self.prepared["synthetic-profile"]["body"]]:
            self.assertNotIn("SECRET_SENTINEL", pipeline.canonical(body))
        for path in ["scripts/semantic_pipeline_v2.py", "scripts/semantic_pipeline_evidence.py"]:
            text = (ROOT / path).read_text(encoding="utf-8")
            self.assertNotIn("exports/", text)
            self.assertNotIn("prospective_semantic_ranking_benchmark", text)

    def test_runtime_requires_no_benchmark_files(self):
        original = Path.open
        def guarded(path, *args, **kwargs):
            if "exports" in path.parts:
                raise AssertionError("benchmark artifacts inaccessible")
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", guarded):
            sources = [evidence.capture_opportunity(self.database, 1)]
            plan = pipeline.prepare_provisioning(sources)
            result = pipeline.finish_provisioning(plan, {self.refs[0]: self.raw[self.refs[0]]})
            batch = {**self.batch, "candidates": self.batch["candidates"][:1]}
            prepared = pipeline.prepare_assessments([batch], result)
            legacy = {"synthetic-profile": self.legacy["synthetic-profile"][:1]}
            self.assertEqual(len(pipeline.integrate_rankings(prepared, {}, legacy)["synthetic-profile"]), 1)


if __name__ == "__main__":
    unittest.main()
