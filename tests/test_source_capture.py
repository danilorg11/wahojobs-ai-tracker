import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from wahojobs.canonical.service import (
    refresh_canonical_rollups,
    sync_fallback_canonical_opportunities,
)
from wahojobs.crawler.types import (
    CompanyCrawlResult,
    JobCandidate,
    ProviderOutcome,
    crawl_run_status_for_result,
    evaluate_removal_authorization,
)
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import (
    create_crawl_run,
    ensure_opportunity_enrichment_schema,
    fail_crawl_run,
    get_job_source_capture_evidence,
    insert_job,
    install_base_schema,
    mark_missing_jobs_inactive,
    upsert_job_source_content,
)
from wahojobs.opportunity_enrichment import (
    load_semantic_input,
    semantic_input_sha256,
)
from wahojobs.opportunity_enrichment_schema import (
    OpportunityEnrichmentSchemaError,
    attest_opportunity_enrichment_schema_extension,
)
from wahojobs.source_capture import (
    CAPTURE_QUALITY_BLOCKED_OR_ERROR,
    CAPTURE_QUALITY_EMPTY,
    CAPTURE_QUALITY_HEALTHY_BODY,
    CAPTURE_QUALITY_METADATA_ONLY,
    EVIDENCE_STATE_ACCEPTED_CURRENT,
    EVIDENCE_STATE_DEGRADED_LATEST,
    EVIDENCE_STATE_LEGACY_ACCEPTED,
    EVIDENCE_STATE_MISSING,
    EVIDENCE_STATE_STALE_LAST_KNOWN_GOOD,
    EVIDENCE_REASON_PROMOTION_POLICY_CHANGED,
    PROMOTION_DECISION_CONFIRMED,
    PROMOTION_DECISION_HELD_DEGRADED,
    PROMOTION_DECISION_HELD_NON_AUTHORITATIVE,
    PROMOTION_DECISION_HELD_SOURCE_CONFLICT,
    PROMOTION_DECISION_PROMOTED,
    REASON_SOURCE_TIMESTAMP_CONFLICT,
    REASON_SOURCE_TIMESTAMP_INVALID,
    REASON_SOURCE_TIMESTAMP_MISSING,
    REASON_SOURCE_TIMESTAMP_REGRESSED,
    SEMANTIC_AUTHORITY_PENDING,
    SEMANTIC_AUTHORITY_VERSIONED_ACCEPTED,
    SOURCE_CAPTURE_CONTRACT_VERSION,
    SOURCE_PROMOTION_POLICY_VERSION,
    SourceCaptureContext,
    prepare_source_capture,
)
from wahojobs.tracking.normalize import with_source_hash
from wahojobs.tracking.service import track_crawl_result


NOW = "2026-08-29T12:00:00+00:00"
_DEFAULT = object()


class SafeSourceCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "source-capture.sqlite"
        self.conn = get_connection(self.database)
        install_base_schema(self.conn)
        self.company_id = self.conn.execute(
            """
            INSERT INTO companies (
              name, slug, careers_url,
              source_tier, inventory_model, market_count_policy
            ) VALUES (
              'Fixture', 'fixture', 'https://example.test/jobs',
              'core', 'live_feed', 'count_live'
            )
            """
        ).lastrowid
        self.job_id = self.insert_job("variant-one")
        self.second_job_id = self.insert_job("variant-two")

    def tearDown(self):
        self.conn.close()
        self.temporary.cleanup()

    def insert_job(self, external_id):
        candidate = with_source_hash(
            "fixture",
            JobCandidate(
                external_id=external_id,
                title=f"Role {external_id}",
                location="Remote",
                url=f"https://example.test/jobs/{external_id}",
                department="AI",
                expertise="Python",
            ),
        )
        return insert_job(self.conn, self.company_id, candidate, NOW)

    def candidate(
        self,
        job_id=None,
        *,
        body=_DEFAULT,
        metadata=_DEFAULT,
        source_updated_at="2026-08-29T12:00:00+00:00",
    ):
        job_id = job_id or self.job_id
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if body is _DEFAULT:
            body = (
                "Build reliable Python systems and evaluate technical work. "
                "Document findings with clear source evidence."
            )
        if metadata is _DEFAULT:
            metadata = {"skills": ["Python"], "workplace": "remote"}
        return JobCandidate(
            external_id=row["external_id"],
            title=row["title"],
            location=row["location"],
            url=row["url"],
            department=row["department"],
            expertise=row["expertise"],
            source_hash=row["source_hash"],
            source_body=body,
            source_body_format="text/plain" if body is not None else None,
            source_metadata=metadata,
            source_updated_at=source_updated_at,
        )

    def capture(
        self,
        candidate,
        *,
        job_id=None,
        now=NOW,
        outcome=ProviderOutcome.SUCCESS,
        snapshot_complete=True,
        pagination_complete=True,
        sample=False,
        normalized_record_count=1,
    ):
        job_id = job_id or self.job_id
        crawl_run_id = create_crawl_run(self.conn, self.company_id, now)
        crawl_result = CompanyCrawlResult(
            jobs=[candidate],
            used_sample_data=sample,
            source_message="fixture",
            source_type="fixture",
            outcome=outcome,
            snapshot_complete=snapshot_complete,
            pagination_complete=pagination_complete,
            raw_record_count=1,
            normalized_record_count=normalized_record_count,
            rejected_record_count=0,
            payload_shape="fixture:v1",
            schema_fingerprint="fixture-schema-v1",
        )
        result = upsert_job_source_content(
            self.conn,
            job_id,
            "fixture",
            "fixture",
            candidate,
            now,
            capture_context=SourceCaptureContext.from_crawl_result(
                crawl_run_id,
                crawl_result,
            ),
        )
        run_status = crawl_run_status_for_result(
            crawl_result,
            evaluate_removal_authorization(crawl_result),
        )
        self.conn.execute(
            """
            UPDATE crawl_runs
            SET status = ?, used_sample_data = ?, finished_at = ?
            WHERE id = ?
            """,
            (run_status, int(sample), now, crawl_run_id),
        )
        capture = self.conn.execute(
            """
            SELECT * FROM job_source_content_captures
            WHERE job_id = ? ORDER BY id DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        self.assertEqual(result.capture_id, capture["id"])
        self.assertEqual(result.promotion_decision, capture["promotion_decision"])
        return result.material_content_sha256, dict(capture)

    def accepted(self, job_id=None):
        return self.conn.execute(
            "SELECT * FROM job_source_contents WHERE job_id = ?",
            (job_id or self.job_id,),
        ).fetchone()

    def test_degraded_observation_chain_never_regresses_good_body(self):
        good_body = "Authoritative complete role body with Python requirements."
        self.capture(
            self.candidate(body=good_body),
            now="2026-08-29T12:00:00+00:00",
        )
        accepted_before = dict(self.accepted())

        cases = (
            (
                self.candidate(
                    body=None,
                    metadata=None,
                    source_updated_at="2026-08-30T12:00:00+00:00",
                ),
                {},
                CAPTURE_QUALITY_EMPTY,
                PROMOTION_DECISION_HELD_DEGRADED,
            ),
            (
                self.candidate(
                    body="Truncated partial body",
                    source_updated_at="2026-08-31T12:00:00+00:00",
                ),
                {
                    "outcome": ProviderOutcome.PARTIAL,
                    "snapshot_complete": False,
                    "pagination_complete": False,
                },
                CAPTURE_QUALITY_HEALTHY_BODY,
                PROMOTION_DECISION_HELD_NON_AUTHORITATIVE,
            ),
            (
                self.candidate(
                    body="Sample fixture body",
                    source_updated_at="2026-09-01T12:00:00+00:00",
                ),
                {"sample": True},
                CAPTURE_QUALITY_HEALTHY_BODY,
                PROMOTION_DECISION_HELD_NON_AUTHORITATIVE,
            ),
            (
                self.candidate(
                    body="Anomalous observation body",
                    source_updated_at="2026-09-02T12:00:00+00:00",
                ),
                {"outcome": ProviderOutcome.ANOMALOUS},
                CAPTURE_QUALITY_HEALTHY_BODY,
                PROMOTION_DECISION_HELD_NON_AUTHORITATIVE,
            ),
            (
                self.candidate(
                    body="<html><title>Access Denied</title>verify you are human</html>",
                    source_updated_at="2026-09-03T12:00:00+00:00",
                ),
                {},
                CAPTURE_QUALITY_BLOCKED_OR_ERROR,
                PROMOTION_DECISION_HELD_DEGRADED,
            ),
        )
        for offset, (candidate, options, quality, decision) in enumerate(cases, 1):
            with self.subTest(quality=quality, decision=decision):
                _, capture = self.capture(
                    candidate,
                    now=f"2026-09-{offset:02d}T13:00:00+00:00",
                    **options,
                )
                self.assertEqual(capture["capture_quality"], quality)
                self.assertEqual(capture["promotion_decision"], decision)
                self.assertEqual(dict(self.accepted()), accepted_before)

        captures = self.conn.execute(
            "SELECT * FROM job_source_content_captures WHERE job_id = ? ORDER BY id",
            (self.job_id,),
        ).fetchall()
        self.assertEqual(len(captures), 6)
        self.assertTrue(
            all(
                row["capture_contract_version"] == SOURCE_CAPTURE_CONTRACT_VERSION
                and row["promotion_policy_version"]
                == SOURCE_PROMOTION_POLICY_VERSION
                for row in captures
            )
        )
        self.assertEqual(
            get_job_source_capture_evidence(self.conn, self.job_id)["state"],
            EVIDENCE_STATE_STALE_LAST_KNOWN_GOOD,
        )

        _, reconfirmed = self.capture(
            self.candidate(
                body=good_body,
                source_updated_at="2026-09-04T12:00:00+00:00",
            ),
            now="2026-09-04T13:00:00+00:00",
        )
        self.assertEqual(
            reconfirmed["promotion_decision"],
            PROMOTION_DECISION_CONFIRMED,
        )
        self.assertEqual(self.accepted()["body"], good_body)
        self.assertEqual(
            get_job_source_capture_evidence(self.conn, self.job_id)["state"],
            EVIDENCE_STATE_ACCEPTED_CURRENT,
        )

        changed_body = "Authoritative changed body after degraded observations."
        _, changed = self.capture(
            self.candidate(
                body=changed_body,
                source_updated_at="2026-09-05T12:00:00+00:00",
            ),
            now="2026-09-05T13:00:00+00:00",
        )
        self.assertEqual(
            changed["promotion_decision"],
            PROMOTION_DECISION_PROMOTED,
        )
        self.assertEqual(self.accepted()["body"], changed_body)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM job_source_content_captures WHERE job_id = ?",
                (self.job_id,),
            ).fetchone()[0],
            8,
        )

    def test_missing_and_degraded_without_accepted_evidence_are_distinct(self):
        self.assertEqual(
            get_job_source_capture_evidence(self.conn, self.job_id)["state"],
            EVIDENCE_STATE_MISSING,
        )
        self.capture(self.candidate(body=None, metadata=None))
        self.assertIsNone(self.accepted())
        self.assertEqual(
            get_job_source_capture_evidence(self.conn, self.job_id)["state"],
            EVIDENCE_STATE_DEGRADED_LATEST,
        )

    def test_healthy_change_promotes_and_preserves_prior_capture(self):
        first_body = "Complete first source body."
        second_body = "Complete revised source body with new requirements."
        first_hash, first_capture = self.capture(
            self.candidate(body=first_body),
            now="2026-08-29T12:00:00+00:00",
        )
        second_hash, second_capture = self.capture(
            self.candidate(
                body=second_body,
                source_updated_at="2026-08-30T12:00:00+00:00",
            ),
            now="2026-08-30T12:00:00+00:00",
        )

        self.assertNotEqual(first_hash, second_hash)
        self.assertEqual(first_capture["promotion_decision"], PROMOTION_DECISION_PROMOTED)
        self.assertEqual(second_capture["promotion_decision"], PROMOTION_DECISION_PROMOTED)
        self.assertEqual(self.accepted()["body"], second_body)
        preserved = self.conn.execute(
            "SELECT body FROM job_source_content_captures WHERE id = ?",
            (first_capture["id"],),
        ).fetchone()
        self.assertEqual(preserved["body"], first_body)
        acceptance = self.conn.execute(
            "SELECT * FROM job_source_content_acceptances WHERE job_id = ?",
            (self.job_id,),
        ).fetchone()
        self.assertEqual(acceptance["accepted_capture_id"], second_capture["id"])
        self.assertEqual(
            get_job_source_capture_evidence(self.conn, self.job_id)["state"],
            EVIDENCE_STATE_ACCEPTED_CURRENT,
        )

    def test_identical_healthy_content_reconfirms_without_semantic_change(self):
        candidate = self.candidate()
        first_hash, first_capture = self.capture(candidate)
        first = dict(self.accepted())
        second_hash, second_capture = self.capture(
            self.candidate(source_updated_at="2026-08-30T12:00:00+00:00"),
            now="2026-08-30T12:00:00+00:00",
        )
        second = dict(self.accepted())

        self.assertEqual(first_hash, second_hash)
        self.assertEqual(first_capture["promotion_decision"], PROMOTION_DECISION_PROMOTED)
        self.assertEqual(second_capture["promotion_decision"], PROMOTION_DECISION_CONFIRMED)
        for field in (
            "provider",
            "source_type",
            "source_url",
            "external_id",
            "body",
            "body_format",
            "metadata_json",
            "material_content_sha256",
            "first_captured_at",
        ):
            self.assertEqual(second[field], first[field])
        self.assertEqual(second["last_captured_at"], "2026-08-30T12:00:00+00:00")
        acceptance = self.conn.execute(
            "SELECT * FROM job_source_content_acceptances WHERE job_id = ?",
            (self.job_id,),
        ).fetchone()
        self.assertEqual(acceptance["accepted_capture_id"], second_capture["id"])

    def test_confirmation_cannot_carry_changed_semantic_job_metadata(self):
        sync_fallback_canonical_opportunities(self.conn, self.company_id)
        canonical_id = self.conn.execute(
            "SELECT canonical_opportunity_id FROM jobs WHERE id = ?",
            (self.job_id,),
        ).fetchone()[0]
        body = "Stable accepted body whose semantic metadata later changes."
        _, first_capture = self.capture(self.candidate(body=body))
        before_hash = semantic_input_sha256(
            load_semantic_input(self.conn, canonical_id)
        )
        changed = replace(
            self.candidate(
                body=body,
                source_updated_at="2026-08-30T12:00:00+00:00",
            ),
            title="Accepted changed title",
            location="Brazil",
            department="Accepted changed department",
            expertise="Accepted changed expertise",
            commitment="Contract",
        )
        changed_result = CompanyCrawlResult(
            jobs=[changed],
            used_sample_data=False,
            source_message="complete semantic change",
            source_type="fixture",
            outcome=ProviderOutcome.SUCCESS,
            snapshot_complete=True,
            pagination_complete=True,
            raw_record_count=1,
            normalized_record_count=1,
            rejected_record_count=0,
            payload_shape="fixture:complete:v1",
            schema_fingerprint="fixture-complete-v1",
        )
        run_id = create_crawl_run(
            self.conn,
            self.company_id,
            "2026-08-30T12:00:00+00:00",
        )
        with patch(
            "wahojobs.tracking.service.tracking_openai_client",
            return_value=None,
        ):
            track_crawl_result(
                self.conn,
                self.company_id,
                run_id,
                changed_result,
                "2026-08-30T12:00:00+00:00",
            )

        promoted = self.conn.execute(
            """
            SELECT * FROM job_source_content_captures
            WHERE job_id = ? ORDER BY id DESC LIMIT 1
            """,
            (self.job_id,),
        ).fetchone()
        self.assertEqual(promoted["promotion_decision"], PROMOTION_DECISION_PROMOTED)
        self.assertEqual(
            promoted["material_content_sha256"],
            first_capture["material_content_sha256"],
        )
        self.assertNotEqual(
            promoted["semantic_material_sha256"],
            first_capture["semantic_material_sha256"],
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT title FROM jobs WHERE id = ?",
                (self.job_id,),
            ).fetchone()[0],
            changed.title,
        )
        promoted_hash = semantic_input_sha256(
            load_semantic_input(self.conn, canonical_id)
        )
        self.assertNotEqual(promoted_hash, before_hash)

        confirmed = replace(
            changed,
            source_updated_at="2026-08-31T12:00:00+00:00",
        )
        confirmed_result = replace(changed_result, jobs=[confirmed])
        confirmed_run_id = create_crawl_run(
            self.conn,
            self.company_id,
            "2026-08-31T12:00:00+00:00",
        )
        with patch(
            "wahojobs.tracking.service.tracking_openai_client",
            return_value=None,
        ):
            track_crawl_result(
                self.conn,
                self.company_id,
                confirmed_run_id,
                confirmed_result,
                "2026-08-31T12:00:00+00:00",
            )
        reconfirmed = self.conn.execute(
            """
            SELECT * FROM job_source_content_captures
            WHERE job_id = ? ORDER BY id DESC LIMIT 1
            """,
            (self.job_id,),
        ).fetchone()
        self.assertEqual(
            reconfirmed["promotion_decision"],
            PROMOTION_DECISION_CONFIRMED,
        )
        self.assertEqual(
            reconfirmed["semantic_material_sha256"],
            promoted["semantic_material_sha256"],
        )
        self.assertEqual(
            semantic_input_sha256(load_semantic_input(self.conn, canonical_id)),
            promoted_hash,
        )

    def test_future_promotion_policy_marks_prior_acceptance_as_lkg(self):
        self.capture(self.candidate())
        self.assertEqual(
            get_job_source_capture_evidence(self.conn, self.job_id)["state"],
            EVIDENCE_STATE_ACCEPTED_CURRENT,
        )

        with patch(
            "wahojobs.db.repository.SOURCE_PROMOTION_POLICY_VERSION",
            "job_source_promotion_v2",
        ):
            evidence = get_job_source_capture_evidence(self.conn, self.job_id)

        self.assertEqual(evidence["state"], EVIDENCE_STATE_STALE_LAST_KNOWN_GOOD)
        self.assertEqual(
            evidence["stale_reasons"],
            [EVIDENCE_REASON_PROMOTION_POLICY_CHANGED],
        )

    def test_acceptance_reader_rejects_a_held_capture_pointer(self):
        self.capture(self.candidate())
        _, held = self.capture(
            self.candidate(),
            outcome=ProviderOutcome.PARTIAL,
            snapshot_complete=False,
            pagination_complete=False,
            now="2026-08-30T12:00:00+00:00",
        )
        self.assertEqual(
            held["promotion_decision"],
            PROMOTION_DECISION_HELD_NON_AUTHORITATIVE,
        )
        self.conn.execute(
            """
            UPDATE job_source_content_acceptances
            SET accepted_capture_id = ?
            WHERE job_id = ?
            """,
            (held["id"], self.job_id),
        )

        with self.assertRaisesRegex(RuntimeError, "provenance is inconsistent"):
            get_job_source_capture_evidence(self.conn, self.job_id)

    def test_metadata_only_can_seed_but_cannot_replace_a_body(self):
        metadata = {"skills": ["Python"]}
        _, metadata_capture = self.capture(
            self.candidate(body=None, metadata=metadata)
        )
        self.assertEqual(metadata_capture["capture_quality"], CAPTURE_QUALITY_METADATA_ONLY)
        self.assertIsNone(self.accepted()["body"])

        body = "Full authoritative body after metadata-only discovery."
        _, body_capture = self.capture(
            self.candidate(
                body=body,
                metadata=metadata,
                source_updated_at="2026-08-29T12:00:00+00:00",
            ),
            now="2026-08-30T12:00:00+00:00",
        )
        self.assertEqual(body_capture["promotion_decision"], PROMOTION_DECISION_PROMOTED)
        self.assertEqual(self.accepted()["body"], body)

        _, thin_capture = self.capture(
            self.candidate(
                body=None,
                metadata={"skills": ["Python", "SQL"]},
                source_updated_at="2026-08-31T12:00:00+00:00",
            ),
            now="2026-08-31T12:00:00+00:00",
        )
        self.assertEqual(
            thin_capture["promotion_decision"],
            PROMOTION_DECISION_HELD_DEGRADED,
        )
        self.assertEqual(self.accepted()["body"], body)

    def test_timestamp_conflicts_hold_only_changed_material(self):
        original = "Current body with authoritative provider time."
        self.capture(self.candidate(body=original))
        conflicts = (
            ("not-a-time", REASON_SOURCE_TIMESTAMP_INVALID),
            (None, REASON_SOURCE_TIMESTAMP_MISSING),
            ("2026-08-28T12:00:00+00:00", REASON_SOURCE_TIMESTAMP_REGRESSED),
            ("2026-08-29T12:00:00+00:00", REASON_SOURCE_TIMESTAMP_CONFLICT),
        )
        for index, (source_updated_at, reason) in enumerate(conflicts, 1):
            with self.subTest(reason=reason):
                _, capture = self.capture(
                    self.candidate(
                        body=f"Changed body {index}",
                        source_updated_at=source_updated_at,
                    ),
                    now=f"2026-09-{index:02d}T12:00:00+00:00",
                )
                self.assertEqual(
                    capture["promotion_decision"],
                    PROMOTION_DECISION_HELD_SOURCE_CONFLICT,
                )
                self.assertEqual(json.loads(capture["decision_reasons_json"]), [reason])
                self.assertEqual(self.accepted()["body"], original)

        _, promoted = self.capture(
            self.candidate(
                body="Newer authoritative body",
                source_updated_at="2026-09-04T12:00:00+00:00",
            ),
            now="2026-09-04T12:00:00+00:00",
        )
        self.assertEqual(promoted["promotion_decision"], PROMOTION_DECISION_PROMOTED)
        self.assertEqual(self.accepted()["body"], "Newer authoritative body")

    def test_capture_and_acceptance_are_isolated_per_variant(self):
        first_hash, first_capture = self.capture(self.candidate())
        second_hash, second_capture = self.capture(
            self.candidate(self.second_job_id),
            job_id=self.second_job_id,
            now="2026-08-30T12:00:00+00:00",
        )
        self.conn.commit()

        self.assertEqual(first_hash, second_hash)
        self.assertNotEqual(first_capture["id"], second_capture["id"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                UPDATE job_source_content_acceptances
                SET accepted_capture_id = ?
                WHERE job_id = ?
                """,
                (second_capture["id"], self.job_id),
            )
        self.capture(
            self.candidate(
                body=None,
                metadata=None,
                source_updated_at="2026-08-31T12:00:00+00:00",
            ),
            now="2026-08-31T12:00:00+00:00",
        )
        self.assertEqual(
            get_job_source_capture_evidence(self.conn, self.job_id)["state"],
            EVIDENCE_STATE_STALE_LAST_KNOWN_GOOD,
        )
        self.assertEqual(
            get_job_source_capture_evidence(self.conn, self.second_job_id)["state"],
            EVIDENCE_STATE_ACCEPTED_CURRENT,
        )
        self.assertIsNotNone(self.accepted(self.second_job_id)["body"])

    def test_candidate_identity_mismatch_is_rejected_before_source_state_changes(self):
        self.capture(self.candidate(body="Variant one accepted body."))
        accepted_before = dict(self.accepted())
        captures_before = self.conn.execute(
            "SELECT COUNT(*) FROM job_source_content_captures WHERE job_id = ?",
            (self.job_id,),
        ).fetchone()[0]
        acceptance_before = dict(
            self.conn.execute(
                "SELECT * FROM job_source_content_acceptances WHERE job_id = ?",
                (self.job_id,),
            ).fetchone()
        )

        with self.assertRaisesRegex(ValueError, "identity does not match"):
            self.capture(
                self.candidate(
                    self.second_job_id,
                    body="Variant two evidence must never attach to variant one.",
                ),
                job_id=self.job_id,
                now="2026-08-30T12:00:00+00:00",
            )

        self.assertEqual(dict(self.accepted()), accepted_before)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM job_source_content_captures WHERE job_id = ?",
                (self.job_id,),
            ).fetchone()[0],
            captures_before,
        )
        self.assertEqual(
            dict(
                self.conn.execute(
                    "SELECT * FROM job_source_content_acceptances WHERE job_id = ?",
                    (self.job_id,),
                ).fetchone()
            ),
            acceptance_before,
        )

    def test_evidence_reader_recomputes_capture_and_materialized_content(self):
        candidate = self.candidate(body="Accepted authoritative body.")
        _, capture = self.capture(candidate)
        sync_fallback_canonical_opportunities(self.conn, self.company_id)
        canonical_id = self.conn.execute(
            "SELECT canonical_opportunity_id FROM jobs WHERE id = ?",
            (self.job_id,),
        ).fetchone()[0]
        self.assertEqual(
            get_job_source_capture_evidence(self.conn, self.job_id)["state"],
            EVIDENCE_STATE_ACCEPTED_CURRENT,
        )
        tampered = prepare_source_capture(
            replace(
                candidate,
                source_body="Internally consistent but unaccepted body.",
            )
        )
        mutations = (
            (
                "UPDATE job_source_contents SET body = ? WHERE job_id = ?",
                ("Silently divergent body.", self.job_id),
            ),
            (
                """
                UPDATE job_source_contents
                SET body = ?, body_format = ?, metadata_json = ?,
                    material_content_sha256 = ?
                WHERE job_id = ?
                """,
                (
                    tampered.body,
                    tampered.body_format,
                    tampered.metadata_json,
                    tampered.material_content_sha256,
                    self.job_id,
                ),
            ),
            (
                "UPDATE job_source_content_captures SET body = ? WHERE id = ?",
                ("Mutated accepted capture body.", capture["id"]),
            ),
            (
                "UPDATE job_source_contents SET external_id = ? WHERE job_id = ?",
                ("tampered-external-id", self.job_id),
            ),
            (
                "UPDATE job_source_contents SET source_url = ? WHERE job_id = ?",
                ("https://tampered.test/job", self.job_id),
            ),
            (
                """
                UPDATE job_source_contents
                SET provider = ?, source_type = ?
                WHERE job_id = ?
                """,
                ("tampered-provider", "tampered-source", self.job_id),
            ),
            (
                "UPDATE job_source_content_captures SET external_id = ? WHERE id = ?",
                ("tampered-capture-external-id", capture["id"]),
            ),
            (
                "UPDATE jobs SET title = ? WHERE id = ?",
                ("Tampered accepted semantic title", self.job_id),
            ),
        )
        for index, (statement, parameters) in enumerate(mutations):
            with self.subTest(mutation=index):
                self.conn.execute("SAVEPOINT source_consistency_probe")
                self.conn.execute(statement, parameters)
                with self.assertRaisesRegex(RuntimeError, "inconsistent"):
                    get_job_source_capture_evidence(self.conn, self.job_id)
                with self.assertRaisesRegex(RuntimeError, "inconsistent"):
                    load_semantic_input(self.conn, canonical_id)
                with self.assertRaisesRegex(RuntimeError, "inconsistent"):
                    self.capture(
                        self.candidate(
                            body="A later capture must not silently heal divergence.",
                            source_updated_at="2026-09-10T12:00:00+00:00",
                        ),
                        now="2026-09-10T12:00:00+00:00",
                    )
                self.conn.execute("ROLLBACK TO SAVEPOINT source_consistency_probe")
                self.conn.execute("RELEASE SAVEPOINT source_consistency_probe")
                self.assertEqual(
                    get_job_source_capture_evidence(self.conn, self.job_id)["state"],
                    EVIDENCE_STATE_ACCEPTED_CURRENT,
                )

    def test_accepted_promotion_provenance_mutations_fail_closed(self):
        self.capture(self.candidate(body="Accepted authoritative body."))
        sync_fallback_canonical_opportunities(self.conn, self.company_id)
        canonical_id = self.conn.execute(
            "SELECT canonical_opportunity_id FROM jobs WHERE id = ?",
            (self.job_id,),
        ).fetchone()[0]
        accepted_capture_id = self.conn.execute(
            """
            SELECT accepted_capture_id
            FROM job_source_content_acceptances
            WHERE job_id = ?
            """,
            (self.job_id,),
        ).fetchone()[0]
        other_company_id = self.conn.execute(
            """
            INSERT INTO companies (name, slug, careers_url)
            VALUES ('Other Fixture', 'other-fixture', 'https://other.test/jobs')
            """
        ).lastrowid
        other_run_id = create_crawl_run(
            self.conn,
            other_company_id,
            "2026-08-30T00:00:00+00:00",
        )
        mutations = (
            (
                """
                UPDATE job_source_content_captures
                SET provider_outcome = 'partial', snapshot_complete = 0
                WHERE id = ?
                """,
                (accepted_capture_id,),
            ),
            (
                """
                UPDATE job_source_content_captures
                SET promotion_decision = 'confirmed'
                WHERE id = ?
                """,
                (accepted_capture_id,),
            ),
            (
                """
                UPDATE job_source_contents
                SET source_updated_at = '2099-01-01T00:00:00+00:00'
                WHERE job_id = ?
                """,
                (self.job_id,),
            ),
            (
                """
                UPDATE job_source_content_captures
                SET source_updated_at = '2099-01-01T00:00:00+00:00'
                WHERE id = ?
                """,
                (accepted_capture_id,),
            ),
            (
                """
                UPDATE job_source_content_captures
                SET crawl_run_id = ?
                WHERE id = ?
                """,
                (other_run_id, accepted_capture_id),
            ),
        )
        for index, (statement, parameters) in enumerate(mutations):
            with self.subTest(mutation=index):
                self.conn.execute("SAVEPOINT promotion_provenance_probe")
                self.conn.execute(statement, parameters)
                with self.assertRaisesRegex(RuntimeError, "inconsistent"):
                    get_job_source_capture_evidence(self.conn, self.job_id)
                with self.assertRaisesRegex(RuntimeError, "inconsistent"):
                    load_semantic_input(self.conn, canonical_id)
                with self.assertRaisesRegex(RuntimeError, "inconsistent"):
                    self.capture(
                        self.candidate(
                            body="A later capture must not repair provenance.",
                            source_updated_at="2026-09-10T12:00:00+00:00",
                        ),
                        now="2026-09-10T12:00:00+00:00",
                    )
                self.conn.execute(
                    "ROLLBACK TO SAVEPOINT promotion_provenance_probe"
                )
                self.conn.execute("RELEASE SAVEPOINT promotion_provenance_probe")
                self.assertEqual(
                    get_job_source_capture_evidence(self.conn, self.job_id)["state"],
                    EVIDENCE_STATE_ACCEPTED_CURRENT,
                )

    def test_legacy_row_stays_truthful_and_schema_upgrade_does_not_backfill(self):
        prepared = prepare_source_capture(self.candidate())
        self.conn.execute(
            """
            INSERT INTO job_source_contents (
              job_id, provider, source_type, source_url, external_id,
              body, body_format, metadata_json, material_content_sha256,
              source_updated_at, first_captured_at, last_captured_at
            ) VALUES (?, 'fixture', 'legacy', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.job_id,
                self.candidate().url,
                self.candidate().external_id,
                prepared.body,
                prepared.body_format,
                prepared.metadata_json,
                prepared.material_content_sha256,
                prepared.source_updated_at,
                NOW,
                NOW,
            ),
        )
        legacy_before = dict(self.accepted())
        self.assertEqual(
            get_job_source_capture_evidence(self.conn, self.job_id)["state"],
            EVIDENCE_STATE_LEGACY_ACCEPTED,
        )

        self.conn.execute("DROP TABLE job_source_content_acceptances")
        self.conn.execute("DROP TABLE job_source_content_captures")
        self.conn.execute(
            "ALTER TABLE jobs DROP COLUMN semantic_authority_state"
        )
        cursor = self.conn.cursor()
        cursor.row_factory = None
        self.assertTrue(attest_opportunity_enrichment_schema_extension(cursor))
        ensure_opportunity_enrichment_schema(self.conn)
        ensure_opportunity_enrichment_schema(self.conn)
        self.assertTrue(attest_opportunity_enrichment_schema_extension(cursor))
        cursor.close()

        self.assertEqual(dict(self.accepted()), legacy_before)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM job_source_content_captures"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM job_source_content_acceptances"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT semantic_authority_state FROM jobs WHERE id = ?",
                (self.job_id,),
            ).fetchone()[0],
            "legacy_accepted",
        )
        capture_columns = {
            row["name"]
            for row in self.conn.execute(
                "PRAGMA table_info(job_source_content_captures)"
            ).fetchall()
        }
        self.assertTrue(
            {"semantic_job_fields_json", "semantic_material_sha256"}.issubset(
                capture_columns
            )
        )
        self.assertEqual(
            self.conn.execute("PRAGMA integrity_check").fetchone()[0],
            "ok",
        )
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

        self.capture(
            self.candidate(
                body=None,
                metadata=None,
                source_updated_at="2026-08-30T12:00:00+00:00",
            ),
            now="2026-08-30T12:00:00+00:00",
        )
        self.assertEqual(dict(self.accepted()), legacy_before)
        self.assertEqual(
            get_job_source_capture_evidence(self.conn, self.job_id)["state"],
            EVIDENCE_STATE_STALE_LAST_KNOWN_GOOD,
        )

    def test_capture_extension_attests_the_exact_jobs_authority_column(self):
        cursor = self.conn.cursor()
        cursor.row_factory = None
        self.assertTrue(attest_opportunity_enrichment_schema_extension(cursor))
        self.conn.execute(
            "ALTER TABLE jobs DROP COLUMN semantic_authority_state"
        )
        with self.assertRaises(OpportunityEnrichmentSchemaError):
            attest_opportunity_enrichment_schema_extension(cursor)
        cursor.close()

    def test_identical_legacy_material_establishes_explicit_promotion(self):
        candidate = self.candidate()
        prepared = prepare_source_capture(candidate)
        self.conn.execute(
            """
            INSERT INTO job_source_contents (
              job_id, provider, source_type, source_url, external_id,
              body, body_format, metadata_json, material_content_sha256,
              source_updated_at, first_captured_at, last_captured_at
            ) VALUES (?, 'fixture', 'fixture', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.job_id,
                candidate.url,
                candidate.external_id,
                prepared.body,
                prepared.body_format,
                prepared.metadata_json,
                prepared.material_content_sha256,
                prepared.source_updated_at,
                NOW,
                NOW,
            ),
        )

        _material_hash, capture = self.capture(candidate)

        self.assertEqual(
            capture["promotion_decision"],
            PROMOTION_DECISION_PROMOTED,
        )
        self.assertEqual(
            get_job_source_capture_evidence(self.conn, self.job_id)["state"],
            EVIDENCE_STATE_ACCEPTED_CURRENT,
        )

    def test_existing_jobs_upgrade_to_explicit_legacy_semantic_authority(self):
        self.conn.execute(
            "ALTER TABLE jobs DROP COLUMN semantic_authority_state"
        )

        install_base_schema(self.conn)
        install_base_schema(self.conn)

        self.assertEqual(
            [
                row["semantic_authority_state"]
                for row in self.conn.execute(
                    "SELECT semantic_authority_state FROM jobs ORDER BY id"
                )
            ],
            ["legacy_accepted", "legacy_accepted"],
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM job_source_content_captures"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM job_source_content_acceptances"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_unupgraded_legacy_semantic_input_remains_readable(self):
        sync_fallback_canonical_opportunities(self.conn, self.company_id)
        canonical_id = self.conn.execute(
            "SELECT canonical_opportunity_id FROM jobs WHERE id = ?",
            (self.job_id,),
        ).fetchone()[0]
        baseline_hash = semantic_input_sha256(
            load_semantic_input(self.conn, canonical_id)
        )
        self.conn.execute("DROP TABLE job_source_content_acceptances")
        self.conn.execute("DROP TABLE job_source_content_captures")
        self.conn.execute(
            "ALTER TABLE jobs DROP COLUMN semantic_authority_state"
        )

        self.assertEqual(
            semantic_input_sha256(load_semantic_input(self.conn, canonical_id)),
            baseline_hash,
        )

    def test_capture_and_promotion_roll_back_together(self):
        self.capture(self.candidate())
        self.conn.commit()
        accepted_before = dict(self.accepted())
        acceptance_before = dict(
            self.conn.execute(
                "SELECT * FROM job_source_content_acceptances WHERE job_id = ?",
                (self.job_id,),
            ).fetchone()
        )
        capture_count = self.conn.execute(
            "SELECT COUNT(*) FROM job_source_content_captures WHERE job_id = ?",
            (self.job_id,),
        ).fetchone()[0]
        job_before = dict(
            self.conn.execute(
                """
                SELECT external_id, title, location, department, expertise,
                       commitment, url, opportunity_kind, availability_basis,
                       include_in_live_market_estimate,
                       semantic_authority_state
                FROM jobs WHERE id = ?
                """,
                (self.job_id,),
            ).fetchone()
        )
        self.conn.execute(
            """
            CREATE TEMP TRIGGER reject_test_source_promotion
            BEFORE UPDATE ON job_source_contents
            BEGIN
              SELECT RAISE(ABORT, 'test promotion failure');
            END
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.capture(
                self.candidate(
                    body="Changed body that must roll back.",
                    source_updated_at="2026-08-30T12:00:00+00:00",
                ),
                now="2026-08-30T12:00:00+00:00",
            )

        self.assertEqual(dict(self.accepted()), accepted_before)
        self.assertEqual(
            dict(
                self.conn.execute(
                    "SELECT * FROM job_source_content_acceptances WHERE job_id = ?",
                    (self.job_id,),
                ).fetchone()
            ),
            acceptance_before,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM job_source_content_captures WHERE job_id = ?",
                (self.job_id,),
            ).fetchone()[0],
            capture_count,
        )
        self.assertEqual(
            dict(
                self.conn.execute(
                    """
                    SELECT external_id, title, location, department, expertise,
                           commitment, url, opportunity_kind, availability_basis,
                           include_in_live_market_estimate,
                           semantic_authority_state
                    FROM jobs WHERE id = ?
                    """,
                    (self.job_id,),
                ).fetchone()
            ),
            job_before,
        )
        self.conn.execute("DROP TRIGGER reject_test_source_promotion")
        self.conn.execute(
            """
            CREATE TEMP TRIGGER reject_test_acceptance_update
            BEFORE UPDATE ON job_source_content_acceptances
            BEGIN
              SELECT RAISE(ABORT, 'test acceptance failure');
            END
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.capture(
                replace(
                    self.candidate(
                        body="A second changed body that must roll back.",
                        source_updated_at="2026-08-30T12:00:00+00:00",
                    ),
                    title="Semantic title that must roll back",
                    location="Semantic location that must roll back",
                ),
                now="2026-08-30T12:00:00+00:00",
            )

        self.assertEqual(dict(self.accepted()), accepted_before)
        self.assertEqual(
            dict(
                self.conn.execute(
                    "SELECT * FROM job_source_content_acceptances WHERE job_id = ?",
                    (self.job_id,),
                ).fetchone()
            ),
            acceptance_before,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM job_source_content_captures WHERE job_id = ?",
                (self.job_id,),
            ).fetchone()[0],
            capture_count,
        )
        self.assertEqual(
            dict(
                self.conn.execute(
                    """
                    SELECT external_id, title, location, department, expertise,
                           commitment, url, opportunity_kind, availability_basis,
                           include_in_live_market_estimate,
                           semantic_authority_state
                    FROM jobs WHERE id = ?
                    """,
                    (self.job_id,),
                ).fetchone()
            ),
            job_before,
        )
        self.conn.execute("DROP TRIGGER reject_test_acceptance_update")

        retry_body = "Authoritative retry after both injected failures."
        _, retry_capture = self.capture(
            replace(
                self.candidate(
                    body=retry_body,
                    source_updated_at="2026-08-31T12:00:00+00:00",
                ),
                title="Accepted semantic retry title",
                location="Accepted semantic retry location",
            ),
            now="2026-08-31T12:00:00+00:00",
        )
        acceptance = self.conn.execute(
            "SELECT * FROM job_source_content_acceptances WHERE job_id = ?",
            (self.job_id,),
        ).fetchone()
        self.assertEqual(acceptance["accepted_capture_id"], retry_capture["id"])
        self.assertEqual(self.accepted()["body"], retry_body)
        self.assertEqual(
            self.accepted()["material_content_sha256"],
            retry_capture["material_content_sha256"],
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT title FROM jobs WHERE id = ?",
                (self.job_id,),
            ).fetchone()[0],
            "Accepted semantic retry title",
        )
        self.assertEqual(
            get_job_source_capture_evidence(self.conn, self.job_id)[
                "accepted_semantic_material_sha256"
            ],
            retry_capture["semantic_material_sha256"],
        )
        self.assertEqual(
            get_job_source_capture_evidence(self.conn, self.job_id)["state"],
            EVIDENCE_STATE_ACCEPTED_CURRENT,
        )

    def test_held_capture_does_not_change_semantic_input_or_enrichment_rows(self):
        sync_fallback_canonical_opportunities(self.conn, self.company_id)
        canonical_id = self.conn.execute(
            "SELECT canonical_opportunity_id FROM jobs WHERE id = ?",
            (self.job_id,),
        ).fetchone()[0]
        self.capture(self.candidate())
        baseline_hash = semantic_input_sha256(
            load_semantic_input(self.conn, canonical_id)
        )
        self.capture(
            self.candidate(
                body="Access Denied",
                source_updated_at="2026-08-30T12:00:00+00:00",
            ),
            now="2026-08-30T12:00:00+00:00",
        )
        held_hash = semantic_input_sha256(load_semantic_input(self.conn, canonical_id))
        self.assertEqual(held_hash, baseline_hash)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM opportunity_enrichments").fetchone()[0],
            0,
        )

        self.capture(
            self.candidate(
                body="Authoritative changed content for a later derivation.",
                source_updated_at="2026-08-31T12:00:00+00:00",
            ),
            now="2026-08-31T12:00:00+00:00",
        )
        changed_hash = semantic_input_sha256(
            load_semantic_input(self.conn, canonical_id)
        )
        self.assertNotEqual(changed_hash, baseline_hash)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM opportunity_enrichments").fetchone()[0],
            0,
        )

    def test_tracking_promotes_job_semantic_fields_only_for_accepted_capture(self):
        sync_fallback_canonical_opportunities(self.conn, self.company_id)
        canonical_id = self.conn.execute(
            "SELECT canonical_opportunity_id FROM jobs WHERE id = ?",
            (self.job_id,),
        ).fetchone()[0]
        good_body = "Complete authoritative source body for semantic authority."
        self.capture(self.candidate(body=good_body))
        semantic_before = semantic_input_sha256(
            load_semantic_input(self.conn, canonical_id)
        )
        job_before = dict(
            self.conn.execute(
                """
                SELECT title, location, department, expertise, commitment,
                       opportunity_kind, availability_basis,
                       include_in_live_market_estimate, last_seen_at
                FROM jobs WHERE id = ?
                """,
                (self.job_id,),
            ).fetchone()
        )

        degraded = replace(
            self.candidate(
                body="Truncated partial body.",
                source_updated_at="2026-08-30T12:00:00+00:00",
            ),
            title="Degraded replacement title",
            location="Unknown degraded location",
            department="Degraded department",
            expertise="Degraded expertise",
            commitment="Degraded commitment",
            opportunity_kind="evergreen_application",
            availability_basis="evergreen_page",
            include_in_live_market_estimate=False,
        )
        degraded_result = CompanyCrawlResult(
            jobs=[degraded],
            used_sample_data=False,
            source_message="partial fixture",
            source_type="fixture",
            outcome=ProviderOutcome.PARTIAL,
            snapshot_complete=False,
            pagination_complete=False,
            raw_record_count=1,
            normalized_record_count=1,
            rejected_record_count=0,
            payload_shape="fixture:partial:v1",
            schema_fingerprint="fixture-partial-v1",
        )
        degraded_run_id = create_crawl_run(
            self.conn,
            self.company_id,
            "2026-08-30T12:00:00+00:00",
        )
        with patch(
            "wahojobs.tracking.service.tracking_openai_client",
            return_value=None,
        ):
            track_crawl_result(
                self.conn,
                self.company_id,
                degraded_run_id,
                degraded_result,
                "2026-08-30T12:00:00+00:00",
            )

        job_after_degraded = dict(
            self.conn.execute(
                """
                SELECT title, location, department, expertise, commitment,
                       opportunity_kind, availability_basis,
                       include_in_live_market_estimate, last_seen_at
                FROM jobs WHERE id = ?
                """,
                (self.job_id,),
            ).fetchone()
        )
        for field in (
            "title",
            "location",
            "department",
            "expertise",
            "commitment",
            "opportunity_kind",
            "availability_basis",
            "include_in_live_market_estimate",
        ):
            self.assertEqual(job_after_degraded[field], job_before[field])
        self.assertEqual(
            job_after_degraded["last_seen_at"],
            "2026-08-30T12:00:00+00:00",
        )
        self.assertEqual(self.accepted()["body"], good_body)
        self.assertEqual(
            semantic_input_sha256(load_semantic_input(self.conn, canonical_id)),
            semantic_before,
        )
        latest = self.conn.execute(
            """
            SELECT promotion_decision
            FROM job_source_content_captures
            WHERE job_id = ? ORDER BY id DESC LIMIT 1
            """,
            (self.job_id,),
        ).fetchone()
        self.assertEqual(
            latest["promotion_decision"],
            PROMOTION_DECISION_HELD_NON_AUTHORITATIVE,
        )

        promoted_body = "New authoritative body with accepted requirements."
        promoted = replace(
            self.candidate(
                body=promoted_body,
                source_updated_at="2026-08-31T12:00:00+00:00",
            ),
            title="Accepted replacement title",
            location="Brazil",
            department="Accepted department",
            expertise="Accepted expertise",
            commitment="Contract",
        )
        other_variant = self.candidate(
            self.second_job_id,
            source_updated_at="2026-08-31T12:00:00+00:00",
        )
        promoted_result = CompanyCrawlResult(
            jobs=[promoted, other_variant],
            used_sample_data=False,
            source_message="complete fixture",
            source_type="fixture",
            outcome=ProviderOutcome.SUCCESS,
            snapshot_complete=True,
            pagination_complete=True,
            raw_record_count=2,
            normalized_record_count=2,
            rejected_record_count=0,
            payload_shape="fixture:complete:v1",
            schema_fingerprint="fixture-complete-v1",
        )
        promoted_run_id = create_crawl_run(
            self.conn,
            self.company_id,
            "2026-08-31T12:00:00+00:00",
        )
        with patch(
            "wahojobs.tracking.service.tracking_openai_client",
            return_value=None,
        ):
            track_crawl_result(
                self.conn,
                self.company_id,
                promoted_run_id,
                promoted_result,
                "2026-08-31T12:00:00+00:00",
            )

        job_after_promotion = self.conn.execute(
            "SELECT title, location, department, expertise, commitment FROM jobs WHERE id = ?",
            (self.job_id,),
        ).fetchone()
        self.assertEqual(job_after_promotion["title"], promoted.title)
        self.assertEqual(job_after_promotion["location"], promoted.location)
        self.assertEqual(job_after_promotion["department"], promoted.department)
        self.assertEqual(job_after_promotion["expertise"], promoted.expertise)
        self.assertEqual(job_after_promotion["commitment"], promoted.commitment)
        self.assertEqual(self.accepted()["body"], promoted_body)
        self.assertNotEqual(
            semantic_input_sha256(load_semantic_input(self.conn, canonical_id)),
            semantic_before,
        )

    def test_new_job_from_held_observation_stays_out_of_semantic_authority(self):
        pending = JobCandidate(
            external_id="pending-new",
            title="Untrusted partial title",
            location="Untrusted partial location",
            url="https://example.test/jobs/pending-new",
            department="Untrusted partial department",
            expertise="Untrusted partial expertise",
            source_body="Truncated partial body.",
            source_body_format="text/plain",
            source_updated_at="2026-08-30T12:00:00+00:00",
        )
        held_result = CompanyCrawlResult(
            jobs=[pending],
            used_sample_data=False,
            source_message="partial new-job observation",
            source_type="fixture",
            outcome=ProviderOutcome.PARTIAL,
            snapshot_complete=False,
            pagination_complete=False,
            raw_record_count=1,
            normalized_record_count=1,
            rejected_record_count=0,
            payload_shape="fixture:partial:v1",
            schema_fingerprint="fixture-partial-v1",
        )
        held_run_id = create_crawl_run(
            self.conn,
            self.company_id,
            "2026-08-30T12:00:00+00:00",
        )
        with patch(
            "wahojobs.tracking.service.tracking_openai_client",
            return_value=None,
        ):
            track_crawl_result(
                self.conn,
                self.company_id,
                held_run_id,
                held_result,
                "2026-08-30T12:00:00+00:00",
            )

        pending_job = self.conn.execute(
            "SELECT * FROM jobs WHERE external_id = 'pending-new'"
        ).fetchone()
        self.assertEqual(
            pending_job["semantic_authority_state"],
            SEMANTIC_AUTHORITY_PENDING,
        )
        self.assertIsNone(pending_job["canonical_opportunity_id"])
        pending_capture = self.conn.execute(
            """
            SELECT promotion_decision FROM job_source_content_captures
            WHERE job_id = ? ORDER BY id DESC LIMIT 1
            """,
            (pending_job["id"],),
        ).fetchone()
        self.assertEqual(
            pending_capture["promotion_decision"],
            PROMOTION_DECISION_HELD_NON_AUTHORITATIVE,
        )
        self.assertEqual(
            self.conn.execute(
                """
                SELECT COUNT(*) FROM opportunity_enrichments oe
                JOIN jobs j
                  ON j.canonical_opportunity_id = oe.canonical_opportunity_id
                WHERE j.id = ?
                """,
                (pending_job["id"],),
            ).fetchone()[0],
            0,
        )

        accepted = replace(
            pending,
            title="Accepted complete title",
            location="Brazil",
            department="Accepted department",
            expertise="Accepted expertise",
            source_body="Complete authoritative body for the new job.",
            source_updated_at="2026-08-31T12:00:00+00:00",
        )
        accepted_result = replace(
            held_result,
            jobs=[accepted],
            source_message="complete new-job observation",
            outcome=ProviderOutcome.SUCCESS,
            snapshot_complete=True,
            pagination_complete=True,
            payload_shape="fixture:complete:v1",
            schema_fingerprint="fixture-complete-v1",
        )
        accepted_run_id = create_crawl_run(
            self.conn,
            self.company_id,
            "2026-08-31T12:00:00+00:00",
        )
        with patch(
            "wahojobs.tracking.service.tracking_openai_client",
            return_value=None,
        ):
            track_crawl_result(
                self.conn,
                self.company_id,
                accepted_run_id,
                accepted_result,
                "2026-08-31T12:00:00+00:00",
            )
        promoted_job = self.conn.execute(
            "SELECT * FROM jobs WHERE id = ?",
            (pending_job["id"],),
        ).fetchone()
        self.assertEqual(
            promoted_job["semantic_authority_state"],
            SEMANTIC_AUTHORITY_VERSIONED_ACCEPTED,
        )
        self.assertEqual(promoted_job["title"], accepted.title)
        self.assertIsNotNone(promoted_job["canonical_opportunity_id"])
        self.assertIsNotNone(
            self.conn.execute(
                """
                SELECT status FROM opportunity_enrichments
                WHERE canonical_opportunity_id = ?
                """,
                (promoted_job["canonical_opportunity_id"],),
            ).fetchone()
        )

    def test_held_reactivation_uses_only_previously_accepted_variant_material(self):
        first_body = "First variant accepted source body."
        second_body = "Second variant accepted source body."
        self.capture(self.candidate(body=first_body))
        self.capture(
            self.candidate(self.second_job_id, body=second_body),
            job_id=self.second_job_id,
            now="2026-08-29T12:01:00+00:00",
        )
        sync_fallback_canonical_opportunities(self.conn, self.company_id)
        canonical_id = self.conn.execute(
            "SELECT canonical_opportunity_id FROM jobs WHERE id = ?",
            (self.job_id,),
        ).fetchone()[0]
        self.conn.execute(
            """
            UPDATE jobs
            SET canonical_opportunity_id = ?, is_active = 0, removed_at = ?
            WHERE id = ?
            """,
            (canonical_id, "2026-08-29T13:00:00+00:00", self.second_job_id),
        )
        refresh_canonical_rollups(self.conn, self.company_id)
        before_hash = semantic_input_sha256(
            load_semantic_input(self.conn, canonical_id)
        )
        accepted_job = dict(
            self.conn.execute(
                """
                SELECT title, location, department, expertise
                FROM jobs WHERE id = ?
                """,
                (self.second_job_id,),
            ).fetchone()
        )
        held = replace(
            self.candidate(
                self.second_job_id,
                body="Truncated reactivation body.",
                source_updated_at="2026-08-30T12:00:00+00:00",
            ),
            title="Untrusted reactivation title",
            location="Untrusted reactivation location",
            department="Untrusted reactivation department",
            expertise="Untrusted reactivation expertise",
        )
        held_result = CompanyCrawlResult(
            jobs=[held],
            used_sample_data=False,
            source_message="partial reactivation",
            source_type="fixture",
            outcome=ProviderOutcome.PARTIAL,
            snapshot_complete=False,
            pagination_complete=False,
            raw_record_count=1,
            normalized_record_count=1,
            rejected_record_count=0,
            payload_shape="fixture:partial:v1",
            schema_fingerprint="fixture-partial-v1",
        )
        run_id = create_crawl_run(
            self.conn,
            self.company_id,
            "2026-08-30T12:00:00+00:00",
        )
        with patch(
            "wahojobs.tracking.service.tracking_openai_client",
            return_value=None,
        ):
            track_crawl_result(
                self.conn,
                self.company_id,
                run_id,
                held_result,
                "2026-08-30T12:00:00+00:00",
            )

        stored = dict(
            self.conn.execute(
                """
                SELECT title, location, department, expertise, is_active,
                       semantic_authority_state
                FROM jobs WHERE id = ?
                """,
                (self.second_job_id,),
            ).fetchone()
        )
        for field in ("title", "location", "department", "expertise"):
            self.assertEqual(stored[field], accepted_job[field])
        self.assertEqual(stored["is_active"], 1)
        self.assertEqual(
            stored["semantic_authority_state"],
            SEMANTIC_AUTHORITY_VERSIONED_ACCEPTED,
        )
        semantic = load_semantic_input(self.conn, canonical_id)
        self.assertNotEqual(semantic_input_sha256(semantic), before_hash)
        self.assertIn(accepted_job["title"], semantic["source_fields"]["title"])
        self.assertNotIn(held.title, semantic["source_fields"]["title"])
        latest = self.conn.execute(
            """
            SELECT promotion_decision FROM job_source_content_captures
            WHERE job_id = ? ORDER BY id DESC LIMIT 1
            """,
            (self.second_job_id,),
        ).fetchone()
        self.assertEqual(
            latest["promotion_decision"],
            PROMOTION_DECISION_HELD_NON_AUTHORITATIVE,
        )

    def test_removal_and_failed_fetch_preserve_history_and_mark_lkg_stale(self):
        self.capture(self.candidate())
        removed = mark_missing_jobs_inactive(
            self.conn,
            self.company_id,
            [],
            "2026-08-30T12:00:00+00:00",
        )
        self.assertIn(self.job_id, removed)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM job_source_content_captures WHERE job_id = ?",
                (self.job_id,),
            ).fetchone()[0],
            1,
        )
        self.assertIsNotNone(self.accepted())

        failed_run_id = create_crawl_run(
            self.conn,
            self.company_id,
            "2026-08-31T12:00:00+00:00",
        )
        fail_crawl_run(
            self.conn,
            failed_run_id,
            "fixture fetch failure",
            "2026-08-31T12:01:00+00:00",
        )
        evidence = get_job_source_capture_evidence(self.conn, self.job_id)
        self.assertEqual(evidence["state"], EVIDENCE_STATE_STALE_LAST_KNOWN_GOOD)
        self.assertEqual(evidence["latest_crawl_run_status"], "failed")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM job_source_content_captures WHERE job_id = ?",
                (self.job_id,),
            ).fetchone()[0],
            1,
        )
        self.assertIsNotNone(self.accepted())

    def test_material_hash_keeps_the_existing_semantic_payload_contract(self):
        candidate = self.candidate()
        material_hash, _capture = self.capture(candidate)
        expected_payload = json.dumps(
            {
                "body": candidate.source_body,
                "body_format": candidate.source_body_format,
                "metadata": candidate.source_metadata,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        expected = hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()
        self.assertEqual(material_hash, expected)
        self.assertEqual(self.accepted()["material_content_sha256"], expected)


if __name__ == "__main__":
    unittest.main()
