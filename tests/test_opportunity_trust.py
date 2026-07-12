import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import scripts.local_product_app as app
import scripts.profile_match_digest as matcher
import scripts.profile_to_matches_preview as preview
from wahojobs.crawler.types import CompanyCrawlResult, JobCandidate
from wahojobs.matching.locations import location_eligibility
from wahojobs.matching.opportunity_trust import (
    INACTIVE,
    INCOMPATIBLE_LOCATION,
    LIVE_FEED_MAX_AGE_HOURS,
    NO_COMPATIBLE_LIVE_VARIANT,
    STALE_SOURCE,
    TRUSTED,
    UNVERIFIED_SOURCE,
    assess_opportunity_trust,
)
from wahojobs.tracking.service import track_crawl_result


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


def trust_row(**overrides):
    run_at = NOW - timedelta(hours=1)
    row = {
        "job_id": 1,
        "job_is_active": True,
        "canonical_is_active": True,
        "job_last_seen_at": run_at.isoformat(),
        "latest_successful_source_run_at": run_at.isoformat(),
        "source_run_started_at": run_at.isoformat(),
        "source_run_id": 10,
        "source_run_qualifies": True,
        "inventory_model": "live_feed",
        "market_count_policy": "count_live",
    }
    row.update(overrides)
    return row


def variant(job_id, location, trust_status, *, score=30, canonical_id=42):
    primary = trust_status == TRUSTED
    return {
        "job_id": job_id,
        "canonical_opportunity_id": canonical_id,
        "display_title": "English Language Specialist - AI Trainer",
        "source": "Meridial",
        "source_slug": "meridial",
        "url": f"https://example.test/jobs/{job_id}",
        "location": location,
        "score": score,
        "preview_section": "best_matches",
        "effective_product_section": "best_matches",
        "affirmative_fit_status": "supported",
        "primary_recommendation_eligible": primary,
        "primary_admission_reasons": [] if primary else [f"opportunity_trust_{trust_status}"],
        "location_eligibility_status": (
            "eligible" if trust_status == TRUSTED else "incompatible"
        ),
        "job_is_active": True,
        "canonical_is_active": True,
        "opportunity_trust_status": trust_status,
        "opportunity_trust_reasons": [],
        "opportunity_trust": {
            "status": trust_status,
            "reasons": [],
            "job_is_active": True,
            "canonical_is_active": True,
            "selected_variant_id": job_id if primary else None,
        },
    }


def matcher_profile(country="Brazil"):
    return {
        "signals": [("Language work", ["language"], 20)],
        "languages": ["English"],
        "skills": ["review"],
        "degrees_or_domains": ["language"],
        "work_preferences": ["remote"],
        "target_opportunity_types": ["AI evaluation"],
        "constraints": [],
        "avoid_keywords": [],
        "country": country,
        "location": country,
        "summary": "English reviewer seeking remote AI evaluation work.",
    }


def matcher_row(**overrides):
    values = {
        "job_id": 1,
        "title": "English Language Data Contributor",
        "canonical_title": "English Language Data Contributor",
        "location": "World Wide - Remote",
        "url": "https://example.test/jobs/1",
        "department": "Language",
        "expertise": "Language",
        "commitment": "Freelance",
        "source_category": "Language",
        "source": "Fixture",
        "source_slug": "fixture",
        "source_tier": "core",
        "inventory_model": "live_feed",
        "market_count_policy": "count_live",
        "opportunity_kind": "live_posting",
        "availability_basis": "api_feed",
        "include_in_live_market_estimate": 1,
        "canonical_opportunity_id": 42,
        "language": "English",
        "language_locale": None,
        "required_languages": None,
        **trust_row(),
    }
    values.update(overrides)
    return values


