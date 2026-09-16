"""Captured regional relationship and synthetic product navigation regressions."""
from contextlib import closing
from dataclasses import replace
from html import unescape
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unittest
from unittest.mock import patch

from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from tests.test_provider_detail_recovery import CASES, candidate, response
from wahojobs import authenticated_profile_matches as browser, public_job_page
from wahojobs.authenticated_variant_details import parse_variant_query, variant_detail_url
from wahojobs.crawler.provider_details import recover_detail, DetailResponse, DETAIL_KEY
from wahojobs.matching.locations import location_eligibility


class CapturedAlignerrRelationshipTests(unittest.TestCase):
    def test_shared_master_different_regional_postings_do_not_transfer_restrictions(self):
        root = Path(__file__).parent / 'fixtures/alignerr_variant_relationship'
        alternative = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
        raw = (root / alternative['file']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), alternative['sha256'])
        first = next(case for case in CASES if case['rank'] == 4)
        a = recover_detail('alignerr', candidate(first), response(first))
        b = recover_detail('alignerr', replace(candidate(first), external_id=alternative['external_id'],
                           url=alternative['url']), DetailResponse(alternative['url'], raw, alternative['observed_at']))
        ar, br = (c.source_metadata[DETAIL_KEY]['record'] for c in (a, b))
        self.assertEqual(ar['masterJobId'], br['masterJobId'])
        self.assertNotEqual(ar['leverPostId'], br['leverPostId'])
        self.assertNotEqual(ar['location'], br['location'])
        self.assertNotEqual(ar['longDescription'], br['longDescription'])
        self.assertEqual(location_eligibility({'country':'Portugal'}, a.__dict__).status, 'incompatible')
        self.assertEqual(location_eligibility({'country':'Portugal'}, b.__dict__).status, 'unknown')


