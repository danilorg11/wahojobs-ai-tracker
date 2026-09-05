"""Clock-controlled product regressions; lifecycle edge values are synthetic.

The three description bodies reuse accepted captures from card_source_wording.
No tests request an application page or certify live application acceptance.
"""
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from html import unescape
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from wahojobs import authenticated_profile_matches as browser, public_job_page
from wahojobs.authenticated_source_detail import append_authenticated_source_detail
from wahojobs.authenticated_variant_details import load_scoped_snapshot, variant_detail_url
from wahojobs.crawler.providers import mercor
from wahojobs.db.repository import create_crawl_run, finish_crawl_run
from wahojobs.matching.opportunity_trust import assess_opportunity_trust
from wahojobs.tracking.service import track_crawl_result


ROOT = Path(__file__).parent / 'fixtures'


class AuthenticatedDetailAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.f = SyntheticMatcherFixture()
        self.addCleanup(self.f.close)
        self.f.update_inventory('UPDATE jobs SET is_active=0')
        self.f.update_inventory("INSERT INTO companies (id,name,slug,careers_url,source_tier,inventory_model,market_count_policy) VALUES (8001,'Mercor','mercor',?,'core','live_feed','count_live')", (mercor.MERCOR_ENDPOINT,))
        self.old = (self.f.now - timedelta(days=10)).isoformat()
        self.observed = self.f.now.isoformat()
        self.f.update_inventory("INSERT INTO crawl_runs (id,company_id,status,started_at,finished_at,used_sample_data) VALUES (8002,8001,'success',?,?,0)", (self.old, self.old))
        self.listing = json.loads((ROOT / 'mercor_observation_contract.json').read_text(encoding='utf-8'))['listing']
        self.observe(('returned', 'absent'), self.old)
        self.before_absent = self.record('absent')
        self.summary, self.run_id = self.observe(('returned',), self.observed)
        self.returned = self.record('returned')

    def observe(self, identities, at):
        listings = [dict(deepcopy(self.listing), listingId=identity,
                         title=('Python Backend AI Coding Evaluator' if identity == 'returned'
                                else 'Python API AI Coding Evaluator'),
                         commitment='full-time', location='Remote') for identity in identities]
        with closing(sqlite3.connect(self.f.path)) as conn:
            conn.row_factory = sqlite3.Row
            result = mercor.parse_mercor_observations({'listings': listings})
            run_id = create_crawl_run(conn, 8001, at)
            with patch('wahojobs.tracking.service.tracking_openai_client', return_value=None):
                summary = track_crawl_result(conn, 8001, run_id, result, at)
            finish_crawl_run(conn, run_id, summary, at, status='partial', error_message='partial synthetic fixture')
            conn.commit()
        return summary, run_id

    def record(self, identity):
        with self.f.provider() as conn:
            return dict(conn.execute('SELECT * FROM jobs WHERE company_id=8001 AND external_id=?', (identity,)).fetchone())

    def path(self, record=None):
        record = record or self.returned
        return f"/job/opportunity-{record['canonical_opportunity_id']}?variant={record['id']}"

    def snapshot(self, record=None):
        record = record or self.returned
        with self.f.provider() as conn:
            conn.execute('BEGIN')
            result = load_scoped_snapshot(conn, record['canonical_opportunity_id'], record['id'], now=self.f.now)
            conn.rollback()
        return result

    def selected_run(self):
        reply = self.f.get()
        self.assertEqual(reply.status, 200)
        run = self.f.last_run()
        return reply, run, browser._primary_presentation_matches(run.recommendation_context)

    def assert_unavailable(self, path):
        reply = self.f.get(path)
        self.assertEqual(reply.status, 200)
        self.assertIn(b'Opportunity unavailable', reply.body)
        self.assertIn(b'This saved listing is no longer current', reply.body)
        self.assertNotIn(b'This is the source variant shown in your current matches.', reply.body)
        self.assertNotIn(b'Apply on company site</a>', reply.body)
        return reply

    def test_fresh_individual_observation_is_shared_without_opening_the_public_site(self):
        reply, run, visible = self.selected_run()
        self.assertEqual([m['job_id'] for m in visible], [self.returned['id']])
        snap = self.snapshot()
        admission = snap['rows'][0]
        detail = next(r for r in snap['detail_evidence']['rows'] if r['job_id'] == admission['job_id'])
        self.assertEqual(assess_opportunity_trust(admission, 'unknown', now=self.f.now),
                         assess_opportunity_trust(detail, 'unknown', now=self.f.now))
        self.assertEqual(detail['latest_successful_source_run_at'], self.observed)
        self.assertEqual(detail['source_run_id'], self.run_id)
        for target in (variant_detail_url(visible[0], run_id=run.match_run_id), self.path().split('?')[0]):
            response = self.f.get(target)
            self.assertEqual(response.status, 200)
            self.assertNotIn(b'Opportunity unavailable', response.body)
            self.assertIn(self.returned['url'], unescape(response.body.decode()))
            self.assertIn(b'Confirm current terms and application availability', response.body)
            self.assertNotIn(self.observed.encode(), response.body)  # raw dates stay out of the candidate view
        with self.f.provider() as conn:
            public = public_job_page.load_public_job(conn, self.path().split('?')[0], now=self.f.now)
        self.assertEqual(public['public_state'], 'temporarily_unavailable')
        self.assertEqual(public['latest_successful_source_run_at'], self.old)

    def test_expiry_invalidates_old_run_and_keeps_existing_recent_cache_warning(self):
        _, run, visible = self.selected_run()
        link = variant_detail_url(visible[0], run_id=run.match_run_id)
        self.f.advance(73)
        self.assert_unavailable(link)
        reply = self.f.get('/find-matches?run=' + run.match_run_id)
        self.assertIn(b'It needs availability confirmation.', reply.body)
        self.assertNotIn(b'good fit right now', reply.body)
        self.f.advance(120)
        self.assert_unavailable(link)
        expired = self.f.get('/find-matches?run=' + run.match_run_id)
        self.assertNotIn(b"class='match-card'", expired.body)

    def test_description_capture_without_approved_observation_does_not_refresh(self):
        # Keep the recent raw/accepted body and capture date; remove only its
        # authority. Neither page length nor a description timestamp can renew.
        self.f.update_inventory("UPDATE job_source_content_captures SET record_promotion_contract_id='' WHERE job_id=?", (self.returned['id'],))
        self.assert_unavailable(self.path())
        self.assertEqual(self.selected_run()[2], [])

    def test_invalid_capture_and_failed_or_sample_run_provenance_cannot_renew(self):
        changes = [('record_promotion_contract_id', 'unrecognized'),
                   ('promotion_policy_version', 'unrecognized'),
                   ('provider_outcome', 'anomalous'), ('used_sample_data', 1),
                   ('normalized_record_count', 99), ('observed_at', 'not-a-time')]
        with self.f.provider() as conn:
            capture = dict(conn.execute('SELECT * FROM job_source_content_captures WHERE job_id=? ORDER BY id DESC LIMIT 1', (self.returned['id'],)).fetchone())
        for field, value in changes:
            with self.subTest(field=field):
                self.f.update_inventory(f'UPDATE job_source_content_captures SET {field}=? WHERE id=?', (value, capture['id']))
                self.assert_unavailable(self.path())
                self.assertEqual(self.selected_run()[2], [])
                self.f.update_inventory(f'UPDATE job_source_content_captures SET {field}=? WHERE id=?', (capture[field], capture['id']))
        for field, bad, restored in [('status', 'failed', 'partial'), ('used_sample_data', 1, 0)]:
            with self.subTest(run_field=field):
                self.f.update_inventory(f'UPDATE crawl_runs SET {field}=? WHERE id=?', (bad, self.run_id))
                self.assert_unavailable(self.path())
                self.assertEqual(self.selected_run()[2], [])
                self.f.update_inventory(f'UPDATE crawl_runs SET {field}=? WHERE id=?', (restored, self.run_id))

    def test_closed_variant_and_canonical_never_resurrect_from_a_saved_run(self):
        for table, identity in [('jobs', self.returned['id']), ('canonical_opportunities', self.returned['canonical_opportunity_id'])]:
            with self.subTest(table=table):
                _, run, visible = self.selected_run()
                link = variant_detail_url(visible[0], run_id=run.match_run_id)
                self.f.update_inventory(f'UPDATE {table} SET is_active=0 WHERE id=?', (identity,))
                self.assert_unavailable(link)
                self.assertNotIn(b"class='match-card'", self.f.get('/find-matches?run=' + run.match_run_id).body)
                self.f.update_inventory(f'UPDATE {table} SET is_active=1 WHERE id=?', (identity,))

    def test_partial_response_retains_absent_record_without_renewal_or_deactivation(self):
        self.assertFalse(self.summary.snapshot_complete)
        self.assertFalse(self.summary.pagination_complete)
        self.assertFalse(self.summary.removals_authorized)
        self.assertEqual(self.summary.jobs_removed, 0)
        self.assertEqual(self.record('absent'), self.before_absent)
        self.assert_unavailable(self.path(self.before_absent))
        self.assertEqual(self.record('absent'), self.before_absent)

    def test_new_failed_retrieval_does_not_erase_prior_valid_observation(self):
        self.f.update_inventory("INSERT INTO crawl_runs (company_id,status,started_at,finished_at,used_sample_data,error_message) VALUES (8001,'failed',?,?,0,'synthetic retrieval failure')", (self.observed, self.observed))
        self.assertNotIn(b'Opportunity unavailable', self.f.get(self.path()).body)
        self.assertEqual(self.snapshot()['detail_evidence']['rows'][0]['source_run_id'], self.run_id)

    def test_valid_source_snapshot_fallback_still_requires_record_observation(self):
        self.f.update_inventory("UPDATE job_source_content_captures SET record_promotion_contract_id='' WHERE job_id IN (SELECT id FROM jobs WHERE company_id=8001)")
        self.f.update_inventory('UPDATE crawl_runs SET started_at=?,finished_at=? WHERE id=8002', (self.observed, self.observed))
        self.assertNotIn(b'Opportunity unavailable', self.f.get(self.path()).body)
        self.assertEqual(self.snapshot()['detail_evidence']['rows'][0]['source_run_id'], 8002)
        self.assert_unavailable(self.path(self.before_absent))

    def test_other_provider_retains_source_snapshot_policy(self):
        self.f.update_inventory('UPDATE jobs SET is_active=1 WHERE id=7003')
        with self.f.provider() as conn:
            original = public_job_page.load_public_job(conn, '/job/opportunity-7002', now=self.f.now)
        self.assertEqual(original['public_state'], 'live')
        self.assertNotIn(b'Opportunity unavailable', self.f.get('/job/opportunity-7002?variant=7003').body)
        self.f.update_inventory('UPDATE crawl_runs SET started_at=?,finished_at=? WHERE id=7004', (self.old,self.old))
        self.assert_unavailable('/job/opportunity-7002?variant=7003')

    def test_details_keep_computation_scoped_after_the_read_transaction(self):
        from contextlib import contextmanager
        original_provider = self.f.provider
        connections, scored = [], []
        @contextmanager
        def provider():
            with original_provider() as conn:
                connections.append(conn)
                try: yield conn
                finally: connections.remove(conn)
        self.f.integration._connection_provider = provider
        original_score = browser.profile_preview.matcher.score_opportunity
        def score(profile, row):
            self.assertFalse(any(c.in_transaction for c in connections))
            self.assertEqual(row['canonical_opportunity_id'], self.returned['canonical_opportunity_id'])
            scored.append(row['job_id'])
            return original_score(profile, row)
        with (patch.object(browser.AuthenticatedProfileMatchesBrowserIntegration, '_load_inventory', side_effect=AssertionError('full catalog')),
              patch.object(browser.AuthenticatedProfileMatchesBrowserIntegration, '_render_persistent_matches', side_effect=AssertionError('full matching')),
              patch.object(browser.profile_preview.matcher, 'score_opportunity', side_effect=score)):
            for target in (self.path(), self.path().split('?')[0]):
                self.assertEqual(self.f.get(target).status, 200)
        self.assertEqual(scored, [self.returned['id'], self.returned['id']])


