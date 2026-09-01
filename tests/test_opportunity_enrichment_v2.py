import copy
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import wahojobs.opportunity_enrichment as opportunity_enrichment
from wahojobs.canonical.service import sync_fallback_canonical_opportunities
from wahojobs.crawler.types import CompanyCrawlResult, JobCandidate, ProviderOutcome
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import (
    ensure_opportunity_enrichment_schema,
    install_base_schema,
    upsert_job_source_content,
)
from wahojobs.opportunity_enrichment import (
    DERIVATION_RECIPE_VERSION,
    EnrichmentValidationError,
    EXTRACTOR_VERSION,
    LEGACY_SEMANTIC_INPUT_VERSION,
    SCHEMA_VERSION,
    SEMANTIC_INPUT_VERSION,
    STALE_REASON_DERIVATION_CONTRACT_CHANGED,
    STALE_REASON_SOURCE_INPUT_CHANGED,
    TAXONOMY_VERSION,
    apply_llm_semantic_acceptance_guards,
    blank_llm_payload,
    blank_document,
    classify_enrichment_freshness,
    derivation_recipe_fingerprint,
    enrich_canonical_opportunity,
    extract_deterministic_document,
    import_reviewed_overlay,
    llm_source_packet,
    llm_usage_observability,
    load_semantic_input,
    merge_llm_payload,
    normalize_llm_list_fields,
    resolve_effective_enrichment,
    save_override,
    semantic_input_sha256,
    semantic_variant_refs,
    skill_is_task_description,
    source_body_text,
    validate_llm_payload,
)
from wahojobs.opportunity_llm import PROMPT_VERSION, StructuredEnrichmentResult
from wahojobs.opportunity_enrichment_schema import (
    OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS,
    OpportunityEnrichmentSchemaError,
    attest_opportunity_enrichment_schema_extension,
)
from wahojobs.reporting.market import get_market_size_summary
from wahojobs.source_capture import SourceCaptureContext
from wahojobs.tracking.service import track_crawl_result


NOW = "2026-08-16T12:00:00+00:00"


class FakeStructuredLLM:
    provider = "openai"
    model = "gpt-5-mini"
    prompt_version = PROMPT_VERSION

    def __init__(
        self,
        *,
        unknown_evidence=False,
        invalid_schema=False,
        duplicate_work_activity=False,
    ):
        self.calls = []
        self.unknown_evidence = unknown_evidence
        self.invalid_schema = invalid_schema
        self.duplicate_work_activity = duplicate_work_activity

    def enrich(self, packet):
        self.calls.append(packet)
        body_block = next(
            item
            for item in packet["evidence_blocks"]
            if item["kind"] == "body_paragraph"
        )
        block_id = body_block["evidence_block_id"]
        alternate_block_id = next(
            item["evidence_block_id"]
            for item in packet["evidence_blocks"]
            if item["kind"] == "listing_field"
            and item["label"] == "listing.title"
        )

        def evidence(_quote):
            if self.unknown_evidence:
                return ["evidence:invented"]
            return [block_id]

        payload = blank_llm_payload()
        payload.update({
            "role_family": {
                "value": "software_engineering",
                "evidence": evidence("Build and review Python services."),
            },
            "professional_domains": [
                {
                    "value": "technical",
                    "evidence": evidence("Build and review Python services."),
                }
            ],
            "work_activities": [
                {
                    "value": "software_development",
                    "evidence": evidence("Build and review Python services."),
                }
            ],
            "skills_required": [
                {
                    "value": "Python",
                    "evidence": evidence("Python is required."),
                }
            ],
            "skills_preferred": [
                {
                    "value": "SQL",
                    "evidence": evidence("SQL is preferred."),
                }
            ],
            "responsibilities": [
                {
                    "value": "Build and review Python services",
                    "evidence": evidence("Build and review Python services."),
                }
            ],
            "candidate_profile": {
                "value": "A Python engineer who can evaluate technical work.",
                "evidence": evidence("Evaluate technical work for quality."),
            },
            "quick_take": {
                "value": "Build Python services and evaluate technical quality.",
                "evidence": evidence("Build and review Python services."),
            },
            "caveats": [],
        })
        if self.invalid_schema:
            payload.pop("quick_take")
        if self.duplicate_work_activity:
            payload["work_activities"].append(
                {
                    "value": "software_development",
                    "evidence": [alternate_block_id],
                }
            )
        return StructuredEnrichmentResult(
            payload=payload,
            response_id=f"resp-{len(self.calls)}",
            input_tokens=1_200,
            output_tokens=300,
            total_tokens=1_500,
            estimated_cost_usd=0.0009,
            response_status="completed",
            http_status=200,
        )


