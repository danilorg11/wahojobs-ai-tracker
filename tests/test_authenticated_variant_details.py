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
        self.assertIn('This is the source variant shown in your current matches.', body)
        self.assertIn('Applicant-location compatibility is supported', body)
        self.assertIn('Source record: synthetic-part-time', body)
        self.assertIn("href='https://jobs.example.test/synthetic-part-time'", body)
        self.assertNotIn("href='https://jobs.example.test/distinctive-bilingual-reviewer'", body)
        for reason in browser._candidate_match_explanations(visible[0]):
            self.assertIn(reason, unescape(body))

    def test_unknown_alternative_is_not_worldwide_permission(self):
        self.f.update_inventory("UPDATE jobs SET location='Remote' WHERE id=7006")
        r, _, visible = self.matches()
        self.assertEqual(visible[0]['location_eligibility_status'], 'unknown')
        detail = self.f.get(self.card_link(r))
        self.assertEqual(detail.status, 200)
        self.assertIn(b'Applicant-location eligibility remains unresolved', detail.body)
        self.assertIn(b'Remote work does not establish worldwide eligibility', detail.body)

    def test_normal_canonical_navigation_uses_current_representative(self):
        self.matches()
        r = self.f.get('/job/opportunity-7002')
        self.assertEqual(r.status, 200)
        self.assertIn(b'Source record: synthetic-part-time', r.body)
        self.assertIn(b'This is the source variant shown in your current matches.', r.body)
        # An explicitly inspected excluded variant remains a source record,
        # with no claim that it is still a recommendation.
        old = self.f.get('/job/opportunity-7002?variant=7003')
        self.assertEqual(old.status, 200)
        self.assertIn(b'This source variant is not in your current recommendations.', old.body)
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
        self.assertIn(b'not in your current recommendations', stale.body)
        self.assertNotIn(b'Apply on company site</a>', stale.body)
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
        self.assertIn(b'Source record: same-source', detail.body)
        self.assertIn(b"href='https://jobs.example.test/same-source'", detail.body)

    def test_relaxation_link_keeps_exact_variant_and_is_not_a_main_match_claim(self):
        self.f.set_preferences('part_time')
        r, _, visible = self.matches()
        self.assertEqual(visible, [])
        self.assertIn(b'More opportunities if', r.body)
        link = unescape(re.search(r"href='([^']+)'>View job details", r.body.decode()).group(1))
        self.assertIn('variant=7006', link)
        detail = self.f.get(link)
        self.assertEqual(detail.status, 200)
        self.assertIn(b'preference-relaxation preview', detail.body)

    def test_public_navigation_is_unchanged_and_unknown_queries_are_rejected(self):
        with self.f.provider() as conn:
            original = public_job_page.load_public_job(conn, '/job/opportunity-7002', now=self.f.now)
            exact = public_job_page.load_public_job(conn, '/job/opportunity-7002', now=self.f.now, selected_job_id=7006)
        self.assertEqual(original['job_id'], 7003)
        self.assertEqual(exact['job_id'], 7006)
        self.assertIsNone(parse_variant_query('unknown=1'))

    def test_no_reuse_token_still_uses_consistent_read_transaction(self):
        r, _, _ = self.matches()
        with patch.object(browser.AuthenticatedProfileMatchesBrowserIntegration,
                          '_inventory_commit_token', return_value=None):
            detail = self.f.get(self.card_link(r))
        self.assertEqual(detail.status, 200)
        self.assertIn(b'Source record: synthetic-part-time', detail.body)

    def test_concurrent_inventory_commit_cannot_split_selection_from_details(self):
        original = public_job_page.load_public_job
        attempts = []
        def concurrent(*args, **kwargs):
            with closing(sqlite3.connect(self.f.path, timeout=0)) as writer:
                with self.assertRaises(sqlite3.OperationalError):
                    writer.execute("UPDATE jobs SET is_active=0")
                    writer.commit()
                writer.rollback()
                attempts.append(True)
            return original(*args, **kwargs)
        with patch.object(public_job_page, 'load_public_job', side_effect=concurrent):
            self.assertEqual(self.f.get('/job/opportunity-7002?variant=7006').status, 200)
        self.assertEqual(attempts, [True])
        # Lock is released on exit; no persistent lock or background machinery.
        self.f.update_inventory("UPDATE jobs SET is_active=0")


if __name__ == '__main__':
    unittest.main()
