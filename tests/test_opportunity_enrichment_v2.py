import json
import tempfile
import unittest
from pathlib import Path

from wahojobs.canonical.service import sync_fallback_canonical_opportunities
from wahojobs.crawler.types import CompanyCrawlResult, JobCandidate, ProviderOutcome
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import install_base_schema
from wahojobs.opportunity_enrichment import (
    EnrichmentValidationError,
    enrich_canonical_opportunity,
    import_reviewed_overlay,
    resolve_effective_enrichment,
    save_override,
)
from wahojobs.opportunity_enrichment_schema import (
    OpportunityEnrichmentSchemaError,
    attest_opportunity_enrichment_schema_extension,
)
from wahojobs.reporting.market import get_market_size_summary
from wahojobs.tracking.service import track_crawl_result


NOW = "2026-08-16T12:00:00+00:00"


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

    def test_fresh_and_existing_schema_install_enrichment_tables(self):
        for table in (
            "opportunity_enrichments",
            "opportunity_enrichment_overrides",
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
            }.issubset(enrichment_columns)
        )

    def test_optional_closed_schema_extension_is_exact_or_rejected(self):
        cursor = self.conn.cursor()
        cursor.row_factory = None
        self.assertTrue(attest_opportunity_enrichment_schema_extension(cursor))
        self.conn.execute("DROP INDEX idx_opportunity_enrichments_status")
        with self.assertRaises(OpportunityEnrichmentSchemaError):
            attest_opportunity_enrichment_schema_extension(cursor)
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

    def test_fallback_does_not_change_live_market_counting_policy(self):
        self.insert_job("market-count", "Unlinked Appen role")
        before = get_market_size_summary(self.conn)["estimated_market_opportunities"]
        sync_fallback_canonical_opportunities(self.conn, self.company_id)
        after = get_market_size_summary(self.conn)["estimated_market_opportunities"]
        self.assertEqual(before, 1)
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
