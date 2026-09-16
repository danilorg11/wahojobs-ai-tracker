from contextlib import closing
from datetime import timedelta
import os
import sqlite3
import unittest
from unittest import mock

from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from tests import test_authenticated_profile_matches as cases
from tests.test_typed_match_criteria import enrichment
from tests.test_profile_preference_model import with_preference_model
from wahojobs import authenticated_profile_matches as browser
from wahojobs.matching.recommendation_validity import database_commit_token
from wahojobs.profiles.preference_model import empty_profile_preferences_v1


class RecommendationReuseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SyntheticMatcherFixture()
        self.addCleanup(self.fixture.close)

    def first(self):
        response = self.fixture.get()
        self.assertEqual(response.status, 200)
        self.assertIn(b"Python Backend AI Coding Evaluator", response.body)
        return response, self.fixture.last_run()

    def test_unchanged_reuse_preserves_html_without_inventory_scan(self):
        original = browser.local_product.secrets.token_urlsafe
        # Existing HTML deliberately generates fresh action idempotency keys.
        # Hold only those keys fixed to compare the actual rendering bytes.
        with mock.patch.object(browser.local_product.secrets, "token_urlsafe",
                               side_effect=lambda n: "synthetic-action-key" if n == 24 else original(n)):
            first, run = self.first()
            self.assertIsNotNone(run.recommendation_context.get("_authenticated_reuse"))
            with mock.patch.object(browser.profile_preview, "query_preview_rows",
                                   side_effect=AssertionError("reuse scanned inventory")):
                second = self.fixture.get("/find-matches?run=" + run.match_run_id)
        self.assertEqual(second.status, 200)
        self.assertEqual(first.body, second.body)

    def test_owner_change_cannot_read_old_run(self):
        _, run = self.first()
        self.fixture.owner = "b"
        response = self.fixture.get("/find-matches?run=" + run.match_run_id)
        self.assertEqual(response.status, 410)
        self.assertNotIn(b"Python Backend", response.body)

    def test_changed_preferences_recompute_old_url_and_keep_relaxations(self):
        # Retain an actual exclusion/relaxation after workload became guidance.
        # Engagement terms remain independent of workload and relationships.
        self.fixture.update_inventory("UPDATE jobs SET commitment='Temporary' WHERE id=7003")
        self.fixture.update_inventory("UPDATE jobs SET commitment='Internship' WHERE id=7006")
        model = empty_profile_preferences_v1()
        model['engagement_terms'] = ['temporary']
        self.fixture.profile = with_preference_model(self.fixture.profile, model)
        first, run = self.first()
        self.assertIn(b"More opportunities if", first.body)
        self.assertEqual([m['job_id'] for m in browser._primary_presentation_matches(run.recommendation_context)], [7003])
        model['engagement_terms'] = ['internship']
        self.fixture.profile = with_preference_model(self.fixture.profile, model)
        second = self.fixture.get("/find-matches?run=" + run.match_run_id)
        self.assertEqual(second.status, 200)
        current = self.fixture.last_run().recommendation_context
        self.assertEqual([m["job_id"] for m in browser._primary_presentation_matches(current)], [7006])
        self.assertIn(b"More opportunities if", second.body)
        self.assertTrue(all(s['criterion_id'] == 'preferences.engagement_terms'
            for s in current['_typed_preference_enforcement']['single_criterion_relaxations']['scenarios']))
        self.assertNotEqual(first.body, second.body)

    def test_changed_profile_recomputes_without_database_change(self):
        _, run = self.first()
        self.fixture.profile["location"]["country"] = "Canada"
        response = self.fixture.get("/find-matches?run=" + run.match_run_id)
        self.assertEqual(response.status, 200)
        self.assertNotIn(b"class='match-card'", response.body)

    def test_inventory_closure_invalidates_even_when_mtime_is_restored(self):
        _, run = self.first()
        before = self.fixture.path.stat()
        self.fixture.update_inventory("UPDATE jobs SET is_active=0")
        os.utime(self.fixture.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        response = self.fixture.get("/find-matches?run=" + run.match_run_id)
        self.assertEqual(response.status, 200)
        self.assertNotIn(b"class='match-card'", response.body)

    def test_inventory_content_change_invalidates(self):
        model = empty_profile_preferences_v1()
        model['employment_relationships'] = ['employee']
        self.fixture.profile = with_preference_model(self.fixture.profile, model)
        _, run = self.first()
        self.fixture.update_inventory("UPDATE jobs SET commitment='Freelance'")
        response = self.fixture.get("/find-matches?run=" + run.match_run_id)
        self.assertEqual(response.status, 200)
        self.assertEqual(browser._primary_presentation_matches(self.fixture.last_run().recommendation_context), [])
        current = self.fixture.last_run().recommendation_context
        self.assertTrue(current['_typed_preference_enforcement']['evaluations'])
        self.assertTrue(all(row['admission']['status'] == 'exclude'
            for row in current['_typed_preference_enforcement']['evaluations']))

    def test_clock_expiry_is_recomputed_and_never_claimed_current(self):
        _, run = self.first()
        self.fixture.advance(73)
        response = self.fixture.get("/find-matches?run=" + run.match_run_id)
        self.assertEqual(response.status, 200)
        self.assertIn(b"Availability is not recently verified", response.body)
        self.assertNotIn(b"good fits right now", response.body)
        self.fixture.advance(96)
        response = self.fixture.get("/find-matches?run=" + run.match_run_id)
        self.assertEqual(response.status, 200)
        self.assertNotIn(b"class='match-card'", response.body)

    def test_clock_rollback_and_config_change_force_recomputation(self):
        _, run = self.first()
        self.fixture.now -= timedelta(seconds=1)
        with mock.patch.object(browser.profile_preview, "query_preview_rows",
                               wraps=browser.profile_preview.query_preview_rows) as query:
            self.assertEqual(self.fixture.get("/find-matches?run=" + run.match_run_id).status, 200)
            query.assert_called_once()
        run = self.fixture.last_run()
        self.fixture.integration._metadata_overlay.records_by_key["irrelevant"] = {}
        with mock.patch.object(browser.profile_preview, "query_preview_rows",
                               wraps=browser.profile_preview.query_preview_rows) as query:
            self.assertEqual(self.fixture.get("/find-matches?run=" + run.match_run_id).status, 200)
            query.assert_called_once()

    def test_unproved_and_wal_results_are_not_reused(self):
        _, run = self.first()
        with mock.patch.object(browser.AuthenticatedProfileMatchesBrowserIntegration,
                               "_inventory_commit_token", return_value=None), \
             mock.patch.object(browser.profile_preview, "query_preview_rows",
                               wraps=browser.profile_preview.query_preview_rows) as query:
            self.assertEqual(self.fixture.get("/find-matches?run=" + run.match_run_id).status, 200)
            query.assert_called_once()
        with closing(sqlite3.connect(self.fixture.path)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN")
            self.assertIsNone(database_commit_token(connection))
            connection.rollback()

    def test_legacy_unkeyed_context_recomputes(self):
        _, run = self.first()
        del run.recommendation_context["_authenticated_reuse"]
        with mock.patch.object(browser.profile_preview, "query_preview_rows",
                               wraps=browser.profile_preview.query_preview_rows) as query:
            self.assertEqual(self.fixture.get("/find-matches?run=" + run.match_run_id).status, 200)
            query.assert_called_once()

    def test_failed_enrichment_read_is_not_reused_after_recovery(self):
        with mock.patch.object(browser.AuthenticatedProfileMatchesBrowserIntegration,
                               "_load_shadow_enrichments", side_effect=sqlite3.OperationalError("synthetic")):
            _, run = self.first()
        self.assertNotIn("_authenticated_reuse", run.recommendation_context)
        with mock.patch.object(browser.profile_preview, "query_preview_rows",
                               wraps=browser.profile_preview.query_preview_rows) as query:
            self.assertEqual(self.fixture.get("/find-matches?run=" + run.match_run_id).status, 200)
            query.assert_called_once()

    def test_commit_during_calculation_is_not_published_or_cached(self):
        original = browser.AuthenticatedProfileMatchesBrowserIntegration._load_shadow_enrichments
        def changed(integration, rows):
            result = original(integration, rows)
            self.fixture.update_inventory("UPDATE jobs SET is_active=0")
            return result
        with mock.patch.object(browser.AuthenticatedProfileMatchesBrowserIntegration,
                               "_load_shadow_enrichments", changed):
            self.assertEqual(self.fixture.get().status, 503)
        self.assertEqual(len(self.fixture.integration._registry), 0)

    def test_clock_expiry_during_calculation_is_not_published_or_cached(self):
        original = browser.AuthenticatedProfileMatchesBrowserIntegration._load_shadow_enrichments
        def expires(integration, rows):
            result = original(integration, rows)
            self.fixture.advance(73)
            return result
        with mock.patch.object(browser.AuthenticatedProfileMatchesBrowserIntegration,
                               "_load_shadow_enrichments", expires):
            self.assertEqual(self.fixture.get().status, 503)
        self.assertEqual(len(self.fixture.integration._registry), 0)
        response = self.fixture.get()
        self.assertEqual(response.status, 200)
        self.assertIn(b"Availability is not recently verified", response.body)
        self.assertEqual(response.body.count(b"Availability is not recently verified"), 2)
        self.assertIn(b"2 opportunities to review", response.body)
        self.assertEqual({m['job_id'] for m in browser._recommendation_presentation_matches(
            self.fixture.last_run().recommendation_context)}, {7003, 7006})

    def test_explicit_diagnostic_keeps_complete_inventory_and_runs_on_revisit(self):
        self.fixture.integration._criteria_shadow_sink = lambda _: None
        original = browser.AuthenticatedProfileMatchesBrowserIntegration._load_shadow_enrichments
        seen = []
        def load(integration, rows):
            seen.append({r["job_id"] for r in rows})
            return original(integration, rows)
        with mock.patch.object(browser.AuthenticatedProfileMatchesBrowserIntegration,
                               "_load_shadow_enrichments", load), \
             mock.patch.object(browser, "run_typed_match_criteria_shadow",
                               wraps=browser.run_typed_match_criteria_shadow) as diagnostic:
            _, run = self.first()
            self.assertEqual(self.fixture.get("/find-matches?run=" + run.match_run_id).status, 200)
            self.assertEqual(diagnostic.call_count, 2)
        self.assertEqual(seen, [{7003, 7006}, {7003, 7006}])


class DemandScopedEnrichmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cases.AuthenticatedProfileMatchesTests.setUpClass()

    def test_consumer_set_includes_tail_and_selected_variant_but_not_duplicates(self):
        context = cases.AuthenticatedProfileMatchesTests._presentation_context(range(1001, 1014))
        matches = next(iter(context["matches"].values()))
        matches[0]["job_id"] = None
        matches[0]["opportunity_trust"]["selected_variant_id"] = 1001
        rows = [cases.AuthenticatedProfileMatchesTests._row(job_id=n) for n in range(1001, 1015)]
        rows.append(dict(rows[1]))  # duplicate 1002 remains unresolved
        rows = [r for r in rows if r["job_id"] != 1003]  # missing reference
        selected = browser._typed_preference_candidate_rows(context, rows)
        self.assertEqual({r["job_id"] for r in selected}, {1001, *range(1004, 1014)})

    def test_scoped_and_full_evidence_have_exact_strict_soft_relaxation_parity(self):
        context = cases.AuthenticatedProfileMatchesTests._presentation_context([1201, 1202, 1203])
        rows = [cases.AuthenticatedProfileMatchesTests._row(job_id=n) for n in (1201, 1202, 1203, 9999)]
        full = {n: enrichment(amount_min=amount, amount_max=amount, amount_type="exact")
                for n, amount in ((1201, 22), (1202, 26), (1203, 30), (9999, 100))}
        for strictness in ("preferred", "strict"):
            model = empty_profile_preferences_v1()
            model["compensation"] = {"minimum_kind": strictness, "amount": "25",
                                     "currency": "USD", "period": "hour"}
            profile = with_preference_model(cases.AuthenticatedProfileMatchesTests.profile_v2, model)
            scoped = {r["canonical_opportunity_id"]: full[r["canonical_opportunity_id"]]
                      for r in browser._typed_preference_candidate_rows(context, rows)}
            expected = browser._apply_typed_preference_enforcement_v1(profile, context, rows, full)
            actual = browser._apply_typed_preference_enforcement_v1(profile, context, rows, scoped)
            self.assertEqual(expected, actual)
            self.assertEqual(browser._render_match_results(expected, inventory_count=4),
                             browser._render_match_results(actual, inventory_count=4))


if __name__ == "__main__":
    unittest.main()