class OpportunityTrustAssessmentTests(unittest.TestCase):
    def test_active_recent_live_feed_is_trusted(self):
        result = assess_opportunity_trust(trust_row(), "eligible", now=NOW)
        self.assertEqual(result.status, TRUSTED)
        self.assertEqual(result.selected_variant_id, 1)
        self.assertEqual(result.freshness_max_age_hours, 72)

    def test_live_feed_freshness_boundary_is_inclusive(self):
        exactly = (NOW - timedelta(hours=LIVE_FEED_MAX_AGE_HOURS)).isoformat()
        beyond = (NOW - timedelta(hours=LIVE_FEED_MAX_AGE_HOURS, seconds=1)).isoformat()

        trusted = assess_opportunity_trust(
            trust_row(
                job_last_seen_at=exactly,
                latest_successful_source_run_at=exactly,
                source_run_started_at=exactly,
            ),
            "eligible",
            now=NOW,
        )
        stale = assess_opportunity_trust(
            trust_row(
                job_last_seen_at=beyond,
                latest_successful_source_run_at=beyond,
                source_run_started_at=beyond,
            ),
            "eligible",
            now=NOW,
        )

        self.assertEqual(trusted.status, TRUSTED)
        self.assertEqual(stale.status, STALE_SOURCE)

    def test_missing_failed_or_sample_qualifying_run_is_unverified(self):
        for source_run_id, qualifies in ((None, False), (20, False)):
            with self.subTest(source_run_id=source_run_id):
                result = assess_opportunity_trust(
                    trust_row(
                        source_run_id=source_run_id,
                        source_run_qualifies=qualifies,
                        latest_successful_source_run_at="",
                    ),
                    "eligible",
                    now=NOW,
                )
                self.assertEqual(result.status, UNVERIFIED_SOURCE)

    def test_inactive_job_or_canonical_is_rejected(self):
        self.assertEqual(
            assess_opportunity_trust(trust_row(job_is_active=False), "eligible", now=NOW).status,
            INACTIVE,
        )
        self.assertEqual(
            assess_opportunity_trust(
                trust_row(canonical_is_active=False), "eligible", now=NOW
            ).status,
            INACTIVE,
        )

    def test_explicit_location_incompatibility_is_rejected(self):
        result = assess_opportunity_trust(trust_row(), "incompatible", now=NOW)
        self.assertEqual(result.status, INCOMPATIBLE_LOCATION)

    def test_freshness_does_not_apply_to_evergreen_or_report_separately(self):
        for inventory_model, policy in (
            ("evergreen_application", "report_separately"),
            ("public_inventory", "report_separately"),
            ("mixed", "report_separately"),
        ):
            with self.subTest(inventory_model=inventory_model):
                result = assess_opportunity_trust(
                    trust_row(
                        inventory_model=inventory_model,
                        market_count_policy=policy,
                        source_run_id=None,
                        source_run_qualifies=False,
                        latest_successful_source_run_at="",
                        job_last_seen_at="",
                    ),
                    "not_applicable",
                    now=NOW,
                )
                self.assertEqual(result.status, TRUSTED)
                self.assertIsNone(result.freshness_max_age_hours)