class AuthenticatedVariantDetailsTests(unittest.TestCase):
    def setUp(self):
        self.f = SyntheticMatcherFixture()
        self.addCleanup(self.f.close)
        # Assert recommendation/eligibility proof directly, not internal UI copy.
        from wahojobs import authenticated_variant_details as variants
        prepare = variants.prepare_variant_notice
        def remember(job, *args, **kwargs):
            prepare(job, *args, **kwargs)
            self.detail_job = job
        capture = patch.object(variants, 'prepare_variant_notice', side_effect=remember)
        capture.start(); self.addCleanup(capture.stop)
        self.f.profile['location']['country'] = 'Portugal'
        self.f.update_inventory("UPDATE jobs SET location='Remote - United States' WHERE id=7003")
        self.f.update_inventory("UPDATE jobs SET canonical_opportunity_id=7002, location='Remote - Portugal', commitment='Full-time' WHERE id=7006")
        self.f.update_inventory("UPDATE canonical_opportunities SET variant_count=2 WHERE id=7002")

    def matches(self):
        r = self.f.get()
        self.assertEqual(r.status, 200)
        run = self.f.last_run()
        visible = browser._primary_presentation_matches(run.recommendation_context)
        return r, run, visible

    def card_link(self, r):
        return unescape(re.search(r"class='button match-primary-action' href='([^']+)'", r.body.decode()).group(1))

    def test_excluded_variant_eligible_alternative_card_detail_explanation_and_destination(self):
        r, run, visible = self.matches()
        self.assertEqual([m['job_id'] for m in visible], [7006])
        link = self.card_link(r)
        self.assertEqual(link, variant_detail_url(visible[0], run_id=run.match_run_id))
        with patch('wahojobs.crawler.provider_details.fetch_detail', side_effect=AssertionError('request-time fetch')):
            detail = self.f.get(link)
        self.assertEqual(detail.status, 200)
        body = detail.body.decode()
        self.assertIsNotNone(self.detail_job['_authenticated_recommendation'])
        self.assertEqual(self.detail_job['_authenticated_local_checks']['match']['location_eligibility_status'], 'eligible')
        self.assertEqual(self.detail_job['external_id'], 'synthetic-part-time')
        self.assertIn("href='https://jobs.example.test/synthetic-part-time'", body)
        self.assertNotIn("href='https://jobs.example.test/distinctive-bilingual-reviewer'", body)
        self.assertNotIn('existing comparison', body)
        self.assertIn(f"href='/find-matches?run={run.match_run_id}#opportunity-7006'", body)
        self.assertIn(b"id='opportunity-7006'", r.body)

    def test_unknown_alternative_is_not_worldwide_permission(self):
        self.f.update_inventory("UPDATE jobs SET location='Remote' WHERE id=7006")
        r, _, visible = self.matches()
        self.assertEqual(visible[0]['location_eligibility_status'], 'unknown')
        detail = self.f.get(self.card_link(r))
        self.assertEqual(detail.status, 200)
        self.assertIn('Eligibility from Portugal needs confirmation.', detail.body.decode())
        self.assertEqual(self.detail_job['_authenticated_local_checks']['match']['location_eligibility_status'], 'unknown')

    def test_normal_canonical_navigation_uses_current_representative(self):
        self.matches()
        r = self.f.get('/job/opportunity-7002')
        self.assertEqual(r.status, 200)
        self.assertEqual(self.detail_job['external_id'], 'synthetic-part-time')
        self.assertFalse(self.detail_job['_authenticated_membership_known'])
        self.assertTrue(self.detail_job['_authenticated_local_checks']['passes'])
        # An explicitly inspected excluded variant remains a source record,
        # with no claim that it is still a recommendation.
        old = self.f.get('/job/opportunity-7002?variant=7003')
        self.assertEqual(old.status, 200)
        self.assertFalse(self.detail_job['_authenticated_membership_known'])
        self.assertIn(b'restriction conflicts with your profile', old.body)
        self.assertNotIn(b'Apply on company site</a>', old.body)
        self.assertIn(b'View source listing</a>', old.body)

    def test_missing_cross_canonical_and_malformed_variant_references_never_fall_back(self):
        for path, code in [('/job/opportunity-7002?variant=999999',404),
                           ('/job/opportunity-7005?variant=7003',404),
                           ('/job/opportunity-7002?variant=0',400),
                           ('/job/opportunity-7002?variant=7006&variant=7003',400)]:
            with self.subTest(path=path):
                r = self.f.get(path)
                self.assertEqual(r.status, code)
                self.assertNotIn(b'Apply on company site', r.body)

    def test_old_run_rechecks_changed_location_closure_and_clock(self):
        r, run, _ = self.matches()
        link = self.card_link(r)
        self.f.update_inventory("UPDATE jobs SET location='Remote - United States' WHERE id=7006")
        stale = self.f.get(link)
        self.assertEqual(stale.status, 200)
        self.assertFalse(self.detail_job['_authenticated_membership_known'])
        self.assertIn(b'restriction conflicts with your profile', stale.body)
        self.assertNotIn(b'Apply on company site</a>', stale.body)
        self.assertIn(b"href='/find-matches#opportunity-7006'", stale.body)
        self.assertNotIn(f"href='/find-matches?run={run.match_run_id}#".encode(), stale.body)
        fresh = self.f.get('/find-matches?run=' + run.match_run_id)
        self.assertNotIn(b"class='match-card'", fresh.body)
        self.f.update_inventory("UPDATE jobs SET is_active=0 WHERE id=7006")
        closed = self.f.get(link)
        self.assertEqual(closed.status, 200)
        self.assertNotIn(b'Apply on company site</a>', closed.body)
        self.f.advance(193)
        self.assertNotIn(b'Apply on company site</a>', self.f.get(link).body)

    def test_owner_and_unauthenticated_isolation(self):
        r, _, _ = self.matches()
        link = self.card_link(r)
        self.f.owner = 'b'
        other = self.f.get(link)
        self.assertEqual(other.status, 404)
        self.assertNotIn(b'Source record:', other.body)
        with patch.object(browser.AuthenticatedProfileMatchesService, 'resolve',
                          return_value=browser.MatchesAuthorityResult('authentication_required')):
            anonymous = self.f.integration.handle('GET', '/job/opportunity-7002?variant=7006', (('Host','app.test'),))
        self.assertEqual(anonymous.status, 401)

    def test_same_proven_source_identity_keeps_same_application_destination(self):
        # Synthetic duplicate representations, not merely equal titles/master IDs.
        self.f.update_inventory("UPDATE jobs SET location='Remote - Portugal', external_id='same-source', url='https://jobs.example.test/same-source' WHERE canonical_opportunity_id=7002")
        r, _, visible = self.matches()
        self.assertEqual(len(visible), 1)
        detail = self.f.get(self.card_link(r))
        self.assertEqual(detail.status, 200)
        self.assertEqual(self.detail_job['external_id'], 'same-source')
        self.assertIn(b"href='https://jobs.example.test/same-source'", detail.body)

    def test_relaxation_link_keeps_exact_variant_and_is_not_a_main_match_claim(self):
        from tests.test_profile_preference_model import with_preference_model
        from wahojobs.profiles.preference_model import empty_profile_preferences_v1
        # Soft workload is no longer an exclusion. Keep the exact same genuine
        # relaxation/variant test using an unchanged relationship criterion.
        model = empty_profile_preferences_v1()
        model['employment_relationships'] = ['employee']
        self.f.profile = with_preference_model(self.f.profile, model)
        self.f.update_inventory("UPDATE jobs SET commitment='Freelance' WHERE id=7006")
        r, run, visible = self.matches()
        self.assertEqual(visible, [])
        self.assertIn(b'More opportunities if', r.body)
        link = unescape(re.search(r"href='([^']+)'>View job details", r.body.decode()).group(1))
        self.assertIn('variant=7006', link)
        detail = self.f.get(link)
        self.assertEqual(detail.status, 200)
        self.assertFalse(self.detail_job['_authenticated_membership_known'])
        self.assertFalse(self.detail_job['_authenticated_local_checks']['passes'])
        # Membership may only come from the original valid run, not a scoped list.
        proven = self.f.get(link + '&run=' + run.match_run_id)
        self.assertEqual(self.detail_job['_authenticated_recommendation']['_detail_recommendation_section'], 'relaxation')

    def test_public_navigation_is_unchanged_and_unknown_queries_are_rejected(self):
        with self.f.provider() as conn:
            original = public_job_page.load_public_job(conn, '/job/opportunity-7002', now=self.f.now)
            exact = public_job_page.load_public_job(conn, '/job/opportunity-7002', now=self.f.now, selected_job_id=7006)
        self.assertEqual(original['job_id'], 7003)
        self.assertEqual(exact['job_id'], 7006)
        self.assertIsNone(parse_variant_query('unknown=1'))

    def test_no_reuse_token_does_not_invent_membership_or_ineligibility(self):
        r, _, _ = self.matches()
        with patch('wahojobs.matching.recommendation_validity.database_commit_token', return_value=None):
            detail = self.f.get(self.card_link(r))
        self.assertEqual(detail.status, 200)
        self.assertEqual(self.detail_job['external_id'], 'synthetic-part-time')
        self.assertFalse(self.detail_job['_authenticated_membership_known'])
        self.assertTrue(self.detail_job['_authenticated_local_checks']['passes'])
        self.assertNotIn(b'not in your current recommendations', detail.body)

    def test_concurrent_inventory_commit_cannot_split_selection_from_details(self):
        original = public_job_page.load_public_job_evidence
        attempts = []
        def concurrent(*args, **kwargs):
            with closing(sqlite3.connect(self.f.path, timeout=0)) as writer:
                with self.assertRaises(sqlite3.OperationalError):
                    writer.execute("UPDATE jobs SET is_active=0")
                    writer.commit()
                writer.rollback()
                attempts.append(True)
            return original(*args, **kwargs)
        with patch.object(public_job_page, 'load_public_job_evidence', side_effect=concurrent):
            self.assertEqual(self.f.get('/job/opportunity-7002?variant=7006').status, 200)
        self.assertTrue(attempts)
        # Lock is released on exit; no persistent lock or background machinery.
        self.f.update_inventory("UPDATE jobs SET is_active=0")

    def test_detail_scores_only_the_requested_group_after_releasing_read_transaction(self):
        from contextlib import contextmanager
        calls, connections, queries = [], [], []
        original_provider = self.f.provider
        @contextmanager
        def traced():
            with original_provider() as conn:
                conn.set_trace_callback(queries.append)
                connections.append(conn)
                try:
                    yield conn
                finally:
                    connections.remove(conn)
        self.f.integration._connection_provider = traced
        score = browser.profile_preview.matcher.score_opportunity
        def scoped(profile, row):
            self.assertEqual(row['canonical_opportunity_id'], 7002)
            self.assertFalse(any(c.in_transaction for c in connections))
            calls.append(row['job_id'])
            return score(profile, row)
        for target in ('/job/opportunity-7002?variant=7006', '/job/opportunity-7002'):
            calls.clear(); queries.clear()
            with (patch.object(browser.AuthenticatedProfileMatchesBrowserIntegration, '_load_inventory', side_effect=AssertionError('full catalog')),
                  patch.object(browser.AuthenticatedProfileMatchesBrowserIntegration, '_render_persistent_matches', side_effect=AssertionError('full matching')),
                  patch.object(browser.profile_preview.matcher, 'score_opportunity', side_effect=scoped)):
                self.assertEqual(self.f.get(target).status, 200)
            self.assertCountEqual(calls, [7003, 7006])
            inventory_queries = [q for q in queries if 'j.id AS job_id' in q and 'NULL AS required_languages' in q]
            self.assertEqual(len(inventory_queries), 3)
            self.assertTrue(all('j.canonical_opportunity_id = 7002' in q for q in inventory_queries))

    def test_scoped_representative_equals_existing_group_logic(self):
        from wahojobs.authenticated_variant_details import load_scoped_snapshot, resolve_scoped_variant
        for alternative in ('Remote - Portugal', 'Remote', 'Remote - United States'):
            self.f.update_inventory('UPDATE jobs SET location=? WHERE id=7006', (alternative,))
            _, run, _ = self.matches()
            full = [m for values in run.recommendation_context['matches'].values() for m in values
                    if m.get('canonical_opportunity_id') == 7002]
            self.assertEqual(len(full), 1)
            with self.f.provider() as conn:
                conn.execute('BEGIN')
                snapshot = load_scoped_snapshot(conn, 7002, None, now=self.f.now)
                conn.rollback()
            job, _ = resolve_scoped_variant(snapshot, self.f.profile, self.f.integration._metadata_overlay, None, now=self.f.now)
            self.assertEqual(job['job_id'], full[0]['job_id'])

    def test_profile_preference_and_configuration_changes_invalidate_membership_only(self):
        from copy import deepcopy
        from wahojobs.matching.metadata_overlay import OpportunityMetadataOverlay
        from tests.test_profile_preference_model import with_preference_model
        r, _, _ = self.matches(); link = self.card_link(r)
        original = deepcopy(self.f.profile)
        self.f.profile['location']['country'] = 'United States'
        body = self.f.get(link).body
        self.assertIn(b'restriction conflicts with your profile', body)
        self.assertFalse(self.detail_job['_authenticated_membership_known'])
        self.f.profile = original
        self.f.set_preferences('part_time')
        body = self.f.get(link).body
        # Changed preferences invalidate old list proof; a soft workload wish
        # still leaves the source variant locally eligible, with actual advice.
        self.assertTrue(self.detail_job['_authenticated_local_checks']['passes'])
        self.assertEqual(self.detail_job['_authenticated_local_checks']['match']['_workload_preference_outcomes'][0]['outcome'], 'fail')
        self.assertFalse(self.detail_job['_authenticated_membership_known'])
        self.f.profile['constraints']['hard_constraints'] = ['part-time only']
        self.f.profile = with_preference_model(self.f.profile, self.f.profile['preferences']['preference_model'])
        self.f.get(link)
        self.assertTrue(self.detail_job['_authenticated_local_checks']['passes'],
                        'An external fixture label cannot become a confirmed hard limit')
        # This is a newly confirmed synthetic limit. The historical base fixture
        # uses external_fixture provenance, which correctly cannot author it.
        for ref in self.f.profile['provenance']['field_sources']:
            if ref['field_path'] == 'constraints.hard_constraints[0]':
                ref.update(source_kind='user_confirmation', explicit=True)
        body = self.f.get(link).body
        self.assertFalse(self.detail_job['_authenticated_local_checks']['passes'])
        self.assertFalse(self.detail_job['_authenticated_membership_known'])
        self.f.profile = deepcopy(original)
        self.f.set_preferences('full_time')
        overlay = self.f.integration._metadata_overlay
        self.f.integration._metadata_overlay = OpportunityMetadataOverlay(overlay.path.with_suffix('.other'), {})
        body = self.f.get(link).body
        self.assertTrue(self.detail_job['_authenticated_local_checks']['passes'])
        self.assertFalse(self.detail_job['_authenticated_membership_known'])

    def test_unrelated_inventory_commit_invalidates_list_proof_without_local_ineligibility(self):
        r, _, _ = self.matches(); link = self.card_link(r)
        self.f.update_inventory("UPDATE canonical_opportunities SET canonical_title=canonical_title || ' updated' WHERE id=7005")
        body = self.f.get(link).body
        self.assertFalse(self.detail_job['_authenticated_membership_known'])
        self.assertTrue(self.detail_job['_authenticated_local_checks']['passes'])

    def test_writer_can_commit_during_evaluation_and_render_in_delete_and_wal_modes(self):
        from wahojobs import authenticated_variant_details as variants
        for journal_mode in ('delete', 'wal'):
            with closing(sqlite3.connect(self.f.path)) as writer:
                self.assertEqual(writer.execute('PRAGMA journal_mode=' + journal_mode).fetchone()[0], journal_mode)
            self.f.update_inventory("UPDATE jobs SET location='Remote - Portugal', url='https://jobs.example.test/synthetic-part-time' WHERE id=7006")
            original = variants.resolve_scoped_variant
            committed = []
            def concurrent(*args, **kwargs):
                with closing(sqlite3.connect(self.f.path, timeout=0)) as writer, writer:
                    writer.execute("UPDATE jobs SET location='Remote - United States', url='https://jobs.example.test/changed' WHERE id=7006")
                committed.append(True)
                return original(*args, **kwargs)
            from wahojobs import authenticated_source_detail as detail_view
            render = detail_view.render_authenticated_job_page
            def render_with_write(*args, **kwargs):
                self.f.update_inventory("UPDATE jobs SET department='Changed after snapshot' WHERE id=7006")
                return render(*args, **kwargs)
            with patch.object(variants, 'resolve_scoped_variant', side_effect=concurrent), patch.object(detail_view, 'render_authenticated_job_page', side_effect=render_with_write):
                response = self.f.get('/job/opportunity-7002?variant=7006')
            self.assertEqual(response.status, 200)
            self.assertEqual(committed, [True])
            self.assertIn(b"href='https://jobs.example.test/synthetic-part-time'", response.body)
            self.assertEqual(self.detail_job['_authenticated_local_checks']['match']['location_eligibility_status'], 'eligible')
            current = self.f.get('/job/opportunity-7002?variant=7006')
            self.assertIn(b"href='https://jobs.example.test/changed'", current.body)
            self.assertIn(b'restriction conflicts with your profile', current.body)

    def test_expired_old_run_and_missing_run_do_not_restore_membership(self):
        r, _, _ = self.matches(); link = self.card_link(r)
        self.f.advance(193)
        result = self.f.get(link)
        self.assertEqual(result.status, 200)
        self.assertFalse(self.detail_job['_authenticated_membership_known'])
        self.assertIn(b'Availability needs rechecking', result.body)
        self.assertNotIn(b'Apply on company site</a>', result.body)
        missing = self.f.get('/job/opportunity-7002?variant=7006&run=' + 'z'*24)
        self.assertEqual(missing.status, 404)

    def test_valid_list_proof_can_exclude_a_locally_eligible_unselected_variant(self):
        self.f.update_inventory("UPDATE jobs SET location='Remote - Portugal' WHERE id=7003")
        _, run, visible = self.matches()
        alternate = ({7003, 7006} - {visible[0]['job_id']}).pop()
        response = self.f.get('/job/opportunity-7002?variant=' + str(alternate) + '&run=' + run.match_run_id)
        self.assertEqual(response.status, 200)
        self.assertTrue(self.detail_job['_authenticated_membership_known'])
        self.assertIsNone(self.detail_job['_authenticated_recommendation'])
        self.assertTrue(self.detail_job['_authenticated_local_checks']['passes'])
        self.f.update_inventory('UPDATE jobs SET is_active=0')
        closed = self.f.get('/job/opportunity-7002')
        self.assertEqual(closed.status, 200)
        self.assertIn(b'Listing marked inactive', closed.body)
        self.assertFalse(self.detail_job['_authenticated_membership_known'])


if __name__ == '__main__':
    unittest.main()
