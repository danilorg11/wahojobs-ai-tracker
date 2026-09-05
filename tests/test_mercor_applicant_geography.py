"""Captured positive regression plus explicitly synthetic geography edge cases."""

from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime
from email.message import Message
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from wahojobs import authenticated_profile_matches as browser
from wahojobs.crawler import pipeline
from wahojobs.crawler.providers import mercor
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import verify_job_source_acceptance_integrity
from wahojobs.matching.locations import location_eligibility
from wahojobs.matching.source_geography import MERCOR_GEOGRAPHY_FIELDS
from wahojobs.profiles.canonical import canonical_to_matcher_profile
from wahojobs.profiles.canonical_v2 import project_v2_to_matcher_v1


FIXTURE = json.loads((Path(__file__).parent / "fixtures/mercor_applicant_geography.json").read_text(encoding="utf-8"))


class MercorApplicantGeographyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SyntheticMatcherFixture()
        self.addCleanup(self.fixture.close)
        self.fixture.profile = deepcopy(FIXTURE["synthetic_profile"]["profile"])
        self.fixture.now = datetime.fromisoformat(FIXTURE["observed_at"])
        self.record = deepcopy(FIXTURE["record"])
        with closing(get_connection(self.fixture.path)) as conn, conn:
            conn.execute("""INSERT INTO companies
                (name, slug, careers_url, source_tier, inventory_model, market_count_policy)
                VALUES ('Mercor', 'mercor', ?, 'core', 'live_feed', 'count_live')""",
                (mercor.MERCOR_ENDPOINT,))
        self.addCleanup(patch.stopall)
        patch.object(mercor, "urlopen", side_effect=AssertionError("network forbidden")).start()

    def ingest(self, records):
        raw = json.dumps({"listings": records}).encode()
        class Response:
            headers = Message()
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return mercor.MERCOR_ENDPOINT
            def read(self): return raw
        @contextmanager
        def connection():
            with closing(get_connection(self.fixture.path)) as conn:
                yield conn
        with patch.object(pipeline, "get_connection", connection), \
                patch.object(pipeline, "utc_now", return_value=FIXTURE["observed_at"]), \
                patch.object(mercor, "urlopen", return_value=Response()), \
                patch("wahojobs.tracking.service.tracking_openai_client", return_value=None):
            return pipeline.run_crawl("mercor")[1]

    def render(self):
        response = self.fixture.get()
        self.assertEqual(response.status, 200)
        context = self.fixture.last_run().recommendation_context
        matches = [m for values in context["matches"].values() for m in values]
        return response, matches

    def projected_check(self, identity=None):
        rows, _ = self.fixture.integration._load_inventory()
        identity = identity or self.record["listingId"]
        row = next(r for r in rows if r["url"].endswith("/" + identity))
        profile = canonical_to_matcher_profile(project_v2_to_matcher_v1(
            self.fixture.profile, matcher_profile_id="synthetic-geography"))
        return location_eligibility(profile, row), row

    def synthetic_record(self, identity="synthetic-geography", **fields):
        record = {k: deepcopy(v) for k, v in self.record.items() if k not in MERCOR_GEOGRAPHY_FIELDS}
        return dict(record, listingId=identity, **fields)

    def test_captured_canadian_regression_uses_accepted_source_evidence(self):
        summary = self.ingest([self.record])
        self.assertFalse(summary.removals_authorized)
        response, matches = self.render()
        check, row = self.projected_check()
        self.assertEqual(check.status, "incompatible")
        self.assertIn("eligibleResidenceLocation", check.reason)
        self.assertIn("Canada", check.reason)
        self.assertIn("United States", check.reason)
        self.assertNotIn(self.record["title"].encode(), response.body)
        matched = next(m for m in matches if m["job_id"] == row["job_id"])
        self.assertIn("incompatible_location", matched["actionability_cap_reasons"])
        self.assertFalse(matched["primary_recommendation_eligible"])
        evidence = row["applicant_geography_evidence"]
        self.assertEqual(evidence["observed_at"], FIXTURE["observed_at"])
        self.assertEqual(evidence["fields"]["eligibleResidenceLocation"], ["USA"])
        with closing(get_connection(self.fixture.path)) as conn:
            verify_job_source_acceptance_integrity(conn, row["job_id"])

    def test_us_location_and_residence_pass_geography_only(self):
        self.fixture.profile["location"].update(country="United States", residence="United States")
        self.ingest([self.record])
        self.render()
        check, _ = self.projected_check()
        self.assertEqual(check.status, "eligible")
        # Deliberately no claim about healthcare leadership or overall job fit.

    def test_synthetic_alternatives_missing_ambiguous_and_exclusion_fields(self):
        cases = [
            ({"eligibleLocation": ["USA", "CAN"], "eligibleResidenceLocation": ["CAN", "GBR"]}, "eligible"),
            ({"eligibleLocation": ["USA"]}, "incompatible"),
            ({}, "unknown"),
            ({"eligibleLocation": None, "eligibleResidenceLocation": []}, "unknown"),
            ({"eligibleLocation": ["USA", "unresolved territory"]}, "unknown"),
            ({"eligibleLocation": ["CAN", "unresolved territory"]}, "eligible"),
            ({"eligibleLocation": "USA"}, "unknown"),
            ({"eligibleLocation": ["preferred: USA"]}, "unknown"),
            ({"ineligibleLocation": ["CAN"]}, "incompatible"),
            ({"ineligibleResidenceLocation": ["CAN"]}, "incompatible"),
            ({"ineligibleLocation": ["USA"]}, "unknown"),
            ({"eligibleLocation": ["CAN"], "ineligibleLocation": ["CAN"]}, "incompatible"),
        ]
        for n, (fields, expected) in enumerate(cases):
            with self.subTest(fields=fields):
                identity = f"synthetic-{n}"
                self.ingest([self.synthetic_record(identity, **fields)])
                response, matches = self.render()
                check, row = self.projected_check(identity)
                self.assertEqual(check.status, expected)
                if expected == "incompatible":
                    visible = browser._primary_presentation_matches(self.fixture.last_run().recommendation_context)
                    self.assertNotIn(row["job_id"], [m["job_id"] for m in visible])

    def test_remote_worldwide_cannot_override_explicit_applicant_restriction(self):
        self.ingest([self.synthetic_record(location="Remote Worldwide", eligibleLocation=["USA"])])
        self.render()
        self.assertEqual(self.projected_check("synthetic-geography")[0].status, "incompatible")

    def test_unknown_profile_and_residence_are_not_replaced_by_authorization(self):
        self.ingest([self.record])
        def location(**values):
            self.fixture.profile["location"].update(values)
            # Clearing synthetic facts also removes their synthetic provenance.
            self.fixture.profile["provenance"]["field_sources"] = [
                deepcopy(s) for s in FIXTURE["synthetic_profile"]["profile"]["provenance"]["field_sources"]
                if not (s["field_path"] in {"location.country", "location.residence"}
                        and not self.fixture.profile["location"][s["field_path"].split(".")[1]])]
        location(country="", residence="", work_authorization="Authorized in USA", eligible_countries=["United States"])
        self.render()
        check, _ = self.projected_check()
        self.assertEqual(check.status, "unknown")
        self.assertTrue(check.actionability_cap_required)
        location(country="Canada", residence="Canada")
        self.ingest([dict(self.record, eligibleLocation=["CAN"], eligibleResidenceLocation=["USA"])])
        self.render()
        check, _ = self.projected_check()
        self.assertEqual(check.status, "incompatible")
        self.assertIn("eligibleResidenceLocation", check.reason)
        self.assertNotIn("Source eligibleLocation:", check.reason)
        location(country="Canada", residence="")
        self.render()
        self.assertEqual(self.projected_check()[0].status, "unknown")

    def test_employer_preference_timezone_and_nationality_are_not_requirements(self):
        record = self.synthetic_record(preferredLocation=["USA"], employerHeadquarters="USA",
                                       timezone="America/New_York", nationality="USA", workAuthorization=["USA"])
        self.ingest([record])
        self.render()
        check, row = self.projected_check("synthetic-geography")
        self.assertEqual(check.status, "unknown")
        self.assertNotIn("applicant_country_requirements", row)

    def test_variant_selection_keeps_each_records_own_geography(self):
        self.ingest([self.synthetic_record("variant-us", eligibleLocation=["USA"]),
                     self.synthetic_record("variant-ca", eligibleLocation=["CAN"])])
        with closing(get_connection(self.fixture.path)) as conn, conn:
            canonical = conn.execute("SELECT canonical_opportunity_id FROM jobs WHERE external_id='variant-us'").fetchone()[0]
            conn.execute("UPDATE jobs SET canonical_opportunity_id=? WHERE external_id='variant-ca'", (canonical,))
        self.render()
        us, us_row = self.projected_check("variant-us")
        ca, ca_row = self.projected_check("variant-ca")
        self.assertEqual((us.status, ca.status), ("incompatible", "eligible"))
        visible = browser._primary_presentation_matches(self.fixture.last_run().recommendation_context)
        self.assertIn(ca_row["job_id"], [m["job_id"] for m in visible])
        self.assertNotIn(us_row["job_id"], [m["job_id"] for m in visible])

    def test_reprocessing_existing_record_invalidates_run_and_blocks_all_relaxations(self):
        unrestricted = {k: v for k, v in self.record.items() if k not in MERCOR_GEOGRAPHY_FIELDS}
        self.ingest([unrestricted])
        first, _ = self.render()
        self.assertIn(self.record["title"].encode(), first.body)
        old = self.fixture.last_run()
        with patch.object(browser.profile_preview, "query_preview_rows", side_effect=AssertionError("unsafe reuse")):
            self.assertEqual(self.fixture.get("/find-matches?run=" + old.match_run_id).status, 200)
        _, before_row = self.projected_check()
        self.ingest([self.record])  # Same source observation time; no freshness invention.
        current = self.fixture.get("/find-matches?run=" + old.match_run_id)
        self.assertEqual(current.status, 200)
        self.assertNotEqual(old.match_run_id, self.fixture.last_run().match_run_id)
        self.assertNotIn(self.record["title"].encode(), current.body)
        check, after_row = self.projected_check()
        self.assertEqual(before_row["job_id"], after_row["job_id"])
        self.assertEqual(before_row["latest_successful_source_run_at"], after_row["latest_successful_source_run_at"])
        self.assertEqual(check.status, "incompatible")
        # A part-time relaxation cannot bypass location, nor can recent-cache fallback.
        self.fixture.set_preferences("part_time")
        for hours in (0, 73):
            if hours: self.fixture.advance(hours)
            self.assertNotIn(self.record["title"].encode(), self.fixture.get("/find-matches?run=" + old.match_run_id).body)
            context = self.fixture.last_run().recommendation_context
            encoded = json.dumps(context.get("_typed_preference_enforcement", {}))
            self.assertNotIn(f'canonical:{after_row["canonical_opportunity_id"]}', encoded)

    def test_preexisting_workload_relaxation_cannot_resurrect_geographic_conflict(self):
        record = self.synthetic_record(commitment="Full-time", location="Remote - Canada")
        self.fixture.set_preferences("part_time")
        self.ingest([record])
        first, _ = self.render()
        old = self.fixture.last_run()
        self.assertIn(b"More opportunities if", first.body)
        self.assertIn(record["title"].encode(), first.body)
        context = old.recommendation_context
        self.assertFalse(any(m["source_slug"] == "mercor" for m in browser._primary_presentation_matches(context)))
        self.ingest([dict(record, eligibleLocation=["USA"], eligibleResidenceLocation=["USA"])])
        second = self.fixture.get("/find-matches?run=" + old.match_run_id)
        self.assertEqual(second.status, 200)
        self.assertNotIn(record["title"].encode(), second.body)


if __name__ == "__main__":
    unittest.main()
