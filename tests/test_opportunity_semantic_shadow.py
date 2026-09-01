import ast
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts import profile_match_digest as matcher
from tests.test_typed_match_criteria import matcher_row, profile_v2
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import (
    install_base_schema,
    upsert_job_source_content,
)
from wahojobs.opportunity_enrichment import (
    EXTRACTOR_VERSION,
    SCHEMA_VERSION,
    SEMANTIC_INPUT_VERSION,
    TAXONOMY_VERSION,
    add_evidence,
    extract_deterministic_document,
    llm_source_packet,
    load_semantic_input,
    refresh_unknown_fields,
    semantic_input_sha256,
    validate_enrichment_document,
)
from wahojobs.opportunity_semantic_authority import (
    OpportunitySemanticAuthorityError,
    authority_can_create_hard_eligibility_failure,
    validate_semantic_matching_packet,
)
from wahojobs.opportunity_semantic_shadow import (
    SHADOW_EXECUTION_MODE,
    construct_oe_semantic_matching_packet_v1_shadow,
)
from wahojobs.profiles.canonical import canonical_to_matcher_profile
from wahojobs.profiles.canonical_v2 import project_v2_to_matcher_v1
from wahojobs.source_capture import SourceCaptureContext
from wahojobs.crawler.types import JobCandidate


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-08-31T12:00:00+00:00"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class OpportunitySemanticShadowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "shadow.sqlite"
        self.connection = get_connection(self.database)
        install_base_schema(self.connection)
        self.company_id = self.connection.execute(
            """
            INSERT INTO companies (
              name, slug, careers_url, source_tier, inventory_model,
              market_count_policy
            ) VALUES ('Shadow Source', 'shadow-source',
                      'https://example.test/careers', 'core', 'live_feed',
                      'count_live')
            """
        ).lastrowid

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def insert_canonical(self, *, key="shadow::canonical", title="AI Reviewer"):
        return self.connection.execute(
            """
            INSERT INTO canonical_opportunities (
              company_id, canonical_key, canonical_title, normalized_title,
              source_category, first_seen_at, last_seen_at, is_active,
              variant_count
            ) VALUES (?, ?, ?, ?, 'AI', ?, ?, 1, 1)
            """,
            (self.company_id, key, title, title.casefold(), NOW, NOW),
        ).lastrowid

    def insert_accepted_variant(
        self,
        canonical_id: int,
        *,
        source_hash: str,
        body: str,
        location="Remote",
    ):
        job_id = self.connection.execute(
            """
            INSERT INTO jobs (
              company_id, canonical_opportunity_id, external_id, title,
              location, department, expertise, commitment, url, source_hash,
              opportunity_kind, availability_basis,
              include_in_live_market_estimate, first_seen_at, last_seen_at,
              is_active
            ) VALUES (?, ?, ?, 'AI Reviewer', ?, 'AI', 'General', NULL, ?, ?,
                      'live_posting', 'api_feed', 1, ?, ?, 1)
            """,
            (
                self.company_id,
                canonical_id,
                f"external-{source_hash}",
                location,
                f"https://example.test/jobs/{source_hash}",
                source_hash,
                NOW,
                NOW,
            ),
        ).lastrowid
        row = self.connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        candidate = JobCandidate(
            external_id=row["external_id"],
            title=row["title"],
            location=row["location"],
            url=row["url"],
            department=row["department"],
            expertise=row["expertise"],
            commitment=row["commitment"],
            source_hash=row["source_hash"],
            source_body=body,
            source_body_format="text/plain",
            source_metadata={"fixture": "semantic-shadow-v1"},
            source_updated_at=NOW,
        )
        upsert_job_source_content(
            self.connection,
            job_id,
            "shadow-source",
            "fixture",
            candidate,
            NOW,
            capture_context=SourceCaptureContext(
                crawl_run_id=None,
                provider_outcome="success",
                used_sample_data=False,
                snapshot_complete=True,
                pagination_complete=True,
                empty_snapshot_validated=False,
                raw_record_count=1,
                normalized_record_count=1,
                candidate_count=1,
                rejected_record_count=0,
                payload_shape="semantic-shadow-fixture:v1",
                schema_fingerprint="semantic-shadow-fixture-v1",
            ),
        )
        return job_id

    def packet_and_alias(self, canonical_id: int, text: str):
        semantic_input = load_semantic_input(self.connection, canonical_id)
        packet, _blocks = llm_source_packet(semantic_input)
        block = next(
            block
            for block in packet["evidence_blocks"]
            if block["authority_class"] == "accepted_body_evidence"
            and text in block["content"]
        )
        return semantic_input, packet, block

    @staticmethod
    def extraction(atoms, groups):
        return {
            "extraction_version": "oe_semantic_extraction_v0",
            "atoms": atoms,
            "constraint_groups": groups,
        }

    @staticmethod
    def atom(atom_id, block, quote, *, kind, typed_payload):
        return {
            "id": atom_id,
            "kind": kind,
            "typed_payload": typed_payload,
            "polarity": "affirmed",
            "temporal": "unspecified",
            "evidence": [
                {"alias": block["evidence_block_id"], "quote": quote}
            ],
        }

    @staticmethod
    def singleton(atom_id, modality="required"):
        return {"modality": modality, "any_of": [{"all_of": [atom_id]}]}

    def persist_native_signals(self, canonical_id, semantic_input, block):
        document = extract_deterministic_document(semantic_input)
        document["attributes"]["content"]["responsibilities"] = [
            "Review model output and document factual defects."
        ]
        document["attributes"]["content"]["candidate_profile"] = (
            "A careful reviewer who can explain model defects."
        )
        for field_path in (
            "attributes.content.responsibilities",
            "attributes.content.candidate_profile",
        ):
            add_evidence(
                document["field_evidence"],
                field_path,
                block["source_ref"],
                block["content"],
                "llm_source_evidence",
                "high",
                evidence_block_ref=block["evidence_block_id"],
                variant_refs=block["variant_refs"],
                authority_refs=block["authority_refs"],
            )
        refresh_unknown_fields(document)
        validate_enrichment_document(document)
        input_sha = semantic_input_sha256(semantic_input)
        self.connection.execute(
            """
            INSERT INTO opportunity_enrichments (
              canonical_opportunity_id, schema_version, taxonomy_version,
              extractor_version, input_sha256, status,
              automatic_document_json, model_provider, model_name,
              prompt_version, semantic_input_version, derivation_fingerprint,
              generated_at
            ) VALUES (?, ?, ?, ?, ?, 'partial', ?, 'openai',
                      'fixture-model', 'fixture-prompt', ?, ?, ?)
            """,
            (
                canonical_id,
                SCHEMA_VERSION,
                TAXONOMY_VERSION,
                EXTRACTOR_VERSION,
                input_sha,
                json.dumps(document, sort_keys=True),
                SEMANTIC_INPUT_VERSION,
                "f" * 64,
                NOW,
            ),
        )

    def test_single_variant_packet_is_available_validated_and_non_exclusionary(self):
        canonical_id = self.insert_canonical()
        body = (
            "Software expertise is required.\n\n"
            "Review model output and document factual defects."
        )
        self.insert_accepted_variant(
            canonical_id, source_hash="single-variant", body=body
        )
        semantic_input, _packet, block = self.packet_and_alias(
            canonical_id, "Software expertise is required."
        )
        self.persist_native_signals(canonical_id, semantic_input, block)
        atom = self.atom(
            "software_domain",
            block,
            "Software expertise is required.",
            kind="domain_expertise",
            typed_payload={"domain": "software"},
        )
        payload = self.extraction(
            [atom], [self.singleton("software_domain")]
        )

        self.connection.commit()
        changes_before = self.connection.total_changes
        database_before = file_sha256(self.database)
        result = construct_oe_semantic_matching_packet_v1_shadow(
            self.connection,
            canonical_id,
            extraction_payload=payload,
        )
        database_after = file_sha256(self.database)

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["execution_mode"], SHADOW_EXECUTION_MODE)
        packet = result["packet"]
        self.assertEqual(
            validate_semantic_matching_packet(packet), packet
        )
        self.assertEqual(packet["opportunity_scope"]["variant_mode"], "single_variant")
        self.assertEqual(
            packet["propositions"][0]["applicability"]["variant_applicability"],
            "single_variant",
        )
        self.assertEqual(packet["accounting"]["semantic_hard_exclusion_count"], 0)
        self.assertEqual(packet["accounting"]["canonical_semantic_fact_claim_count"], 0)
        self.assertFalse(authority_can_create_hard_eligibility_failure(packet))
        self.assertFalse(
            result["objective_deterministic_context"]["included_in_semantic_packet"]
        )
        self.assertFalse(
            result["objective_deterministic_context"]["eligibility_consumer_invoked"]
        )
        self.assertEqual(
            result["metrics"]["descriptive_signal_availability"]
            ["responsibilities"]["status"],
            "available",
        )
        self.assertEqual(
            result["metrics"]["descriptive_signal_availability"]
            ["candidate_profile"]["status"],
            "available",
        )
        self.assertEqual(self.connection.total_changes, changes_before)
        self.assertEqual(database_after, database_before)

    def test_multi_variant_packet_preserves_differing_variant_applicability(self):
        canonical_id = self.insert_canonical(key="shadow::multi")
        common = "Software expertise is required."
        english = "Fluent English is required."
        spanish = "Fluent Spanish is required."
        self.insert_accepted_variant(
            canonical_id,
            source_hash="multi-english",
            body=f"{common}\n\n{english}",
            location="United States",
        )
        self.insert_accepted_variant(
            canonical_id,
            source_hash="multi-spanish",
            body=f"{common}\n\n{spanish}",
            location="Spain",
        )
        _input, packet, common_block = self.packet_and_alias(canonical_id, common)
        english_block = next(
            block for block in packet["evidence_blocks"] if english in block["content"]
        )
        spanish_block = next(
            block for block in packet["evidence_blocks"] if spanish in block["content"]
        )
        atoms = [
            self.atom(
                "software_domain",
                common_block,
                common,
                kind="domain_expertise",
                typed_payload={"domain": "software"},
            ),
            self.atom(
                "english_required",
                english_block,
                english,
                kind="language_proficiency",
                typed_payload={
                    "language": "English",
                    "locale": None,
                    "proficiency": "fluent",
                },
            ),
            self.atom(
                "spanish_required",
                spanish_block,
                spanish,
                kind="language_proficiency",
                typed_payload={
                    "language": "Spanish",
                    "locale": None,
                    "proficiency": "fluent",
                },
            ),
        ]
        payload = self.extraction(
            atoms,
            [self.singleton(atom["id"]) for atom in atoms],
        )
        result = construct_oe_semantic_matching_packet_v1_shadow(
            self.connection,
            canonical_id,
            extraction_payload=payload,
        )

        self.assertEqual(result["status"], "available")
        packet = result["packet"]
        self.assertEqual(packet["opportunity_scope"]["variant_mode"], "multi_variant")
        propositions = {
            item["proposition_id"]: item for item in packet["propositions"]
        }
        self.assertEqual(
            propositions["software_domain"]["applicability"]
            ["variant_applicability"],
            "all_known_variants",
        )
        self.assertEqual(
            propositions["english_required"]["applicability"]
            ["variant_applicability"],
            "variant_subset",
        )
        self.assertEqual(
            propositions["spanish_required"]["applicability"]
            ["variant_applicability"],
            "variant_subset",
        )
        self.assertNotEqual(
            propositions["english_required"]["applicability"]["variant_refs"],
            propositions["spanish_required"]["applicability"]["variant_refs"],
        )
        self.assertTrue(
            all(
                item["applicability"]["canonical_applicability"] == "not_claimed"
                and not authority_can_create_hard_eligibility_failure(item)
                for item in packet["propositions"]
            )
        )

    def test_fail_closed_status_returns_no_partial_packet(self):
        canonical_id = self.insert_canonical(key="shadow::failure")
        self.insert_accepted_variant(
            canonical_id,
            source_hash="failure-variant",
            body="Software expertise is required.",
        )
        result = construct_oe_semantic_matching_packet_v1_shadow(
            self.connection, canonical_id
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(
            result["failure"]["code"], "semantic_extraction_not_supplied"
        )
        self.assertIsNone(result["packet"])
        self.assertFalse(result["isolation"]["database_persistence_authorized"])

        invalid = construct_oe_semantic_matching_packet_v1_shadow(
            self.connection,
            canonical_id,
            extraction_payload=[],
        )
        self.assertEqual(invalid["status"], "invalid")
        self.assertEqual(
            invalid["failure"]["code"],
            "packet_construction_validation_failed",
        )
        self.assertIsNone(invalid["packet"])

    def test_packet_authority_tampering_fails_complete_validation(self):
        canonical_id = self.insert_canonical(key="shadow::tamper")
        self.insert_accepted_variant(
            canonical_id,
            source_hash="tamper-variant",
            body="Software expertise is required.",
        )
        _input, _source_packet, block = self.packet_and_alias(
            canonical_id, "Software expertise is required."
        )
        payload = self.extraction(
            [
                self.atom(
                    "software_domain",
                    block,
                    "Software expertise is required.",
                    kind="domain_expertise",
                    typed_payload={"domain": "software"},
                )
            ],
            [self.singleton("software_domain")],
        )
        packet = construct_oe_semantic_matching_packet_v1_shadow(
            self.connection,
            canonical_id,
            extraction_payload=payload,
        )["packet"]
        tampered = copy.deepcopy(packet)
        tampered["propositions"][0]["authority"][
            "hard_eligibility_authorized"
        ] = True
        material = copy.deepcopy(tampered)
        material.pop("packet_sha256")
        tampered["packet_sha256"] = hashlib.sha256(
            json.dumps(
                material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaises(OpportunitySemanticAuthorityError):
            validate_semantic_matching_packet(tampered)

    def test_current_matcher_and_runtime_import_graph_are_inert(self):
        canonical_id = self.insert_canonical(key="shadow::matcher-inert")
        self.insert_accepted_variant(
            canonical_id,
            source_hash="matcher-inert-variant",
            body="Software expertise is required.",
        )
        _input, _source_packet, block = self.packet_and_alias(
            canonical_id, "Software expertise is required."
        )
        payload = self.extraction(
            [
                self.atom(
                    "software_domain",
                    block,
                    "Software expertise is required.",
                    kind="domain_expertise",
                    typed_payload={"domain": "software"},
                )
            ],
            [self.singleton("software_domain")],
        )
        authoritative = profile_v2()
        row = matcher_row()
        projected = project_v2_to_matcher_v1(
            authoritative, matcher_profile_id="semantic-shadow-inert"
        )
        matcher_profile = canonical_to_matcher_profile(projected)
        score_before = matcher.score_opportunity(matcher_profile, row)
        result = construct_oe_semantic_matching_packet_v1_shadow(
            self.connection,
            canonical_id,
            extraction_payload=payload,
        )
        score_after = matcher.score_opportunity(matcher_profile, row)
        self.assertEqual(result["status"], "available")
        self.assertEqual(score_after, score_before)

        consumers = []
        for path in (ROOT / "wahojobs").rglob("*.py"):
            if path.name == "opportunity_semantic_shadow.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == (
                    "wahojobs.opportunity_semantic_shadow"
                ):
                    consumers.append(path.relative_to(ROOT).as_posix())
                if isinstance(node, ast.Import) and any(
                    alias.name == "wahojobs.opportunity_semantic_shadow"
                    for alias in node.names
                ):
                    consumers.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(consumers, [])
        self.assertTrue(
            all(value is False for value in result["isolation"].values())
        )


if __name__ == "__main__":
    unittest.main()