class OpportunityTrustCanonicalSelectionTests(unittest.TestCase):
    def test_trust_admission_preserves_raw_score_label_and_sections(self):
        stale = (NOW - timedelta(days=22)).isoformat()
        row = matcher_row(
            job_last_seen_at=stale,
            latest_successful_source_run_at=stale,
            source_run_started_at=stale,
        )
        scored = matcher.score_opportunity(matcher_profile(), row)
        raw_signature = (
            scored["score"],
            scored["raw_product_section"],
            scored["effective_product_section"],
        )
        projected = preview.apply_preview_guardrails(
            matcher_profile(), row, scored, evaluated_at=NOW
        )

        self.assertEqual(
            (
                projected["score"],
                projected["raw_product_section"],
                projected["effective_product_section"],
            ),
            raw_signature,
        )
        self.assertEqual(projected["affirmative_fit_status"], "supported")
        self.assertEqual(projected["opportunity_trust_status"], STALE_SOURCE)
        self.assertFalse(projected["primary_recommendation_eligible"])

    def test_structured_incompatible_location_makes_fit_conflicting(self):
        row = matcher_row(location="India")
        scored = matcher.score_opportunity(matcher_profile(), row)
        projected = preview.apply_preview_guardrails(
            matcher_profile(), row, scored, evaluated_at=NOW
        )

        self.assertEqual(projected["location_eligibility_status"], "incompatible")
        self.assertEqual(projected["opportunity_trust_status"], INCOMPATIBLE_LOCATION)
        self.assertEqual(projected["affirmative_fit_status"], "conflicting")
        self.assertFalse(projected["primary_recommendation_eligible"])

    def test_brazil_or_worldwide_variant_wins_over_india(self):
        india = variant(1, "India", INCOMPATIBLE_LOCATION, score=40)
        brazil = variant(2, "Brazil", TRUSTED, score=30)
        selected = preview.dedupe_matches([india, brazil])[0]

        self.assertEqual(selected["job_id"], 2)
        self.assertEqual(selected["selected_variant_id"], 2)
        self.assertEqual(selected["url"], "https://example.test/jobs/2")
        self.assertTrue(selected["primary_recommendation_eligible"])

    def test_brazil_rejects_india_us_and_singapore_only_variants(self):
        variants = [
            variant(1, "India", INCOMPATIBLE_LOCATION),
            variant(2, "United States", INCOMPATIBLE_LOCATION),
            variant(3, "Singapore", INCOMPATIBLE_LOCATION),
        ]
        selected = preview.dedupe_matches(variants)[0]

        self.assertIsNone(selected["selected_variant_id"])
        self.assertFalse(selected["primary_recommendation_eligible"])
        self.assertEqual(selected["opportunity_trust_status"], NO_COMPATIBLE_LIVE_VARIANT)

    def test_remote_does_not_override_country_and_worldwide_remains_eligible(self):
        profile = {"country": "Brazil"}
        restricted = location_eligibility(profile, {"location": "Remote - India"})
        worldwide = location_eligibility(profile, {"location": "World Wide - Remote"})

        self.assertEqual(restricted.status, "incompatible")
        self.assertEqual(worldwide.status, "not_applicable")

    def test_untrusted_rows_do_not_pad_primary_list(self):
        stale = variant(1, "Worldwide", STALE_SOURCE)
        context = {"matches": {section: [] for section in preview.SECTION_ORDER}}
        context["matches"]["best_matches"] = [stale]
        self.assertEqual(app.build_ranked_presentation_matches(context), [])

    def test_stale_and_incompatible_rows_remain_in_demo_qa_without_positive_fit_copy(self):
        stale = variant(1, "Worldwide", STALE_SOURCE)
        stale["opportunity_trust"].update(
            {
                "job_last_seen_at": "2026-06-19T00:00:00+00:00",
                "latest_successful_source_run_at": "2026-06-19T00:00:00+00:00",
                "source_age_hours": 528,
                "inventory_model": "live_feed",
                "market_count_policy": "count_live",
                "freshness_max_age_hours": 72,
                "source_run_id": 10,
                "source_run_qualifies": True,
            }
        )
        context = {"matches": {section: [] for section in preview.SECTION_ORDER}}
        context["matches"]["best_matches"] = [stale]

        qa = app.render_opportunity_trust_qa(context)
        self.assertIn("English Language Specialist", qa)
        self.assertIn(STALE_SOURCE, qa)
        self.assertIn("528", qa)
        self.assertIn("needs current source verification", preview.user_fit_reason(stale))

    def test_stale_empty_state_differs_from_genuine_no_match(self):
        stale = variant(1, "Worldwide", STALE_SOURCE)
        context = {"matches": {section: [] for section in preview.SECTION_ORDER}}
        context["matches"]["best_matches"] = [stale]
        stale_page = app.render_ranked_preview_matches(context, {}, "run-stale")
        empty_page = app.render_ranked_preview_matches(
            {"matches": {section: [] for section in preview.SECTION_ORDER}},
            {},
            "run-empty",
        )

        self.assertIn("We're refreshing the latest opportunities", stale_page)
        self.assertNotIn("No clear matches surfaced", stale_page)
        self.assertIn("No clear matches surfaced", empty_page)


class OpportunityTrustSourceRunTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "trust.sqlite"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        schema = Path(__file__).resolve().parents[1] / "wahojobs" / "db" / "schema.sql"
        self.conn.executescript(schema.read_text(encoding="utf-8"))
        self.conn.execute(
            """
            INSERT INTO companies (
              id, name, slug, careers_url, source_tier, inventory_model, market_count_policy
            ) VALUES (1, 'Meridial', 'meridial', 'https://example.test', 'core', 'live_feed', 'count_live')
            """
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_failed_and_sample_runs_do_not_replace_qualifying_success(self):
        self.conn.execute(
            """
            INSERT INTO jobs (
              id, company_id, title, location, url, source_hash,
              first_seen_at, last_seen_at, is_active, updated_at
            ) VALUES (1, 1, 'English Reviewer', 'Worldwide', 'https://example.test/1',
                      'hash-1', ?, ?, 1, ?)
            """,
            (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
        old = NOW - timedelta(hours=80)
        self.conn.execute(
            "INSERT INTO crawl_runs (id, company_id, status, started_at, finished_at, used_sample_data) VALUES (10, 1, 'success', ?, ?, 0)",
            (old.isoformat(), old.isoformat()),
        )
        self.conn.execute(
            "INSERT INTO crawl_runs (id, company_id, status, started_at, finished_at, used_sample_data, error_message) VALUES (11, 1, 'failed', ?, ?, 0, 'provider failed')",
            (NOW.isoformat(), NOW.isoformat()),
        )
        self.conn.execute(
            "INSERT INTO crawl_runs (id, company_id, status, started_at, finished_at, used_sample_data) VALUES (12, 1, 'success', ?, ?, 1)",
            (NOW.isoformat(), NOW.isoformat()),
        )
        self.conn.commit()

        row = dict(matcher.get_active_rows(self.conn)[0])
        self.assertEqual(row["source_run_id"], 10)
        self.assertEqual(row["latest_successful_source_run_at"], old.isoformat())

    def test_closed_greenhouse_style_row_is_stale_then_inactivated_by_snapshot(self):
        stale_time = (NOW - timedelta(days=22)).isoformat()
        self.conn.execute(
            "INSERT INTO crawl_runs (id, company_id, status, started_at, finished_at, used_sample_data) VALUES (20, 1, 'success', ?, ?, 0)",
            (stale_time, stale_time),
        )
        candidate = JobCandidate(
            external_id="4778238101",
            title="English Language Data Contributor (Multimodal) - Freelance AI Trainer Project",
            location="World Wide - Remote",
            url="https://job-boards.eu.greenhouse.io/agency/jobs/4778238101",
            department="Language & Linguistics",
            expertise="Language & Linguistics",
        )
        track_crawl_result(
            self.conn,
            1,
            20,
            CompanyCrawlResult([candidate], False, "fixture", "fixture"),
            stale_time,
        )
        self.conn.commit()
        active = dict(matcher.get_active_rows(self.conn)[0])
        before = assess_opportunity_trust(active, "not_applicable", now=NOW)
        self.assertEqual(before.status, STALE_SOURCE)

        fresh_time = NOW.isoformat()
        self.conn.execute(
            "INSERT INTO crawl_runs (id, company_id, status, started_at, finished_at, used_sample_data) VALUES (21, 1, 'running', ?, NULL, 0)",
            (fresh_time,),
        )
        track_crawl_result(
            self.conn,
            1,
            21,
            CompanyCrawlResult([], False, "fixture", "fixture"),
            fresh_time,
        )
        self.conn.commit()

        job = self.conn.execute(
            "SELECT is_active, canonical_opportunity_id FROM jobs WHERE external_id = '4778238101'"
        ).fetchone()
        canonical = self.conn.execute(
            "SELECT is_active, variant_count FROM canonical_opportunities WHERE id = ?",
            (job["canonical_opportunity_id"],),
        ).fetchone()
        self.assertEqual(job["is_active"], 0)
        self.assertEqual(canonical["is_active"], 0)
        self.assertEqual(canonical["variant_count"], 0)
        self.assertEqual(matcher.get_active_rows(self.conn), [])


if __name__ == "__main__":
    unittest.main()