class OpportunityEnrichmentV2Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "opportunities.sqlite"
        self.conn = get_connection(self.database)
        install_base_schema(self.conn)
        self.company_id = self.insert_company("Appen", "appen")

    def tearDown(self):
        self.conn.close()
        self.temporary.cleanup()

    def insert_company(self, name, slug):
        cursor = self.conn.execute(
            """
            INSERT INTO companies (
              name, slug, careers_url,
              source_tier, inventory_model, market_count_policy
            ) VALUES (?, ?, ?, 'core', 'live_feed', 'count_live')
            """,
            (name, slug, f"https://example.test/{slug}"),
        )
        return cursor.lastrowid

    def insert_job(
        self,
        source_hash,
        title,
        *,
        canonical_id=None,
        location="Remote",
        department="AI",
        expertise="General",
        commitment=None,
        active=True,
    ):
        cursor = self.conn.execute(
            """
            INSERT INTO jobs (
              company_id, canonical_opportunity_id, external_id, title,
              location, department, expertise, commitment, url, source_hash,
              opportunity_kind, availability_basis,
              include_in_live_market_estimate, first_seen_at, last_seen_at,
              is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'live_posting', 'api_feed', 1, ?, ?, ?)
            """,
            (
                self.company_id,
                canonical_id,
                f"external-{source_hash}",
                title,
                location,
                department,
                expertise,
                commitment,
                f"https://example.test/jobs/{source_hash}",
                source_hash,
                NOW,
                NOW,
                int(active),
            ),
        )
        return cursor.lastrowid

    def insert_canonical(self, key="provider::kept", title="Provider Group"):
        cursor = self.conn.execute(
            """
            INSERT INTO canonical_opportunities (
              company_id, canonical_key, canonical_title, normalized_title,
              source_category, first_seen_at, last_seen_at, is_active,
              variant_count
            ) VALUES (?, ?, ?, ?, 'AI', ?, ?, 1, 1)
            """,
            (self.company_id, key, title, title.casefold(), NOW, NOW),
        )
        return cursor.lastrowid

    def fallback_enriched_job(self):
        job_id = self.insert_job(
            "fallback-source-hash",
            "Senior Portuguese Software Developer and AI Trainer - Python",
            location="Brazil - Remote",
            expertise="Software Engineering",
            commitment="Freelance; Rate: USD 25/hr; 10-20 hours per week",
        )
        self.assertEqual(
            sync_fallback_canonical_opportunities(self.conn, self.company_id),
            1,
        )
        canonical_id = self.conn.execute(
            "SELECT canonical_opportunity_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()["canonical_opportunity_id"]
        return job_id, canonical_id

    def rich_source_body(self, suffix=""):
        return (
            "Build and review Python services. Python is required. SQL is preferred. "
            "Evaluate technical work for quality. Collaborate with engineers to improve "
            "model-generated code, explain defects, and write clear technical feedback. "
            "The work includes reading specifications, testing implementations, and "
            "documenting reproducible findings. "
            + ("Use source evidence carefully. " * 8)
            + suffix
        )

    def persist_rich_source(
        self,
        job_id,
        body=None,
        *,
        source_updated_at="2026-08-16T00:00:00+00:00",
    ):
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        candidate = JobCandidate(
            external_id=row["external_id"],
            title=row["title"],
            location=row["location"],
            url=row["url"],
            department=row["department"],
            expertise=row["expertise"],
            commitment=row["commitment"],
            source_hash=row["source_hash"],
            source_body=body or self.rich_source_body(),
            source_body_format="text/plain",
            source_metadata={"provider_field": "provider value"},
            source_updated_at=source_updated_at,
        )
        result = upsert_job_source_content(
            self.conn,
            job_id,
            "appen",
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
                payload_shape="fixture:v1",
                schema_fingerprint="fixture-v1",
            ),
        )
        return result.material_content_sha256

    def llm_payload_fixture(self):
        job_id, canonical_id = self.fallback_enriched_job()
        self.persist_rich_source(job_id)
        packet, evidence_blocks = llm_source_packet(
            load_semantic_input(self.conn, canonical_id)
        )
        payload = FakeStructuredLLM().enrich(packet).payload
        block_ids = list(evidence_blocks)
        self.assertGreaterEqual(len(block_ids), 3)
        return payload, evidence_blocks, block_ids

    def test_duplicate_semantic_value_with_identical_evidence_is_normalized(self):
        payload, evidence_blocks, block_ids = self.llm_payload_fixture()
        payload["responsibilities"] = [
            {"value": "Review labeled data", "evidence": [block_ids[0]]},
            {"value": " review   LABELED data ", "evidence": [block_ids[0]]},
        ]

        normalized = normalize_llm_list_fields(payload, evidence_blocks)

        self.assertEqual(
            normalized["responsibilities"],
            [{"value": "Review labeled data", "evidence": [block_ids[0]]}],
        )
        validate_llm_payload(normalized, evidence_blocks)

    def test_duplicate_semantic_value_merges_different_valid_evidence(self):
        payload, evidence_blocks, block_ids = self.llm_payload_fixture()
        payload["responsibilities"] = [
            {"value": "Review labeled data", "evidence": [block_ids[0]]},
            {"value": "Review labeled data", "evidence": [block_ids[1]]},
        ]

        normalized = normalize_llm_list_fields(payload, evidence_blocks)

        self.assertEqual(
            normalized["responsibilities"],
            [
                {
                    "value": "Review labeled data",
                    "evidence": [block_ids[0], block_ids[1]],
                }
            ],
        )
        validate_llm_payload(normalized, evidence_blocks)

    def test_list_normalization_preserves_first_value_and_evidence_order(self):
        payload, evidence_blocks, block_ids = self.llm_payload_fixture()
        payload["responsibilities"] = [
            {"value": "First task", "evidence": [block_ids[0]]},
            {"value": "Second task", "evidence": [block_ids[1]]},
            {"value": "first task", "evidence": [block_ids[2], block_ids[0]]},
        ]

        normalized = normalize_llm_list_fields(payload, evidence_blocks)

        self.assertEqual(
            normalized["responsibilities"],
            [
                {
                    "value": "First task",
                    "evidence": [block_ids[0], block_ids[2]],
                },
                {"value": "Second task", "evidence": [block_ids[1]]},
            ],
        )

    def test_duplicate_evidence_references_are_normalized_for_lists_and_scalars(self):
        payload, evidence_blocks, block_ids = self.llm_payload_fixture()
        payload["quick_take"]["evidence"] = [
            block_ids[0],
            block_ids[0],
            block_ids[1],
            block_ids[0],
        ]
        payload["responsibilities"][0]["evidence"] = [
            block_ids[0],
            block_ids[0],
        ]

        normalized = normalize_llm_list_fields(payload, evidence_blocks)

        self.assertEqual(
            normalized["quick_take"]["evidence"],
            [block_ids[0], block_ids[1]],
        )
        self.assertEqual(
            normalized["responsibilities"][0]["evidence"],
            [block_ids[0]],
        )
        validate_llm_payload(normalized, evidence_blocks)

    def test_unknown_evidence_survives_normalization_and_still_fails_validation(self):
        payload, evidence_blocks, block_ids = self.llm_payload_fixture()
        payload["quick_take"]["evidence"] = [
            block_ids[0],
            "E0000000000000000",
            block_ids[0],
        ]

        normalized = normalize_llm_list_fields(payload, evidence_blocks)

        self.assertEqual(
            normalized["quick_take"]["evidence"],
            [block_ids[0], "E0000000000000000"],
        )
        with self.assertRaisesRegex(
            EnrichmentValidationError,
            "unknown evidence block ID",
        ):
            validate_llm_payload(normalized, evidence_blocks)

    def test_duplicate_with_invalid_evidence_still_fails(self):
        payload, evidence_blocks, block_ids = self.llm_payload_fixture()
        payload["responsibilities"] = [
            {"value": "Review labeled data", "evidence": [block_ids[0]]},
            {"value": "Review labeled data", "evidence": ["evidence:invented"]},
        ]

        with self.assertRaisesRegex(
            EnrichmentValidationError,
            "unknown evidence block ID",
        ):
            normalize_llm_list_fields(payload, evidence_blocks)

    def test_distinct_values_are_not_merged_and_unknown_taxonomy_still_fails(self):
        payload, evidence_blocks, block_ids = self.llm_payload_fixture()
        payload["work_activities"] = [
            {"value": "software_development", "evidence": [block_ids[0]]},
            {"value": "software_testing", "evidence": [block_ids[1]]},
        ]
        normalized = normalize_llm_list_fields(payload, evidence_blocks)
        self.assertEqual(
            [item["value"] for item in normalized["work_activities"]],
            ["software_development", "software_testing"],
        )

        payload["work_activities"].append(
            {"value": "invented_activity", "evidence": [block_ids[2]]}
        )
        with self.assertRaisesRegex(EnrichmentValidationError, "unsupported"):
            normalize_llm_list_fields(payload, evidence_blocks)

    def test_evidence_blocks_are_explicit_deterministic_and_stable(self):
        job_id, canonical_id = self.fallback_enriched_job()
        self.persist_rich_source(job_id)
        semantic_input = load_semantic_input(self.conn, canonical_id)

        first_packet, first_blocks = llm_source_packet(semantic_input)
        second_packet, second_blocks = llm_source_packet(semantic_input)

        self.assertEqual(first_packet, second_packet)
        self.assertEqual(first_blocks, second_blocks)
        self.assertEqual(
            set(first_blocks),
            {item["evidence_block_id"] for item in first_packet["evidence_blocks"]},
        )
        self.assertTrue(
            all(re.fullmatch(r"E[0-9a-f]{16}", block_id) for block_id in first_blocks)
        )
        self.assertTrue(
            any(item["kind"] == "body_paragraph" for item in first_blocks.values())
        )
        self.assertTrue(
            any(
                item["kind"] == "metadata_field"
                and item["label"] == "metadata.provider_field"
                for item in first_blocks.values()
            )
        )
        self.assertTrue(
            any(item["kind"] == "listing_field" for item in first_blocks.values())
        )
        self.assertEqual(
            {item["authority_class"] for item in first_blocks.values()},
            {
                "accepted_body_evidence",
                "page_metadata_context",
                "variant_listing_evidence",
            },
        )

        changed_input = copy.deepcopy(semantic_input)
        changed_input["rich_content"][0]["body"] = (
            "A newly inserted provider paragraph.\n\n"
            + changed_input["rich_content"][0]["body"]
        )
        _, changed_blocks = llm_source_packet(changed_input)
        original_body_ids = {
            block_id
            for block_id, block in first_blocks.items()
            if block["kind"] == "body_paragraph"
        }
        self.assertTrue(original_body_ids.issubset(changed_blocks))

    def test_deterministic_activity_classification_ignores_incidental_categories(self):
        job_id, canonical_id = self.fallback_enriched_job()
        self.conn.execute(
            """
            UPDATE jobs
            SET title = 'Frontend Developer',
                department = 'Creator (Writer); Coding',
                expertise = 'Coding'
            WHERE id = ?
            """,
            (job_id,),
        )
        self.conn.execute(
            """
            UPDATE canonical_opportunities
            SET canonical_title = 'Frontend Developer',
                normalized_title = 'frontend developer'
            WHERE id = ?
            """,
            (canonical_id,),
        )

        result = enrich_canonical_opportunity(
            self.conn,
            canonical_id,
            now=NOW,
        )

        self.assertEqual(
            result["document"]["attributes"]["role"]["work_activities"],
            ["software_development"],
        )

    def test_deterministic_research_study_is_not_research_analysis_work(self):
        job_id, canonical_id = self.fallback_enriched_job()
        self.conn.execute(
            "UPDATE jobs SET title = 'Colby Research Study Participant' WHERE id = ?",
            (job_id,),
        )
        self.conn.execute(
            """
            UPDATE canonical_opportunities
            SET canonical_title = 'Colby Research Study Participant',
                normalized_title = 'colby research study participant'
            WHERE id = ?
            """,
            (canonical_id,),
        )

        result = enrich_canonical_opportunity(self.conn, canonical_id, now=NOW)

        self.assertNotIn(
            "research_analysis",
            result["document"]["attributes"]["role"]["work_activities"],
        )

    def test_semantic_guards_reject_domains_incidental_activities_and_constraints(self):
        blocks = {
            "E1": {
                "kind": "metadata_field",
                "label": "metadata.category",
                "content": "metadata.category: Creator (Writer)",
            },
            "E2": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": "Participate in a research study by recording videos.",
            },
            "E3": {
                "kind": "metadata_field",
                "label": "metadata.skills.0",
                "content": "metadata.skills.0: legal research",
            },
            "E4": {
                "kind": "body_paragraph",
                "label": "body paragraph 2",
                "content": "Review footage before submission and tag every video.",
            },
            "E5": {
                "kind": "body_paragraph",
                "label": "body paragraph 3",
                "content": (
                    "All household members must be age 5 or older. "
                    "No children under 5 may live in the home."
                ),
            },
        }
        payload = {
            "role_family": {"value": "data_collection", "evidence": ["E2"]},
            "professional_domains": [
                {"value": "technical", "evidence": ["E1"]},
                {"value": "legal", "evidence": ["E3"]},
            ],
            "work_activities": [
                {"value": "writing_editing", "evidence": ["E1"]},
                {"value": "research_analysis", "evidence": ["E2"]},
                {"value": "data_collection", "evidence": ["E2"]},
            ],
            "skills_required": [
                {"value": "Review footage before submission", "evidence": ["E4"]},
                {"value": "Reliable computer and internet connection", "evidence": ["E2"]},
                {
                    "value": "Ensure videos meet technical specifications",
                    "evidence": ["E4"],
                },
                {
                    "value": "Provide videos in accepted file formats",
                    "evidence": ["E4"],
                },
                {
                    "value": "capture video at specified technical standards",
                    "evidence": ["E4"],
                },
                {
                    "value": "recording or sourcing high-quality real-world video footage",
                    "evidence": ["E4"],
                },
                {
                    "value": "reviewing footage to ensure it meets project quality standards",
                    "evidence": ["E4"],
                },
                {"value": "Python", "evidence": ["E2"]},
            ],
            "skills_preferred": [
                {"value": "Legal research", "evidence": ["E3"]},
            ],
            "responsibilities": [
                {"value": "Review footage before submission", "evidence": ["E4"]},
            ],
            "candidate_profile": {"value": None, "evidence": []},
            "quick_take": {"value": None, "evidence": []},
            "caveats": [
                {"value": "Remote, flexible schedule at $20 per hour", "evidence": ["E2"]},
                {"value": "You must sign an NDA and pass a test", "evidence": ["E2"]},
                {"value": "Videos must not infringe privacy or copyright", "evidence": ["E2"]},
            ],
        }
        document = blank_document()
        document["attributes"]["role"]["professional_domains"] = ["legal"]
        document["attributes"]["role"]["work_activities"] = ["data_collection"]

        guarded = apply_llm_semantic_acceptance_guards(payload, blocks, document)

        self.assertEqual(
            [item["value"] for item in guarded["professional_domains"]],
            [],
        )
        self.assertEqual(
            [item["value"] for item in guarded["work_activities"]],
            ["data_collection"],
        )
        self.assertEqual(
            [item["value"] for item in guarded["skills_required"]],
            ["Python"],
        )
        self.assertEqual(guarded["skills_preferred"], [])
        self.assertEqual(
            [item["value"] for item in guarded["caveats"]],
            [
                "Videos must not infringe privacy or copyright",
                "All household members must be 5 years or older; "
                "no children under 5 may live in the home.",
            ],
        )
        self.assertEqual(guarded["caveats"][1]["evidence"], ["E5"])

    def test_semantic_guards_reject_context_metadata_and_unsupported_currency(self):
        blocks = {
            "ELANG": {
                "kind": "metadata_field",
                "label": "metadata.language",
                "content": "metadata.language: en",
            },
            "EOFFICE": {
                "kind": "metadata_field",
                "label": "metadata.offices.0.name",
                "content": "metadata.offices.0.name: Worldwide - Remote",
            },
            "EPAY": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": "Pay is $20 per hour.",
            },
        }
        payload = blank_llm_payload()
        payload["languages"] = [
            {
                "value": {
                    "language": "english",
                    "locale": None,
                    "requirement_mode": "single",
                },
                "evidence": ["ELANG"],
            }
        ]
        payload["compensation_currency"] = {
            "value": "USD",
            "evidence": ["EPAY"],
        }
        payload["workplace_mode"] = {
            "value": "remote",
            "evidence": ["EOFFICE"],
        }
        payload["location_scope"] = {
            "value": "remote_worldwide",
            "evidence": ["EOFFICE"],
        }

        guarded = apply_llm_semantic_acceptance_guards(
            payload,
            blocks,
            blank_document(),
        )

        self.assertEqual(guarded["languages"], [])
        self.assertEqual(
            guarded["compensation_currency"],
            {"value": None, "evidence": []},
        )
        self.assertEqual(
            guarded["workplace_mode"],
            {"value": None, "evidence": []},
        )
        self.assertEqual(
            guarded["location_scope"],
            {"value": None, "evidence": []},
        )

    def test_semantic_guards_preserve_explicit_sensitive_facts(self):
        blocks = {
            "ELANG": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": "Candidates must be fluent in English.",
            },
            "ELOC": {
                "kind": "body_paragraph",
                "label": "body paragraph 2",
                "content": "This remote role is open only to candidates in the United States.",
            },
            "EPAY": {
                "kind": "body_paragraph",
                "label": "body paragraph 3",
                "content": "Pay is USD 20 per hour.",
            },
        }
        payload = blank_llm_payload()
        payload["languages"] = [
            {
                "value": {
                    "language": "english",
                    "locale": None,
                    "requirement_mode": "single",
                },
                "evidence": ["ELANG"],
            }
        ]
        payload["compensation_currency"] = {
            "value": "USD",
            "evidence": ["EPAY"],
        }
        payload["location_scope"] = {
            "value": "remote_restricted",
            "evidence": ["ELOC"],
        }
        payload["eligible_countries"] = [
            {"value": "United States", "evidence": ["ELOC"]}
        ]

        guarded = apply_llm_semantic_acceptance_guards(
            payload,
            blocks,
            blank_document(),
        )

        self.assertEqual(len(guarded["languages"]), 1)
        self.assertEqual(guarded["compensation_currency"]["value"], "USD")
        self.assertEqual(guarded["location_scope"]["value"], "remote_restricted")
        self.assertEqual(
            [item["value"] for item in guarded["eligible_countries"]],
            ["United States"],
        )

    def test_semantic_guards_reclassify_modality_and_experience(self):
        blocks = {
            "EFOUNDER": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": "Founder experience is ideal, but it is not required.",
            },
            "ECOMM": {
                "kind": "body_paragraph",
                "label": "body paragraph 2",
                "content": "Clear metacognitive communication is essential.",
            },
            "EYEARS": {
                "kind": "body_paragraph",
                "label": "body paragraph 3",
                "content": (
                    "Minimum of 2 years of retirement plan administration "
                    "experience is required."
                ),
            },
        }
        payload = blank_llm_payload()
        payload["skills_required"] = [
            {"value": "Founder experience", "evidence": ["EFOUNDER"]}
        ]
        payload["skills_preferred"] = [
            {"value": "Clear metacognitive communication", "evidence": ["ECOMM"]}
        ]
        payload["years_experience_min"] = {
            "value": 2,
            "evidence": ["EYEARS"],
        }

        guarded = apply_llm_semantic_acceptance_guards(
            payload,
            blocks,
            blank_document(),
        )

        self.assertEqual(
            [item["value"] for item in guarded["skills_required"]],
            ["Clear metacognitive communication"],
        )
        self.assertEqual(guarded["skills_preferred"], [])
        self.assertEqual(
            [item["value"] for item in guarded["experience_preferred"]],
            ["Founder experience"],
        )
        self.assertEqual(
            [item["value"] for item in guarded["experience_required"]],
            ["retirement plan administration experience"],
        )

    def test_semantic_guards_require_experiential_value_before_changing_family(self):
        blocks = {
            "ETRAITS": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": (
                    "No prior AI or technical experience is necessary; what matters "
                    "is curiosity, strong communication, attention to detail, and "
                    "willingness to engage thoughtfully with the work."
                ),
            },
            "EFAMILIAR": {
                "kind": "body_paragraph",
                "label": "body paragraph 2",
                "content": (
                    "Prior research experience is a plus, and familiarity with "
                    "inspection workflows is strongly valued."
                ),
            },
        }
        payload = blank_llm_payload()
        payload["skills_required"] = [
            {"value": value, "evidence": ["ETRAITS"]}
            for value in (
                "Curiosity",
                "Strong communication",
                "Attention to detail",
                "Willingness to engage thoughtfully",
            )
        ]
        payload["skills_preferred"] = [
            {
                "value": "Familiarity with inspection workflows",
                "evidence": ["EFAMILIAR"],
            }
        ]

        guarded = apply_llm_semantic_acceptance_guards(
            payload,
            blocks,
            blank_document(),
        )

        self.assertEqual(
            [item["value"] for item in guarded["skills_required"]],
            [
                "Curiosity",
                "Strong communication",
                "Attention to detail",
                "Willingness to engage thoughtfully",
            ],
        )
        self.assertEqual(
            [item["value"] for item in guarded["skills_preferred"]],
            ["Familiarity with inspection workflows"],
        )
        self.assertEqual(guarded["experience_required"], [])
        self.assertEqual(guarded["experience_preferred"], [])

    def test_semantic_guards_preserve_real_experience_and_its_modality(self):
        blocks = {
            "ELAW": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": (
                    "Current or former law enforcement professionals must have "
                    "real-world law enforcement experience."
                ),
            },
            "EPYTHON": {
                "kind": "body_paragraph",
                "label": "body paragraph 2",
                "content": "Real-world Python experience is a strong signal of fit.",
            },
            "EYEARS": {
                "kind": "body_paragraph",
                "label": "body paragraph 3",
                "content": (
                    "A minimum of 2 years of retirement plan administration "
                    "experience is required."
                ),
            },
        }
        payload = blank_llm_payload()
        payload["experience_required"] = [
            {
                "value": "Real-world law enforcement experience",
                "evidence": ["ELAW"],
            }
        ]
        payload["skills_required"] = [
            {"value": "Real-world Python experience", "evidence": ["EPYTHON"]},
            {
                "value": (
                    "At least 2 years of retirement plan administration experience"
                ),
                "evidence": ["EYEARS"],
            },
        ]
        payload["years_experience_min"] = {
            "value": 2,
            "evidence": ["EYEARS"],
        }

        guarded = apply_llm_semantic_acceptance_guards(
            payload,
            blocks,
            blank_document(),
        )

        self.assertEqual(guarded["skills_required"], [])
        self.assertEqual(
            [item["value"] for item in guarded["experience_required"]],
            [
                "Real-world law enforcement experience",
                "At least 2 years of retirement plan administration experience",
            ],
        )
        self.assertEqual(guarded["years_experience_min"]["value"], 2)
        self.assertEqual(
            [item["value"] for item in guarded["experience_preferred"]],
            ["Real-world Python experience"],
        )

    def test_language_modes_reconcile_by_meaning_without_invalid_duplicates(self):
        one_language = opportunity_enrichment._resolve_language_requirement_values(
            [
                {"language": "french", "locale": None, "requirement_mode": "single"},
                {
                    "language": "french",
                    "locale": None,
                    "requirement_mode": "all_required",
                },
            ]
        )
        bilingual = opportunity_enrichment._resolve_language_requirement_values(
            [
                {"language": "french", "locale": None, "requirement_mode": "single"},
                {
                    "language": "french",
                    "locale": None,
                    "requirement_mode": "all_required",
                },
                {
                    "language": "english",
                    "locale": None,
                    "requirement_mode": "all_required",
                },
            ]
        )
        irreconcilable = opportunity_enrichment._resolve_language_requirement_values(
            [
                {"language": "french", "locale": None, "requirement_mode": "single"},
                {
                    "language": "french",
                    "locale": None,
                    "requirement_mode": "any_supported",
                },
            ]
        )
        locale_narrowed = opportunity_enrichment._resolve_language_requirement_values(
            [
                {"language": "chinese", "locale": None, "requirement_mode": "single"},
                {
                    "language": "chinese",
                    "locale": "Mandarin",
                    "requirement_mode": "all_required",
                },
            ]
        )
        distinct_locales = opportunity_enrichment._resolve_language_requirement_values(
            [
                {"language": "spanish", "locale": None, "requirement_mode": "single"},
                {"language": "spanish", "locale": "Cordobes", "requirement_mode": "single"},
                {"language": "spanish", "locale": "Rioplatense", "requirement_mode": "single"},
            ]
        )

        self.assertEqual(one_language[0]["requirement_mode"], "single")
        self.assertEqual(
            {item["requirement_mode"] for item in bilingual},
            {"all_required"},
        )
        self.assertEqual(irreconcilable[0]["requirement_mode"], "ambiguous")
        self.assertEqual(
            locale_narrowed,
            [
                {
                    "language": "chinese",
                    "locale": "mandarin",
                    "requirement_mode": "single",
                }
            ],
        )
        self.assertEqual(
            [(item["language"], item["locale"]) for item in distinct_locales],
            [
                ("spanish", None),
                ("spanish", "cordobes"),
                ("spanish", "rioplatense"),
            ],
        )

    def test_language_guard_accepts_explicit_bilingual_work_not_page_language(self):
        blocks = {
            "EBODY": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": (
                    "On a typical day, you will converse with the model in both "
                    "English and French."
                ),
            },
            "EMETA": {
                "kind": "metadata_field",
                "label": "metadata.language",
                "content": "metadata.language: English",
            },
        }
        payload = blank_llm_payload()
        payload["languages"] = [
            {
                "value": {
                    "language": language,
                    "locale": None,
                    "requirement_mode": "all_required",
                },
                "evidence": [evidence],
            }
            for language, evidence in (
                ("english", "EBODY"),
                ("french", "EBODY"),
                ("english", "EMETA"),
            )
        ]

        guarded = apply_llm_semantic_acceptance_guards(
            payload, blocks, blank_document()
        )

        self.assertEqual(len(guarded["languages"]), 2)
        self.assertEqual(
            {item["value"]["language"] for item in guarded["languages"]},
            {"english", "french"},
        )

    def test_language_guard_uses_locale_evidence_but_not_preferred_dialect_fluency(self):
        blocks = {
            "EMANDARIN": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": "Verified Mandarin language proficiency of C1 or C2.",
            },
            "ECORDOBES": {
                "kind": "body_paragraph",
                "label": "body paragraph 2",
                "content": (
                    "Native-level Cordobes fluency is preferred, but Cordobes "
                    "dialect expertise is required."
                ),
            },
        }
        payload = blank_llm_payload()
        payload["languages"] = [
            {
                "value": {
                    "language": "chinese",
                    "locale": "Mandarin",
                    "requirement_mode": "single",
                },
                "evidence": ["EMANDARIN"],
            },
            {
                "value": {
                    "language": "spanish",
                    "locale": "Cordobes",
                    "requirement_mode": "single",
                },
                "evidence": ["ECORDOBES"],
            },
        ]
        payload["skills_required"] = [
            {"value": "Cordobes dialect expertise", "evidence": ["ECORDOBES"]}
        ]
        payload["skills_preferred"] = [
            {"value": "Native-level Cordobes fluency", "evidence": ["ECORDOBES"]}
        ]

        guarded = apply_llm_semantic_acceptance_guards(
            payload, blocks, blank_document()
        )

        self.assertEqual(
            [item["value"] for item in guarded["languages"]],
            [
                {
                    "language": "chinese",
                    "locale": "Mandarin",
                    "requirement_mode": "single",
                }
            ],
        )
        self.assertEqual(
            [item["value"] for item in guarded["skills_required"]],
            ["Cordobes dialect expertise"],
        )
        self.assertEqual(
            [item["value"] for item in guarded["skills_preferred"]],
            ["Native-level Cordobes fluency"],
        )

    def test_mixed_background_or_interest_is_not_required_experience(self):
        blocks = {
            "ESPORT": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": (
                    "Candidates need a sports-industry background or strong "
                    "interest in sports."
                ),
            }
        }
        payload = blank_llm_payload()
        payload["experience_required"] = [
            {
                "value": "Sports-industry background or strong interest in sports",
                "evidence": ["ESPORT"],
            }
        ]

        guarded = apply_llm_semantic_acceptance_guards(
            payload, blocks, blank_document()
        )

        self.assertEqual(guarded["experience_required"], [])
        self.assertEqual(
            [item["value"] for item in guarded["skills_required"]],
            ["Sports-industry background or strong interest in sports"],
        )

    def test_current_professional_status_has_its_own_required_field(self):
        blocks = {
            "EOWNER": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": (
                    "We're looking for current small-business owners and co-owners "
                    "to advise on real operational decisions."
                ),
            }
        }
        payload = blank_llm_payload()
        payload["experience_preferred"] = [
            {
                "value": "Small-business owner or co-owner",
                "evidence": ["EOWNER"],
            }
        ]

        guarded = apply_llm_semantic_acceptance_guards(
            payload, blocks, blank_document()
        )

        self.assertEqual(guarded["experience_required"], [])
        self.assertEqual(guarded["experience_preferred"], [])
        self.assertEqual(
            [item["value"] for item in guarded["current_status_requirements"]],
            ["Small-business owner or co-owner"],
        )

    def test_current_status_accepts_equivalent_owning_verb(self):
        blocks = {
            "EOWNER": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": "We're looking for current owners and co-owners.",
            }
        }
        payload = blank_llm_payload()
        payload["current_status_requirements"] = [
            {
                "value": "Currently own or co-own a small business",
                "evidence": ["EOWNER"],
            }
        ]

        guarded = apply_llm_semantic_acceptance_guards(
            payload, blocks, blank_document()
        )

        self.assertEqual(len(guarded["current_status_requirements"]), 1)

    def test_current_status_asset_and_experience_families_keep_separate_authority(self):
        blocks = {
            "EOWNER": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": (
                    "Demonstrable professional expertise as an active owner or "
                    "operator of a local business."
                ),
            },
            "EASSET": {
                "kind": "body_paragraph",
                "label": "body paragraph 2",
                "content": (
                    "Required asset: must currently possess and actively manage "
                    "a verified business profile."
                ),
            },
            "EEXPERTISE": {
                "kind": "body_paragraph",
                "label": "body paragraph 3",
                "content": "Contractors must possess deep finance expertise.",
            },
            "EMIXED": {
                "kind": "body_paragraph",
                "label": "body paragraph 4",
                "content": (
                    "Contractors must possess direct operational experience and "
                    "verified digital storefront assets."
                ),
            },
            "EHISTORY": {
                "kind": "body_paragraph",
                "label": "body paragraph 5",
                "content": "Prior work experience operating retail stores is required.",
            },
        }
        payload = blank_llm_payload()
        payload["experience_required"] = [
            {
                "value": "Professional expertise as an active owner or operator",
                "evidence": ["EOWNER"],
            },
            {
                "value": "Deep finance expertise",
                "evidence": ["EEXPERTISE"],
            },
            {
                "value": "Direct operational experience and verified digital storefront assets",
                "evidence": ["EMIXED"],
            },
            {
                "value": "Prior work experience operating retail stores",
                "evidence": ["EHISTORY"],
            },
        ]
        payload["current_status_requirements"] = [
            {
                "value": "Currently possess and manage a verified business profile",
                "evidence": ["EASSET"],
            },
            {
                "value": "Direct operational experience as an active owner or operator",
                "evidence": ["EOWNER"],
            },
        ]

        guarded = apply_llm_semantic_acceptance_guards(
            payload, blocks, blank_document()
        )

        self.assertEqual(
            [item["value"] for item in guarded["current_status_requirements"]],
            [
                "Currently possess and manage a verified business profile",
                "Active owner or operator",
                "Professional expertise as an active owner or operator",
            ],
        )
        self.assertEqual(
            [item["value"] for item in guarded["skills_required"]],
            ["Deep finance expertise"],
        )
        self.assertEqual(
            [item["value"] for item in guarded["experience_required"]],
            ["Prior work experience operating retail stores"],
        )

    def test_targeted_domain_specialist_expertise_is_required_without_inference(self):
        blocks = {
            "EINSURANCE": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": (
                    "We're looking for insurance specialists to evaluate "
                    "insurance-domain model responses, especially with deep "
                    "knowledge of regulations in French-speaking regions."
                ),
            }
        }
        payload = blank_llm_payload()
        payload["skills_required"] = [
            {
                "value": (
                    "Insurance expertise, including deep knowledge of regulations "
                    "in French-speaking regions"
                ),
                "evidence": ["EINSURANCE"],
            }
        ]
        document = blank_document()
        document["source"]["canonical_title"] = "Insurance Specialist - French"

        guarded = apply_llm_semantic_acceptance_guards(
            payload, blocks, document
        )

        self.assertEqual(
            [item["value"] for item in guarded["skills_required"]],
            ["Insurance expertise"],
        )
        self.assertEqual(
            [item["value"] for item in guarded["skills_preferred"]],
            [
                "Insurance expertise, including deep knowledge of regulations "
                "in French-speaking regions"
            ],
        )
        self.assertEqual(guarded["experience_required"], [])
        self.assertEqual(guarded["credentials"], [])

    def test_nearby_preference_modifier_controls_its_fact_not_role_identity(self):
        blocks = {
            "EEDUCATION": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": (
                    "We're looking for education specialists with curriculum knowledge, "
                    "especially with experience in French-speaking education systems."
                ),
            }
        }
        payload = blank_llm_payload()
        payload["experience_preferred"] = [
            {
                "value": "Experience in French-speaking education systems",
                "evidence": ["EEDUCATION"],
            }
        ]

        guarded = apply_llm_semantic_acceptance_guards(
            payload, blocks, blank_document()
        )

        self.assertEqual(guarded["experience_required"], [])
        self.assertEqual(
            [item["value"] for item in guarded["experience_preferred"]],
            ["Experience in French-speaking education systems"],
        )

    def test_domain_expertise_is_not_dropped_as_compensation_metadata(self):
        blocks = {
            "EHR": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": (
                    "We're looking for HR specialists with expertise in talent "
                    "acquisition, compensation and benefits, and employee relations."
                ),
            }
        }
        payload = blank_llm_payload()
        payload["skills_required"] = [
            {
                "value": (
                    "HR expertise in talent acquisition, compensation and benefits, "
                    "and employee relations"
                ),
                "evidence": ["EHR"],
            }
        ]

        guarded = apply_llm_semantic_acceptance_guards(
            payload, blocks, blank_document()
        )

        self.assertEqual(len(guarded["skills_required"]), 1)

    def test_activity_guard_keeps_only_direct_action_evidence(self):
        blocks = {
            "EAI": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": "You will converse with the model and verify factual accuracy.",
            },
            "EOPS": {
                "kind": "body_paragraph",
                "label": "body paragraph 2",
                "content": "Manage client onboarding workflows and monitor progress.",
            },
            "EANNOTATE": {
                "kind": "body_paragraph",
                "label": "body paragraph 3",
                "content": "Review AI-generated content and annotate errors.",
            },
            "ECONTEXT": {
                "kind": "listing_field",
                "label": "listing.title",
                "content": "AI Operations and Data Specialist",
            },
            "EUNSUPPORTED": {
                "kind": "body_paragraph",
                "label": "body paragraph 4",
                "content": "Join the data collection team for this project.",
            },
        }
        payload = blank_llm_payload()
        payload["work_activities"] = [
            {"value": "ai_training_evaluation", "evidence": ["EAI", "ECONTEXT"]},
            {"value": "operations", "evidence": ["EOPS", "ECONTEXT"]},
            {"value": "data_annotation", "evidence": ["EANNOTATE", "ECONTEXT"]},
            {"value": "data_collection", "evidence": ["EUNSUPPORTED"]},
        ]

        guarded = apply_llm_semantic_acceptance_guards(
            payload, blocks, blank_document()
        )

        self.assertEqual(
            [item["value"] for item in guarded["work_activities"]],
            ["ai_training_evaluation", "operations", "data_annotation"],
        )
        self.assertEqual(
            [item["evidence"] for item in guarded["work_activities"]],
            [["EAI"], ["EOPS"], ["EANNOTATE"]],
        )

    def test_domain_and_activity_guards_require_role_coherent_body_actions(self):
        blocks = {
            "EQA": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": (
                    "Quality Assurance and Fact-Checking: ensure generated tasks "
                    "and scoring criteria reflect industry standards."
                ),
            },
            "EFEATURE": {
                "kind": "body_paragraph",
                "label": "body paragraph 2",
                "content": "Test newly deployed platform features and digital tools.",
            },
            "ELATEX": {
                "kind": "body_paragraph",
                "label": "body paragraph 3",
                "content": (
                    "LaTeX specialists perform document typesetting, mathematical "
                    "formatting, and package troubleshooting."
                ),
            },
            "EMATH": {
                "kind": "body_paragraph",
                "label": "body paragraph 4",
                "content": (
                    "Mathematics specialists apply mathematical reasoning and "
                    "proof analysis to evaluate model answers."
                ),
            },
            "ERESOURCE": {
                "kind": "body_paragraph",
                "label": "body paragraph 5",
                "content": (
                    "Develop educational resources and feedback documentation "
                    "from observed quality trends."
                ),
            },
        }
        payload = blank_llm_payload()
        payload["professional_domains"] = [
            {"value": "mathematics", "evidence": ["ELATEX", "EMATH"]}
        ]
        payload["work_activities"] = [
            {"value": "software_testing", "evidence": ["EQA", "EFEATURE"]},
            {"value": "writing_editing", "evidence": ["ERESOURCE"]},
        ]

        guarded = apply_llm_semantic_acceptance_guards(
            payload, blocks, blank_document()
        )

        self.assertEqual(
            guarded["professional_domains"],
            [{"value": "mathematics", "evidence": ["EMATH"]}],
        )
        self.assertEqual(
            guarded["work_activities"],
            [
                {"value": "software_testing", "evidence": ["EFEATURE"]},
                {"value": "writing_editing", "evidence": ["ERESOURCE"]},
            ],
        )

    def test_vnext_conflicts_clear_canonical_projection_and_preserve_scoped_facts(self):
        canonical_id = self.insert_canonical(
            key="provider::conflicting-location",
            title="English Language Specialist",
        )
        job_id = self.insert_job(
            "conflicting-location",
            "English Language Specialist",
            canonical_id=canonical_id,
            location="World Wide - Remote",
        )
        self.persist_rich_source(
            job_id,
            body=(
                "Workplace type: Remote US Only. "
                + "Review language data against the supplied rubric. " * 12
            ),
        )

        document = extract_deterministic_document(
            load_semantic_input(self.conn, canonical_id)
        )

        self.assertEqual(
            document["attributes"]["work_arrangement"]["location_scope"],
            "unknown",
        )
        self.assertEqual(
            {
                fact["value"]
                for fact in document["variant_facts"]
                if fact["field_path"]
                == "attributes.work_arrangement.location_scope"
            },
            {"remote_restricted", "remote_worldwide"},
        )
        self.assertIn(
            "attributes.work_arrangement.location_scope",
            document["unknown_fields"],
        )

    def test_vnext_compensation_range_accepts_nonbreaking_hyphen(self):
        canonical_id = self.insert_canonical(
            key="provider::nonbreaking-pay",
            title="Python Coding Specialist",
        )
        job_id = self.insert_job(
            "nonbreaking-pay",
            "Python Coding Specialist",
            canonical_id=canonical_id,
        )
        self.persist_rich_source(
            job_id,
            body=(
                "Pay range: $8‑to‑$65 per hour. "
                + "Review Python solutions against project requirements. " * 12
            ),
        )

        document = extract_deterministic_document(
            load_semantic_input(self.conn, canonical_id)
        )
        compensation = document["attributes"]["compensation"]

        self.assertEqual(compensation["amount_min"], 8)
        self.assertEqual(compensation["amount_max"], 65)
        self.assertIsNone(compensation["currency"])

    def test_skill_task_distinction_fixtures(self):
        fixtures = {
            "genuine skills": {
                "Python": False,
                "video editing": False,
                "data collection": False,
                "knowledge of H.264 encoding": False,
                "proficiency with Adobe Premiere Pro": False,
                "video recording expertise": False,
            },
            "responsibilities phrased as verbs": {
                "Prepare video files in an accepted format": True,
                "Review footage before submission": True,
                "Tag each video with metadata": True,
            },
            "gerund tasks": {
                "preparing video files in accepted formats": True,
                "reviewing footage before submission": True,
                "uploading recordings to the project portal": True,
            },
            "compound noun task phrases": {
                "video recording and sourcing of real-world footage": True,
                "footage review and submission of video files": True,
                "preparation of video files": True,
            },
            "mixed phrases with real capabilities": {
                "experience preparing video files in H.264": False,
                "ability to record and source real-world footage": False,
                "skilled in reviewing and tagging video submissions": False,
            },
        }

        for category, examples in fixtures.items():
            for value, expected in examples.items():
                with self.subTest(category=category, value=value):
                    self.assertEqual(skill_is_task_description(value), expected)

    def test_semantic_guard_fails_closed_on_obvious_role_activity_conflict(self):
        blocks = {
            "E1": {
                "kind": "body_paragraph",
                "label": "body paragraph 1",
                "content": "Evaluate AI model responses for quality.",
            }
        }
        payload = {
            "role_family": {"value": "administrative_support", "evidence": ["E1"]},
            "professional_domains": [],
            "work_activities": [],
            "skills_required": [],
            "skills_preferred": [],
            "responsibilities": [],
            "candidate_profile": {"value": None, "evidence": []},
            "quick_take": {"value": None, "evidence": []},
            "caveats": [],
        }
        document = blank_document()
        document["source"]["canonical_title"] = (
            "Ex-MBB Strategy Consultant - AI Training (Remote)"
        )
        document["attributes"]["role"]["role_family"] = "writing_editing"
        document["attributes"]["role"]["work_activities"] = [
            "ai_training_evaluation"
        ]
        document["field_evidence"] = [
            {
                "field_path": "attributes.role.role_family",
                "source_ref": "title",
                "evidence_text": "Creator (Writer)",
                "basis": "deterministic_classification",
                "confidence": "medium",
            }
        ]

        guarded = apply_llm_semantic_acceptance_guards(payload, blocks, document)

        self.assertEqual(guarded["role_family"], {"value": None, "evidence": []})
        self.assertIsNone(document["attributes"]["role"]["role_family"])
        self.assertEqual(document["field_evidence"], [])

    def test_source_text_preserves_valid_unicode_punctuation(self):
        text = source_body_text(
            "<p>Review the project’s 7–10 day recording period.</p>",
            "text/html",
        )

        self.assertEqual(text, "Review the project’s 7–10 day recording period.")
        self.assertNotIn("\ufffd", text)

    def test_fresh_and_existing_schema_install_enrichment_tables(self):
        for table in (
            "job_source_contents",
            "opportunity_enrichments",
            "opportunity_enrichment_overrides",
            "opportunity_enrichment_runs",
            "opportunity_enrichment_run_diagnostics",
        ):
            self.assertIsNotNone(
                self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
            )

        self.conn.execute("DROP TABLE opportunity_enrichment_overrides")
        self.conn.execute("DROP TABLE opportunity_enrichments")
        install_base_schema(self.conn)
        install_base_schema(self.conn)
        enrichment_columns = {
            row["name"]
            for row in self.conn.execute(
                "PRAGMA table_info(opportunity_enrichments)"
            ).fetchall()
        }
        self.assertTrue(
            {
                "schema_version",
                "taxonomy_version",
                "extractor_version",
                "input_sha256",
                "status",
                "automatic_document_json",
                "model_provider",
                "model_name",
                "prompt_version",
                "semantic_input_version",
                "derivation_fingerprint",
            }.issubset(enrichment_columns)
        )
        run_columns = {
            row["name"]
            for row in self.conn.execute(
                "PRAGMA table_info(opportunity_enrichment_runs)"
            ).fetchall()
        }
        self.assertTrue(
            {"semantic_input_version", "derivation_fingerprint"}.issubset(
                run_columns
            )
        )

    def test_new_rows_persist_explicit_input_and_derivation_identity(self):
        _job_id, canonical_id = self.fallback_enriched_job()

        result = enrich_canonical_opportunity(self.conn, canonical_id, now=NOW)
        stored = dict(
            self.conn.execute(
                "SELECT * FROM opportunity_enrichments "
                "WHERE canonical_opportunity_id = ?",
                (canonical_id,),
            ).fetchone()
        )
        freshness = classify_enrichment_freshness(
            load_semantic_input(self.conn, canonical_id),
            stored,
        )

        self.assertEqual(result["semantic_input_version"], SEMANTIC_INPUT_VERSION)
        self.assertEqual(stored["semantic_input_version"], SEMANTIC_INPUT_VERSION)
        self.assertEqual(
            stored["derivation_fingerprint"],
            derivation_recipe_fingerprint(),
        )
        self.assertEqual(
            result["derivation_fingerprint"],
            stored["derivation_fingerprint"],
        )
        self.assertEqual(freshness["freshness"], "current")
        self.assertEqual(freshness["derivation_status"], "current")

    def test_additive_version_columns_leave_legacy_rows_and_overrides_untouched(self):
        legacy = sqlite3.connect(":memory:")
        legacy.row_factory = sqlite3.Row
        legacy.execute("PRAGMA foreign_keys = ON")
        legacy.execute(
            "CREATE TABLE canonical_opportunities (id INTEGER PRIMARY KEY)"
        )
        legacy.execute(
            """
            CREATE TABLE jobs (
              id INTEGER PRIMARY KEY,
              semantic_authority_state TEXT NOT NULL DEFAULT 'legacy_accepted'
                CHECK (semantic_authority_state IN (
                  'legacy_accepted', 'pending', 'versioned_accepted'
                )),
              authority_fixture_marker TEXT
            )
            """
        )
        legacy.execute("INSERT INTO canonical_opportunities (id) VALUES (1)")
        old_enrichment_statement = OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[1].replace(
            "      semantic_input_version TEXT,\n", ""
        ).replace("      derivation_fingerprint TEXT,\n", "")
        legacy.execute(old_enrichment_statement)
        old_run_statement = OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[2].replace(
            "      semantic_input_version TEXT,\n", ""
        ).replace("      derivation_fingerprint TEXT,\n", "")
        legacy.execute(old_run_statement)
        legacy.execute(OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[4])
        legacy.execute(
            "INSERT INTO opportunity_enrichments ("
            "canonical_opportunity_id, schema_version, taxonomy_version, "
            "extractor_version, input_sha256, status, automatic_document_json, "
            "generated_at) VALUES (1, ?, ?, 'deterministic_v1', ?, 'partial', '{}', ?)",
            (SCHEMA_VERSION, TAXONOMY_VERSION, "a" * 64, NOW),
        )
        legacy.execute(
            "INSERT INTO opportunity_enrichment_runs ("
            "canonical_opportunity_id, input_sha256, outcome, model_provider, "
            "model_name, prompt_version, started_at, finished_at) "
            "VALUES (1, ?, 'failed', 'openai', 'legacy-model', 'legacy-prompt', ?, ?)",
            ("a" * 64, NOW, NOW),
        )
        legacy.execute(
            "INSERT INTO opportunity_enrichment_overrides ("
            "canonical_opportunity_id, field_path, operation, value_json, actor, reason) "
            "VALUES (1, 'attributes.requirements.languages', 'set', '[\"English\"]', "
            "'reviewer', 'verified')"
        )
        override_before = dict(
            legacy.execute("SELECT * FROM opportunity_enrichment_overrides").fetchone()
        )

        ensure_opportunity_enrichment_schema(legacy)

        row = dict(legacy.execute("SELECT * FROM opportunity_enrichments").fetchone())
        run = dict(
            legacy.execute("SELECT * FROM opportunity_enrichment_runs").fetchone()
        )
        override_after = dict(
            legacy.execute("SELECT * FROM opportunity_enrichment_overrides").fetchone()
        )
        self.assertIsNone(row["semantic_input_version"])
        self.assertIsNone(row["derivation_fingerprint"])
        self.assertIsNone(run["semantic_input_version"])
        self.assertIsNone(run["derivation_fingerprint"])
        self.assertEqual(row["extractor_version"], "deterministic_v1")
        self.assertEqual(override_after, override_before)
        cursor = legacy.cursor()
        cursor.row_factory = None
        self.assertTrue(attest_opportunity_enrichment_schema_extension(cursor))
        cursor.close()
        legacy.close()

    def test_freshness_attributes_legacy_contract_debt_separately_from_source_change(self):
        job_id, canonical_id = self.fallback_enriched_job()
        self.persist_rich_source(job_id)
        semantic_input = load_semantic_input(self.conn, canonical_id)
        legacy_hash = semantic_input_sha256(
            semantic_input,
            LEGACY_SEMANTIC_INPUT_VERSION,
        )
        legacy = {
            "schema_version": SCHEMA_VERSION,
            "taxonomy_version": TAXONOMY_VERSION,
            "extractor_version": "deterministic_v1",
            "input_sha256": legacy_hash,
            "model_provider": None,
            "model_name": None,
            "prompt_version": None,
        }

        contract_debt = classify_enrichment_freshness(semantic_input, legacy)

        self.assertEqual(contract_debt["freshness"], "stale")
        self.assertEqual(contract_debt["source_input_status"], "current")
        self.assertEqual(
            contract_debt["stale_reasons"],
            [STALE_REASON_DERIVATION_CONTRACT_CHANGED],
        )
        self.assertEqual(
            contract_debt["semantic_input_version_basis"],
            "legacy_extractor_mapping",
        )
        self.assertEqual(contract_debt["comparable_input_sha256"], legacy_hash)
        self.assertNotEqual(contract_debt["current_input_sha256"], legacy_hash)

        current = {
            **legacy,
            "extractor_version": EXTRACTOR_VERSION,
            "semantic_input_version": SEMANTIC_INPUT_VERSION,
            "input_sha256": semantic_input_sha256(semantic_input),
            "derivation_fingerprint": derivation_recipe_fingerprint(),
        }
        changed_input = copy.deepcopy(semantic_input)
        changed_input["variants"][0]["title"] += " changed"
        source_change = classify_enrichment_freshness(changed_input, current)

        self.assertEqual(source_change["freshness"], "stale")
        self.assertEqual(source_change["derivation_status"], "current")
        self.assertEqual(
            source_change["stale_reasons"],
            [STALE_REASON_SOURCE_INPUT_CHANGED],
        )

    def test_historical_extractor_mappings_pin_literal_semantic_input_versions(self):
        self.assertEqual(
            opportunity_enrichment.LEGACY_SEMANTIC_INPUT_VERSION_BY_EXTRACTOR,
            {
                "deterministic_v1": "opportunity_semantic_input_v1",
                "deterministic_plus_structured_llm_v1": (
                    "opportunity_semantic_input_v2"
                ),
            },
        )

    def test_future_semantic_input_bump_stales_current_v3_without_source_change(self):
        job_id, canonical_id = self.fallback_enriched_job()
        self.persist_rich_source(job_id)
        semantic_input = load_semantic_input(self.conn, canonical_id)
        v3_hash = semantic_input_sha256(semantic_input)
        current_v3 = {
            "schema_version": SCHEMA_VERSION,
            "taxonomy_version": TAXONOMY_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "semantic_input_version": SEMANTIC_INPUT_VERSION,
            "input_sha256": v3_hash,
            "derivation_fingerprint": derivation_recipe_fingerprint(),
            "model_provider": None,
            "model_name": None,
            "prompt_version": None,
        }

        before = classify_enrichment_freshness(semantic_input, current_v3)
        future_input = copy.deepcopy(semantic_input)
        future_input["future_semantic_signal"] = {"value": "v3-only"}
        module_path = Path(opportunity_enrichment.__file__)
        current_source = module_path.read_text(encoding="utf-8")
        current_assignment = (
            'SEMANTIC_INPUT_VERSION = "opportunity_semantic_input_v3"'
        )
        future_assignment = (
            'SEMANTIC_INPUT_VERSION = "opportunity_semantic_input_v4"'
        )
        self.assertEqual(current_source.count(current_assignment), 1)
        future_source = current_source.replace(
            current_assignment,
            future_assignment,
            1,
        )
        future_module = {
            "__name__": "wahojobs.opportunity_enrichment_future_contract_test",
            "__package__": "wahojobs",
            "__file__": str(module_path),
        }
        exec(compile(future_source, str(module_path), "exec"), future_module)
        self.assertEqual(
            future_module["LEGACY_SEMANTIC_INPUT_VERSION_BY_EXTRACTOR"][
                "deterministic_plus_structured_llm_v1"
            ],
            "opportunity_semantic_input_v2",
        )
        after = future_module["classify_enrichment_freshness"](
            future_input,
            current_v3,
        )

        self.assertEqual(before["freshness"], "current")
        self.assertEqual(before["derivation_status"], "current")
        self.assertEqual(after["freshness"], "stale")
        self.assertEqual(
            after["stored_semantic_input_version"],
            "opportunity_semantic_input_v3",
        )
        self.assertEqual(
            after["current_semantic_input_version"],
            "opportunity_semantic_input_v4",
        )
        self.assertEqual(after["derivation_status"], "changed")
        self.assertEqual(after["source_input_status"], "current")
        self.assertEqual(after["comparable_input_sha256"], v3_hash)
        self.assertEqual(
            after["stale_reasons"],
            [STALE_REASON_DERIVATION_CONTRACT_CHANGED],
        )

    def test_future_recipe_bump_stales_current_v3_independently(self):
        job_id, canonical_id = self.fallback_enriched_job()
        self.persist_rich_source(job_id)
        semantic_input = load_semantic_input(self.conn, canonical_id)
        current_v3 = {
            "schema_version": SCHEMA_VERSION,
            "taxonomy_version": TAXONOMY_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "semantic_input_version": SEMANTIC_INPUT_VERSION,
            "input_sha256": semantic_input_sha256(semantic_input),
            "derivation_fingerprint": derivation_recipe_fingerprint(),
            "model_provider": None,
            "model_name": None,
            "prompt_version": None,
        }

        with patch.object(
            opportunity_enrichment,
            "derivation_recipe_fingerprint",
            return_value="f" * 64,
        ):
            freshness = classify_enrichment_freshness(semantic_input, current_v3)

        self.assertEqual(freshness["freshness"], "stale")
        self.assertEqual(freshness["derivation_status"], "changed")
        self.assertEqual(freshness["source_input_status"], "current")
        self.assertEqual(
            freshness["stale_reasons"],
            [STALE_REASON_DERIVATION_CONTRACT_CHANGED],
        )

    def test_unknown_historical_extractor_fails_conservatively(self):
        job_id, canonical_id = self.fallback_enriched_job()
        self.persist_rich_source(job_id)
        semantic_input = load_semantic_input(self.conn, canonical_id)
        unknown = {
            "schema_version": SCHEMA_VERSION,
            "taxonomy_version": TAXONOMY_VERSION,
            "extractor_version": "unknown_historical_extractor",
            "input_sha256": semantic_input_sha256(semantic_input),
            "model_provider": None,
            "model_name": None,
            "prompt_version": None,
        }

        freshness = classify_enrichment_freshness(semantic_input, unknown)

        self.assertEqual(freshness["freshness"], "stale")
        self.assertEqual(freshness["semantic_input_version_basis"], "unknown")
        self.assertIsNone(freshness["stored_semantic_input_version"])
        self.assertEqual(freshness["source_input_status"], "not_comparable")
        self.assertEqual(freshness["derivation_status"], "changed")
        self.assertEqual(
            freshness["stale_reasons"],
            [STALE_REASON_DERIVATION_CONTRACT_CHANGED],
        )

    def test_derivation_fingerprint_changes_intentionally_with_recipe_inputs(self):
        baseline = derivation_recipe_fingerprint()

        self.assertNotEqual(
            baseline,
            derivation_recipe_fingerprint(
                recipe_version=DERIVATION_RECIPE_VERSION + "_next"
            ),
        )
        self.assertNotEqual(
            baseline,
            derivation_recipe_fingerprint(
                model_provider="openai",
                model_name="future-model",
                prompt_version="future-prompt",
            ),
        )

    def test_rich_source_content_is_persisted_with_provenance_and_material_hash(self):
        job_id, canonical_id = self.fallback_enriched_job()
        first_hash = self.persist_rich_source(job_id)
        first = dict(
            self.conn.execute(
                "SELECT * FROM job_source_contents WHERE job_id = ?", (job_id,)
            ).fetchone()
        )
        first_enrichment = enrich_canonical_opportunity(self.conn, canonical_id, now=NOW)

        same_hash = self.persist_rich_source(
            job_id,
            source_updated_at="2026-08-17T00:00:00+00:00",
        )
        second_enrichment = enrich_canonical_opportunity(
            self.conn, canonical_id, now="2026-08-17T00:00:00+00:00"
        )
        self.assertEqual(first_hash, same_hash)
        self.assertEqual(first["provider"], "appen")
        self.assertEqual(first["source_type"], "fixture")
        self.assertEqual(first["source_url"], "https://example.test/jobs/fallback-source-hash")
        self.assertEqual(first["body_format"], "text/plain")
        self.assertEqual(json.loads(first["metadata_json"])["provider_field"], "provider value")
        self.assertEqual(first_enrichment["input_sha256"], second_enrichment["input_sha256"])
        self.assertEqual(second_enrichment["outcome"], "unchanged")

        changed_hash = self.persist_rich_source(
            job_id,
            self.rich_source_body(" Materially new responsibility."),
            source_updated_at="2026-08-18T00:00:00+00:00",
        )
        changed = enrich_canonical_opportunity(
            self.conn, canonical_id, now="2026-08-18T00:00:00+00:00"
        )
        self.assertNotEqual(first_hash, changed_hash)
        self.assertNotEqual(first_enrichment["input_sha256"], changed["input_sha256"])

    def test_valid_evidence_block_refs_enrich_once_and_reprocess_material_change(self):
        job_id, canonical_id = self.fallback_enriched_job()
        self.persist_rich_source(job_id)
        fake = FakeStructuredLLM()

        first = enrich_canonical_opportunity(
            self.conn,
            canonical_id,
            now=NOW,
            llm_client=fake,
        )
        second = enrich_canonical_opportunity(
            self.conn,
            canonical_id,
            now="2026-08-17T00:00:00+00:00",
            llm_client=fake,
        )
        self.assertEqual(first["llm"]["outcome"], "succeeded")
        self.assertEqual(second["outcome"], "unchanged")
        self.assertFalse(second["llm"]["called"])
        self.assertEqual(len(fake.calls), 1)
        attributes = first["document"]["attributes"]
        self.assertEqual(attributes["requirements"]["skills_required"], ["Python"])
        self.assertEqual(attributes["requirements"]["skills_preferred"], ["SQL"])
        self.assertEqual(
            attributes["content"]["quick_take"],
            "Build Python services and evaluate technical quality.",
        )
        self.assertTrue(
            all(
                item["basis"] != "llm_source_evidence"
                or item["evidence_text"] in self.rich_source_body()
                for item in first["document"]["field_evidence"]
            )
        )

        row = self.conn.execute(
            "SELECT * FROM opportunity_enrichments WHERE canonical_opportunity_id = ?",
            (canonical_id,),
        ).fetchone()
        self.assertEqual(row["model_provider"], "openai")
        self.assertEqual(row["model_name"], "gpt-5-mini")
        self.assertEqual(row["prompt_version"], PROMPT_VERSION)
        self.assertEqual(llm_usage_observability(self.conn)["calls"], 1)
        diagnostic = json.loads(
            self.conn.execute(
                """
                SELECT d.diagnostic_json
                FROM opportunity_enrichment_run_diagnostics d
                JOIN opportunity_enrichment_runs r ON r.id = d.run_id
                WHERE r.canonical_opportunity_id = ?
                ORDER BY r.id
                LIMIT 1
                """,
                (canonical_id,),
            ).fetchone()["diagnostic_json"]
        )
        self.assertEqual(diagnostic["category"], "succeeded")
        self.assertEqual(diagnostic["response_status"], "completed")

        self.persist_rich_source(
            job_id,
            self.rich_source_body(" The provider added new review work."),
            source_updated_at="2026-08-18T00:00:00+00:00",
        )
        changed = enrich_canonical_opportunity(
            self.conn,
            canonical_id,
            now="2026-08-18T00:00:00+00:00",
            llm_client=fake,
        )
        self.assertEqual(changed["llm"]["outcome"], "succeeded")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(llm_usage_observability(self.conn)["calls"], 2)

    def test_duplicate_work_activity_is_normalized_before_final_validation(self):
        job_id, canonical_id = self.fallback_enriched_job()
        self.persist_rich_source(job_id)
        fake = FakeStructuredLLM(duplicate_work_activity=True)

        result = enrich_canonical_opportunity(
            self.conn,
            canonical_id,
            now=NOW,
            llm_client=fake,
        )

        self.assertEqual(result["llm"]["outcome"], "succeeded")
        self.assertEqual(len(fake.calls), 1)
        llm_evidence = [
            item
            for item in result["document"]["field_evidence"]
            if item["field_path"] == "attributes.role.work_activities"
            and item["basis"] == "llm_source_evidence"
        ]
        self.assertEqual(len(llm_evidence), 1)

    def test_new_prompt_version_reprocesses_unchanged_source_once(self):
        job_id, canonical_id = self.fallback_enriched_job()
        self.persist_rich_source(job_id)
        original = FakeStructuredLLM()
        first = enrich_canonical_opportunity(
            self.conn,
            canonical_id,
            now=NOW,
            llm_client=original,
        )
        revised = FakeStructuredLLM()
        revised.prompt_version = "opportunity_semantic_next"
        second = enrich_canonical_opportunity(
            self.conn,
            canonical_id,
            now="2026-08-17T00:00:00+00:00",
            llm_client=revised,
        )
        third = enrich_canonical_opportunity(
            self.conn,
            canonical_id,
            now="2026-08-18T00:00:00+00:00",
            llm_client=revised,
        )

        self.assertEqual(first["llm"]["outcome"], "succeeded")
        self.assertEqual(second["llm"]["outcome"], "succeeded")
        self.assertTrue(second["llm"]["called"])
        self.assertEqual(len(revised.calls), 1)
        self.assertEqual(third["llm"]["outcome"], "already_enriched")
        self.assertFalse(third["llm"]["called"])

    def test_failed_new_version_preserves_previous_successful_document(self):
        job_id, canonical_id = self.fallback_enriched_job()
        self.persist_rich_source(job_id)
        successful = FakeStructuredLLM()
        first = enrich_canonical_opportunity(
            self.conn,
            canonical_id,
            now=NOW,
            llm_client=successful,
        )
        stored_before = dict(
            self.conn.execute(
                """
                SELECT automatic_document_json, model_provider, model_name,
                       prompt_version, generated_at, updated_at
                FROM opportunity_enrichments
                WHERE canonical_opportunity_id = ?
                """,
                (canonical_id,),
            ).fetchone()
        )

        failing = FakeStructuredLLM(unknown_evidence=True)
        failing.prompt_version = "opportunity_semantic_failed_next"
        second = enrich_canonical_opportunity(
            self.conn,
            canonical_id,
            now="2026-08-17T00:00:00+00:00",
            llm_client=failing,
        )
        stored_after = dict(
            self.conn.execute(
                """
                SELECT automatic_document_json, model_provider, model_name,
                       prompt_version, generated_at, updated_at
                FROM opportunity_enrichments
                WHERE canonical_opportunity_id = ?
                """,
                (canonical_id,),
            ).fetchone()
        )
        third = enrich_canonical_opportunity(
            self.conn,
            canonical_id,
            now="2026-08-18T00:00:00+00:00",
            llm_client=failing,
        )
        runs = self.conn.execute(
            """
            SELECT prompt_version, outcome
            FROM opportunity_enrichment_runs
            WHERE canonical_opportunity_id = ?
            ORDER BY id
            """,
            (canonical_id,),
        ).fetchall()

        self.assertEqual(first["llm"]["outcome"], "succeeded")
        self.assertEqual(second["llm"]["outcome"], "failed")
        self.assertTrue(second["llm"]["preserved_previous_success"])
        self.assertEqual(second["outcome"], "unchanged")
        self.assertEqual(second["document"], first["document"])
        self.assertEqual(stored_after, stored_before)
        self.assertEqual(third["llm"]["outcome"], "already_attempted")
        self.assertFalse(third["llm"]["called"])
        self.assertEqual(len(failing.calls), 1)
        self.assertEqual(
            [(row["prompt_version"], row["outcome"]) for row in runs],
            [
                (PROMPT_VERSION, "succeeded"),
                ("opportunity_semantic_failed_next", "failed"),
            ],
        )

    def test_short_source_content_does_not_trigger_llm(self):
        job_id, canonical_id = self.fallback_enriched_job()
        self.persist_rich_source(job_id, "Short provider note.")
        fake = FakeStructuredLLM()

        result = enrich_canonical_opportunity(
            self.conn,
            canonical_id,
            now=NOW,
            llm_client=fake,
        )

        self.assertEqual(result["llm"]["outcome"], "not_eligible")
        self.assertFalse(result["llm"]["called"])
        self.assertEqual(fake.calls, [])
        self.assertEqual(llm_usage_observability(self.conn)["calls"], 0)

    def test_unknown_llm_evidence_block_is_rejected_and_not_retried_unchanged(self):
        job_id, canonical_id = self.fallback_enriched_job()
        self.persist_rich_source(job_id)
        fake = FakeStructuredLLM(unknown_evidence=True)

        first = enrich_canonical_opportunity(
            self.conn, canonical_id, now=NOW, llm_client=fake
        )
        second = enrich_canonical_opportunity(
            self.conn,
            canonical_id,
            now="2026-08-17T00:00:00+00:00",
            llm_client=fake,
        )
        self.assertEqual(first["llm"]["outcome"], "failed")
        self.assertIsNone(first["document"]["attributes"]["content"]["quick_take"])
        self.assertEqual(second["outcome"], "unchanged")
        self.assertEqual(second["llm"]["outcome"], "already_attempted")
        self.assertEqual(len(fake.calls), 1)
        run = self.conn.execute(
            "SELECT * FROM opportunity_enrichment_runs WHERE canonical_opportunity_id = ?",
            (canonical_id,),
        ).fetchone()
        self.assertEqual(run["outcome"], "failed")
        self.assertEqual(run["error_type"], "EnrichmentValidationError")
        self.assertEqual(run["semantic_input_version"], SEMANTIC_INPUT_VERSION)
        self.assertEqual(
            run["derivation_fingerprint"],
            derivation_recipe_fingerprint(
                model_provider=fake.provider,
                model_name=fake.model,
                prompt_version=fake.prompt_version,
            ),
        )
        diagnostic = json.loads(
            self.conn.execute(
                "SELECT diagnostic_json FROM opportunity_enrichment_run_diagnostics WHERE run_id = ?",
                (run["id"],),
            ).fetchone()["diagnostic_json"]
        )
        self.assertEqual(diagnostic["category"], "evidence_validation")

    def test_invalid_llm_schema_has_distinct_sanitized_diagnostic(self):
        job_id, canonical_id = self.fallback_enriched_job()
        self.persist_rich_source(job_id)
        fake = FakeStructuredLLM(invalid_schema=True)

        result = enrich_canonical_opportunity(
            self.conn,
            canonical_id,
            now=NOW,
            llm_client=fake,
        )

        self.assertEqual(result["llm"]["outcome"], "failed")
        run = self.conn.execute(
            "SELECT * FROM opportunity_enrichment_runs WHERE canonical_opportunity_id = ?",
            (canonical_id,),
        ).fetchone()
        diagnostic = json.loads(
            self.conn.execute(
                "SELECT diagnostic_json FROM opportunity_enrichment_run_diagnostics WHERE run_id = ?",
                (run["id"],),
            ).fetchone()["diagnostic_json"]
        )
        self.assertEqual(diagnostic["category"], "schema_validation")
        self.assertEqual(diagnostic["validation_error_type"], "EnrichmentValidationError")

    def test_human_override_remains_above_regenerated_llm_enrichment(self):
        job_id, canonical_id = self.fallback_enriched_job()
        self.persist_rich_source(job_id)
        fake = FakeStructuredLLM()
        enrich_canonical_opportunity(self.conn, canonical_id, now=NOW, llm_client=fake)
        save_override(
            self.conn,
            canonical_id,
            "attributes.content.quick_take",
            "set",
            value="Human-reviewed Quick Take",
            actor="reviewer@example.test",
            reason="Human review",
            now=NOW,
        )
        self.persist_rich_source(
            job_id,
            self.rich_source_body(" The provider materially changed this role."),
            source_updated_at="2026-08-17T00:00:00+00:00",
        )
        enrich_canonical_opportunity(
            self.conn,
            canonical_id,
            now="2026-08-17T00:00:00+00:00",
            llm_client=fake,
        )
        effective = resolve_effective_enrichment(self.conn, canonical_id)
        self.assertEqual(
            effective["document"]["attributes"]["content"]["quick_take"],
            "Human-reviewed Quick Take",
        )
        self.assertEqual(
            effective["field_sources"]["attributes.content.quick_take"],
            "human_override",
        )

    def test_optional_closed_schema_extension_is_exact_or_rejected(self):
        cursor = self.conn.cursor()
        cursor.row_factory = None
        self.assertTrue(attest_opportunity_enrichment_schema_extension(cursor))
        self.conn.execute("DROP INDEX idx_opportunity_enrichments_status")
        with self.assertRaises(OpportunityEnrichmentSchemaError):
            attest_opportunity_enrichment_schema_extension(cursor)
        cursor.close()

    def test_exact_prior_v2_extension_is_accepted_for_additive_upgrade(self):
        self.conn.execute("DROP TABLE job_source_content_acceptances")
        self.conn.execute("DROP TABLE job_source_content_captures")
        self.conn.execute(
            "ALTER TABLE jobs DROP COLUMN semantic_authority_state"
        )
        self.conn.execute("DROP TABLE opportunity_enrichment_run_diagnostics")
        self.conn.execute("DROP TABLE opportunity_enrichment_runs")
        self.conn.execute("DROP TABLE job_source_contents")
        cursor = self.conn.cursor()
        cursor.row_factory = None
        self.assertTrue(attest_opportunity_enrichment_schema_extension(cursor))

        install_base_schema(self.conn)
        self.assertTrue(attest_opportunity_enrichment_schema_extension(cursor))
        cursor.close()

    def test_prior_rich_extension_adds_run_diagnostics_table(self):
        self.conn.execute("DROP TABLE opportunity_enrichment_run_diagnostics")
        cursor = self.conn.cursor()
        cursor.row_factory = None
        self.assertTrue(attest_opportunity_enrichment_schema_extension(cursor))

        install_base_schema(self.conn)
        self.assertTrue(attest_opportunity_enrichment_schema_extension(cursor))
        cursor.close()

    def test_fallback_is_one_job_per_hash_and_preserves_existing_links(self):
        existing_canonical_id = self.insert_canonical()
        linked_job = self.insert_job(
            "provider-linked", "Provider linked", canonical_id=existing_canonical_id
        )
        fallback_job = self.insert_job("only-this-hash", "Unlinked role")
        simulation_job = self.insert_job(
            "simulation-hash", "[SIMULATION] Test role"
        )

        self.assertEqual(
            sync_fallback_canonical_opportunities(self.conn, self.company_id), 1
        )
        rows = {
            row["id"]: row
            for row in self.conn.execute(
                "SELECT id, canonical_opportunity_id FROM jobs"
            ).fetchall()
        }
        self.assertEqual(
            rows[linked_job]["canonical_opportunity_id"], existing_canonical_id
        )
        self.assertIsNotNone(rows[fallback_job]["canonical_opportunity_id"])
        self.assertIsNone(rows[simulation_job]["canonical_opportunity_id"])
        fallback = self.conn.execute(
            "SELECT * FROM canonical_opportunities WHERE id = ?",
            (rows[fallback_job]["canonical_opportunity_id"],),
        ).fetchone()
        self.assertEqual(fallback["canonical_key"], "raw::only-this-hash")
        self.assertEqual(fallback["variant_count"], 1)
        self.assertEqual(
            sync_fallback_canonical_opportunities(self.conn, self.company_id), 0
        )

    def test_deterministic_extraction_reuses_matching_metadata(self):
        _job_id, canonical_id = self.fallback_enriched_job()
        result = enrich_canonical_opportunity(self.conn, canonical_id, now=NOW)
        attributes = result["document"]["attributes"]

        self.assertEqual(result["outcome"], "created")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["document"]["source"]["company_slug"], "appen")
        self.assertEqual(result["document"]["source"]["source_tier"], "core")
        self.assertEqual(
            result["document"]["source"]["opportunity_kinds"], ["live_posting"]
        )
        self.assertEqual(attributes["role"]["role_family"], "software_engineering")
        self.assertIn("technical", attributes["role"]["professional_domains"])
        self.assertIn("software_development", attributes["role"]["work_activities"])
        self.assertIn("python", attributes["role"]["specializations"])
        self.assertEqual(attributes["role"]["seniority"], "senior")
        self.assertEqual(attributes["work_arrangement"]["workplace_mode"], "remote")
        self.assertEqual(
            attributes["work_arrangement"]["location_scope"], "remote_restricted"
        )
        self.assertEqual(
            attributes["work_arrangement"]["eligible_countries"], ["Brazil"]
        )
        self.assertEqual(
            attributes["work_arrangement"]["engagement_type"], "freelance"
        )
        self.assertEqual(attributes["work_arrangement"]["hours_per_week_min"], 10)
        self.assertEqual(attributes["work_arrangement"]["hours_per_week_max"], 20)
        self.assertEqual(
            attributes["requirements"]["languages"][0]["language"], "portuguese"
        )
        self.assertEqual(attributes["compensation"]["currency"], "USD")
        self.assertEqual(attributes["compensation"]["amount_min"], 25)
        self.assertEqual(attributes["compensation"]["period"], "hour")
        self.assertIsNone(attributes["content"]["quick_take"])

    def test_semantic_hash_ignores_timestamps_and_unchanged_run_does_not_write(self):
        job_id, canonical_id = self.fallback_enriched_job()
        first = enrich_canonical_opportunity(
            self.conn, canonical_id, now="2026-08-16T12:01:00+00:00"
        )
        stored_before = dict(
            self.conn.execute(
                "SELECT * FROM opportunity_enrichments WHERE canonical_opportunity_id = ?",
                (canonical_id,),
            ).fetchone()
        )
        self.conn.execute(
            "UPDATE jobs SET last_seen_at = ?, updated_at = ? WHERE id = ?",
            ("2026-08-17T00:00:00+00:00", "2026-08-17T00:00:00+00:00", job_id),
        )
        second = enrich_canonical_opportunity(
            self.conn, canonical_id, now="2026-08-17T12:00:00+00:00"
        )
        stored_after = dict(
            self.conn.execute(
                "SELECT * FROM opportunity_enrichments WHERE canonical_opportunity_id = ?",
                (canonical_id,),
            ).fetchone()
        )
        self.assertEqual(second["outcome"], "unchanged")
        self.assertEqual(first["input_sha256"], second["input_sha256"])
        self.assertEqual(stored_before, stored_after)

        self.conn.execute(
            "UPDATE jobs SET commitment = 'Full-time; USD 30/hr' WHERE id = ?",
            (job_id,),
        )
        changed = enrich_canonical_opportunity(
            self.conn, canonical_id, now="2026-08-18T12:00:00+00:00"
        )
        self.assertEqual(changed["outcome"], "updated")
        self.assertNotEqual(first["input_sha256"], changed["input_sha256"])

    def test_overrides_win_and_survive_automatic_refresh(self):
        job_id, canonical_id = self.fallback_enriched_job()
        enrich_canonical_opportunity(self.conn, canonical_id, now=NOW)
        self.assertEqual(
            save_override(
                self.conn,
                canonical_id,
                "attributes.role.role_family",
                "set",
                value="translation_localization",
                actor="reviewer@example.test",
                reason="Reviewed source description",
                now="2026-08-16T12:05:00+00:00",
            ),
            "created",
        )
        self.assertEqual(
            save_override(
                self.conn,
                canonical_id,
                "attributes.work_arrangement.workplace_mode",
                "set_unknown",
                actor="reviewer@example.test",
                reason="Source did not establish workplace mode",
                now="2026-08-16T12:06:00+00:00",
            ),
            "created",
        )

        self.conn.execute(
            "UPDATE jobs SET title = 'Senior Portuguese Finance Expert' WHERE id = ?",
            (job_id,),
        )
        enrich_canonical_opportunity(
            self.conn, canonical_id, now="2026-08-17T12:00:00+00:00"
        )
        effective = resolve_effective_enrichment(self.conn, canonical_id)
        self.assertEqual(
            effective["document"]["attributes"]["role"]["role_family"],
            "translation_localization",
        )
        self.assertEqual(
            effective["document"]["attributes"]["work_arrangement"]["workplace_mode"],
            "unknown",
        )
        self.assertEqual(
            set(effective["stale_override_fields"]),
            {
                "attributes.role.role_family",
                "attributes.work_arrangement.workplace_mode",
            },
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS count FROM opportunity_enrichment_overrides WHERE canonical_opportunity_id = ?",
                (canonical_id,),
            ).fetchone()["count"],
            2,
        )
        with self.assertRaises(EnrichmentValidationError):
            save_override(
                self.conn,
                canonical_id,
                "attributes.role.not_a_field",
                "set",
                value="invented",
                actor="reviewer@example.test",
                reason="Invalid test",
            )

    def test_reviewed_overlay_imports_as_validated_overrides(self):
        _job_id, canonical_id = self.fallback_enriched_job()
        enrich_canonical_opportunity(self.conn, canonical_id, now=NOW)
        overlay_path = Path(self.temporary.name) / "overlay.json"
        overlay_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "records": [
                        {
                            "stable_opportunity_key": "source_hash:appen:fallback-source-hash",
                            "source": "appen",
                            "source_hash": "fallback-source-hash",
                            "title": "Portuguese role",
                            "required_languages": ["Portuguese"],
                            "language_locale": ["Portuguese (Brazil)"],
                            "location_restriction": ["Brazil"],
                            "provenance": [
                                {
                                    "review_id": "review-1",
                                    "evidence_text": "Reviewed title and location",
                                }
                            ],
                            "warnings": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        first = import_reviewed_overlay(self.conn, overlay_path)
        second = import_reviewed_overlay(self.conn, overlay_path)
        effective = resolve_effective_enrichment(self.conn, canonical_id)
        arrangement = effective["document"]["attributes"]["work_arrangement"]
        languages = effective["document"]["attributes"]["requirements"]["languages"]
        self.assertEqual(first["records_imported"], 1)
        self.assertEqual(first["fields_created"], 2)
        self.assertEqual(second["fields_unchanged"], 2)
        self.assertEqual(arrangement["eligible_locations"], ["Brazil"])
        self.assertEqual(
            arrangement["location_scope"], "remote_restricted"
        )
        self.assertEqual(
            languages,
            [
                {
                    "language": "portuguese",
                    "locale": "Brazil",
                    "requirement_mode": "single",
                }
            ],
        )

    def test_successful_tracking_creates_fallback_and_enrichment(self):
        crawl_run_id = self.conn.execute(
            "INSERT INTO crawl_runs (company_id, status, started_at) VALUES (?, 'running', ?)",
            (self.company_id, NOW),
        ).lastrowid
        result = CompanyCrawlResult(
            jobs=[
                JobCandidate(
                    external_id="tracked-1",
                    title="Portuguese AI Trainer",
                    location="Remote worldwide",
                    url="https://example.test/jobs/tracked-1",
                    expertise="AI Training",
                    source_body="Complete authoritative tracking source body.",
                    source_body_format="text/plain",
                    source_updated_at=NOW,
                )
            ],
            used_sample_data=False,
            source_message="fixture",
            source_type="fixture",
            outcome=ProviderOutcome.SUCCESS,
            snapshot_complete=True,
            pagination_complete=True,
            raw_record_count=1,
            normalized_record_count=1,
        )
        track_crawl_result(self.conn, self.company_id, crawl_run_id, result, NOW)
        row = self.conn.execute(
            """
            SELECT j.canonical_opportunity_id, oe.status
            FROM jobs j
            JOIN opportunity_enrichments oe
              ON oe.canonical_opportunity_id = j.canonical_opportunity_id
            WHERE j.external_id = 'tracked-1'
            """
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["canonical_opportunity_id"])
        self.assertEqual(row["status"], "partial")

    def test_tracking_refreshes_only_materially_changed_semantic_inputs(self):
        def candidate(external_id, body, source_updated_at):
            return JobCandidate(
                external_id=external_id,
                title=f"Tracked role {external_id}",
                location="Remote worldwide",
                url=f"https://example.test/jobs/{external_id}",
                expertise="AI Training",
                source_body=body,
                source_body_format="text/plain",
                source_updated_at=source_updated_at,
            )

        stable_body = "Complete accepted body for the stable opportunity."
        changed_body = "Complete accepted body before its material revision."

        def crawl(jobs):
            return CompanyCrawlResult(
                jobs=jobs,
                used_sample_data=False,
                source_message="fixture",
                source_type="fixture",
                outcome=ProviderOutcome.SUCCESS,
                snapshot_complete=True,
                pagination_complete=True,
                raw_record_count=2,
                normalized_record_count=2,
            )

        first_run_id = self.conn.execute(
            "INSERT INTO crawl_runs (company_id, status, started_at) VALUES (?, 'running', ?)",
            (self.company_id, NOW),
        ).lastrowid
        with patch(
            "wahojobs.tracking.service.tracking_openai_client",
            return_value=None,
        ):
            track_crawl_result(
                self.conn,
                self.company_id,
                first_run_id,
                crawl(
                    [
                        candidate("changed", changed_body, NOW),
                        candidate("stable", stable_body, NOW),
                    ]
                ),
                NOW,
            )

        canonical_ids = {
            row["external_id"]: row["canonical_opportunity_id"]
            for row in self.conn.execute(
                "SELECT external_id, canonical_opportunity_id FROM jobs"
            ).fetchall()
        }
        stable_id = canonical_ids["stable"]
        changed_id = canonical_ids["changed"]
        self.conn.execute(
            """
            UPDATE opportunity_enrichments
            SET schema_version = 'recipe_migration_sentinel'
            WHERE canonical_opportunity_id = ?
            """,
            (stable_id,),
        )

        later = "2026-08-17T12:00:00+00:00"
        second_run_id = self.conn.execute(
            "INSERT INTO crawl_runs (company_id, status, started_at) VALUES (?, 'running', ?)",
            (self.company_id, later),
        ).lastrowid
        with patch(
            "wahojobs.tracking.service.tracking_openai_client",
            return_value=None,
        ):
            track_crawl_result(
                self.conn,
                self.company_id,
                second_run_id,
                crawl(
                    [
                        candidate(
                            "changed",
                            changed_body + " Materially changed requirements.",
                            later,
                        ),
                        candidate("stable", stable_body, later),
                    ]
                ),
                later,
            )

        stored_versions = {
            row["canonical_opportunity_id"]: row["schema_version"]
            for row in self.conn.execute(
                """
                SELECT canonical_opportunity_id, schema_version
                FROM opportunity_enrichments
                WHERE canonical_opportunity_id IN (?, ?)
                """,
                (changed_id, stable_id),
            ).fetchall()
        }
        self.assertEqual(stored_versions[changed_id], SCHEMA_VERSION)
        self.assertEqual(
            stored_versions[stable_id],
            "recipe_migration_sentinel",
        )

    def test_fallback_does_not_change_live_market_counting_policy(self):
        self.insert_job("market-count", "Unlinked Appen role")
        before = get_market_size_summary(self.conn)["estimated_market_opportunities"]
        sync_fallback_canonical_opportunities(self.conn, self.company_id)
        after = get_market_size_summary(self.conn)["estimated_market_opportunities"]
        self.assertEqual(before, 1)
        self.assertEqual(after, before)

    def test_vnext_objective_parser_uses_accepted_body_with_exact_evidence(self):
        canonical_id = self.insert_canonical(
            key="provider::objective",
            title="Operations Reviewer",
        )
        job_id = self.insert_job(
            "objective-body",
            "Operations Reviewer",
            canonical_id=canonical_id,
            location="Brazil - Remote",
        )
        body = (
            "Minimum of 3 years of operations experience is required.\n\n"
            "A bachelor's degree is required.\n\n"
            "A master's degree is preferred but not required.\n\n"
            "Hours: 20-30 hours per week.\n\n"
            "Pay range: USD 25 to USD 35 per hour.\n\n"
            "Employment type: Contract. Workplace type: Remote."
        )
        self.persist_rich_source(job_id, body=body)

        semantic_input = load_semantic_input(self.conn, canonical_id)
        document = extract_deterministic_document(semantic_input)
        attributes = document["attributes"]

        self.assertEqual(
            attributes["requirements"]["education"]["minimum_level"],
            "bachelor",
        )
        self.assertEqual(
            attributes["requirements"]["education"]["preferred_levels"],
            ["master"],
        )
        self.assertEqual(attributes["requirements"]["years_experience_min"], 3)
        self.assertEqual(attributes["work_arrangement"]["hours_per_week_min"], 20)
        self.assertEqual(attributes["work_arrangement"]["hours_per_week_max"], 30)
        self.assertEqual(attributes["work_arrangement"]["engagement_type"], "contract")
        self.assertEqual(attributes["compensation"]["currency"], "USD")
        self.assertEqual(attributes["compensation"]["amount_min"], 25)
        self.assertEqual(attributes["compensation"]["amount_max"], 35)
        body_evidence = [
            item
            for item in document["field_evidence"]
            if item["field_path"]
            == "attributes.requirements.years_experience_min"
        ]
        self.assertEqual(len(body_evidence), 1)
        self.assertRegex(body_evidence[0]["evidence_block_id"], r"^E[0-9a-f]{16}$")
        self.assertTrue(
            any(
                item.startswith("source_content:")
                for item in body_evidence[0]["authority_refs"]
            )
        )
        self.assertEqual(
            body_evidence[0]["variant_refs"], semantic_variant_refs(semantic_input)
        )
        listing_evidence = next(
            item
            for item in document["field_evidence"]
            if item["field_path"]
            == "attributes.work_arrangement.eligible_countries"
        )
        self.assertTrue(
            any(
                item.startswith("semantic_source:")
                for item in listing_evidence["authority_refs"]
            )
        )
        self.assertFalse(
            any(
                item.startswith("source_content:")
                for item in listing_evidence["authority_refs"]
            )
        )

    def test_vnext_does_not_promote_conflicting_variant_compensation(self):
        canonical_id = self.insert_canonical(
            key="provider::variant-pay",
            title="Payroll Reviewer",
        )
        first_job = self.insert_job(
            "variant-pay-br",
            "Payroll Reviewer",
            canonical_id=canonical_id,
            location="Brazil",
        )
        second_job = self.insert_job(
            "variant-pay-ph",
            "Payroll Reviewer",
            canonical_id=canonical_id,
            location="Philippines",
        )
        common = (
            "Review payroll records and document discrepancies for the operations team. "
            + "Use accepted source evidence carefully. " * 12
        )
        self.persist_rich_source(first_job, body=common + " Pay rate: $5 per hour.")
        self.persist_rich_source(second_job, body=common + " Pay rate: $7 per hour.")

        semantic_input = load_semantic_input(self.conn, canonical_id)
        document = extract_deterministic_document(semantic_input)

        self.assertTrue(document["attributes"]["compensation"]["disclosed"])
        self.assertIsNone(document["attributes"]["compensation"]["amount_min"])
        self.assertIn(
            "attributes.compensation.amount_min", document["unknown_fields"]
        )
        scoped_amounts = {
            fact["value"]
            for fact in document["variant_facts"]
            if fact["field_path"] == "attributes.compensation.amount_min"
        }
        self.assertEqual(scoped_amounts, {5, 7})
        self.assertEqual(
            len(
                {
                    variant_ref
                    for fact in document["variant_facts"]
                    if fact["field_path"] == "attributes.compensation.amount_min"
                    for variant_ref in fact["variant_refs"]
                }
            ),
            2,
        )

    def test_vnext_llm_fact_scope_is_derived_from_evidence_variants(self):
        canonical_id = self.insert_canonical(
            key="provider::variant-semantic",
            title="Operations Reviewer",
        )
        first_job = self.insert_job(
            "variant-semantic-one",
            "Operations Reviewer",
            canonical_id=canonical_id,
            location="Brazil",
        )
        second_job = self.insert_job(
            "variant-semantic-two",
            "Operations Reviewer",
            canonical_id=canonical_id,
            location="Philippines",
        )
        shared = "Review payroll records and document discrepancies."
        filler = " Use accepted evidence and follow the published workflow." * 12
        self.persist_rich_source(first_job, body=shared + filler)
        self.persist_rich_source(second_job, body=shared + filler)
        semantic_input = load_semantic_input(self.conn, canonical_id)
        document = extract_deterministic_document(semantic_input)
        packet, blocks = llm_source_packet(semantic_input)
        shared_block = next(
            item
            for item in packet["evidence_blocks"]
            if shared in item["content"]
        )
        self.assertEqual(
            shared_block["variant_refs"], semantic_variant_refs(semantic_input)
        )
        payload = blank_llm_payload()
        payload["responsibilities"] = [
            {
                "value": "Review payroll records and document discrepancies",
                "evidence": [shared_block["evidence_block_id"]],
            }
        ]
        validate_llm_payload(payload, blocks)

        merged = merge_llm_payload(
            document,
            payload,
            blocks,
            all_variant_refs=semantic_variant_refs(semantic_input),
        )

        self.assertEqual(
            merged["attributes"]["content"]["responsibilities"],
            ["Review payroll records and document discrepancies"],
        )
        self.assertNotIn(
            "attributes.content.responsibilities", merged["unknown_fields"]
        )

    def test_vnext_known_empty_is_distinct_from_unknown(self):
        canonical_id = self.insert_canonical(
            key="provider::known-empty",
            title="General Reviewer",
        )
        job_id = self.insert_job(
            "known-empty-source",
            "General Reviewer",
            canonical_id=canonical_id,
        )
        body = (
            "No specific software skills are required for this opportunity. "
            + "Candidates will receive clear task instructions and examples. " * 12
        )
        self.persist_rich_source(job_id, body=body)
        semantic_input = load_semantic_input(self.conn, canonical_id)
        document = extract_deterministic_document(semantic_input)
        packet, blocks = llm_source_packet(semantic_input)
        block = next(
            item
            for item in packet["evidence_blocks"]
            if "No specific software skills" in item["content"]
        )
        payload = blank_llm_payload()
        payload["known_empty_fields"] = [
            {
                "field_path": "attributes.requirements.skills_required",
                "evidence": [block["evidence_block_id"]],
            }
        ]
        contradictory = copy.deepcopy(payload)
        contradictory["skills_required"] = [
            {
                "value": "Python",
                "evidence": [block["evidence_block_id"]],
            }
        ]
        with self.assertRaises(EnrichmentValidationError):
            validate_llm_payload(contradictory, blocks)
        validate_llm_payload(payload, blocks)

        merged = merge_llm_payload(
            document,
            payload,
            blocks,
            all_variant_refs=semantic_variant_refs(semantic_input),
        )

        self.assertEqual(merged["attributes"]["requirements"]["skills_required"], [])
        self.assertNotIn(
            "attributes.requirements.skills_required", merged["unknown_fields"]
        )
        known_empty = next(
            fact
            for fact in merged["variant_facts"]
            if fact["field_path"] == "attributes.requirements.skills_required"
        )
        self.assertEqual(known_empty["knowledge_state"], "known_empty")


if __name__ == "__main__":
    unittest.main()