class AcceptedListingDescriptionTests(unittest.TestCase):
    def test_captured_demonstration_wording_remains_variant_bound_and_unassessed(self):
        sources = json.loads((ROOT / 'card_source_wording.json').read_text(encoding='utf-8'))
        expected = {11242: ['**Required**', '**Preferred**', 'Docker'],
                    11271: ['Ideal Qualifications', 'exceptional depth'],
                    1039: ['not a specific job posting', 'future contract opportunities']}
        for source in sources:
            job = dict(company_slug=source['source_slug'], external_id=source['external_id'],
                       rich_provider=source['source_slug'], rich_external_id=source['content_external_id'],
                       official_url=source['url'], rich_source_url=source['source_url'],
                       rich_body=source['body'], rich_body_format=source['body_format'],
                       rich_metadata_json=source['metadata_json'], last_captured_at=source['last_captured_at'])
            content = "<div class='job-description'></div>"
            rendered = unescape(append_authenticated_source_detail(content, job, authenticated=True))
            for phrase in expected[source['job_id']]: self.assertIn(phrase, rendered)
            self.assertIn('unassessed against your profile', rendered)
            self.assertIn(source['url'], rendered)
            self.assertEqual(append_authenticated_source_detail(content, job, authenticated=False), content)
            for key in ('rich_provider', 'rich_external_id', 'rich_source_url'):
                self.assertEqual(append_authenticated_source_detail(content, dict(job, **{key: 'different'}), authenticated=True), content)
