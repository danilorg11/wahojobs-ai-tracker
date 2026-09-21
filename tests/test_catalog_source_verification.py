"""Fixture-only negative contrasts for the existing Mercor verification contract."""
from copy import deepcopy
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch
from wahojobs import public_jobs_catalog as catalog, public_job_page as detail
from wahojobs.catalog_source_geography import mercor_candidate_eligibility
from tests import test_mercor_observation_semantics as fixture
from tests import test_browse_filter_correction as browse


class MercorCatalogContractTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.MercorObservationSemanticsTests('runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.conn = self.f.conn
        self.now = datetime.fromisoformat(fixture.NOW)

    def load(self, now=None):
        return catalog.load_public_jobs(self.conn, now=now or self.now)

    def observe(self, ids, at=fixture.NOW):
        return self.f.observe([self.f.listing_for(i) for i in ids], at)

    def test_individual_observation_and_omitted_record_have_independent_clocks(self):
        self.observe(['observed', 'omitted'], fixture.OLD)
        before = deepcopy(self.f.rows()['omitted'])
        _, run = self.observe(['observed'])
        with patch('scripts.profile_match_digest.get_active_rows', side_effect=AssertionError('no matching pipeline')):
            jobs = self.load()
        self.assertEqual([j['external_id'] for j in jobs], ['observed'])
        job = jobs[0]
        self.assertEqual(job['source_run_id'], run)
        self.assertEqual(job['latest_successful_source_run_at'], fixture.NOW)
        self.assertEqual(self.f.rows()['omitted'], before)
        self.assertTrue(self.f.rows()['omitted']['is_active'])
        selected = detail.load_public_job(self.conn, job['path'], now=self.now, selected_job_id=job['job_id'])
        self.assertEqual(selected['availability_trust'], job['availability_trust'])
        self.assertEqual(self.conn.execute('SELECT status FROM crawl_runs WHERE id=?', (run,)).fetchone()[0], 'partial')
        self.assertEqual(len(self.load(self.now + timedelta(hours=72))), 1)
        self.assertEqual(self.load(self.now + timedelta(hours=72, seconds=1)), [])
        near_expiry = self.now + timedelta(hours=71, minutes=59, seconds=59)
        self.assertEqual(catalog.catalog_cache_deadline(jobs, near_expiry), self.now + timedelta(hours=72))

    def test_nonqualifying_record_and_parent_evidence_do_not_admit(self):
        self.observe(['one'])
        capture = dict(self.conn.execute('SELECT * FROM job_source_content_captures ORDER BY id DESC LIMIT 1').fetchone())
        cases = [('record_promotion_contract_id', 'unknown-contract'),
                 ('promotion_policy_version', 'unknown-policy'), ('promotion_decision', 'held_non_authoritative'),
                 ('provider', 'other'), ('source_type', 'other'), ('used_sample_data', 1),
                 ('normalized_record_count', 0), ('provider_outcome', 'anomalous'),
                 ('observed_at', 'not-a-date')]
        for key, value in cases:
            with self.subTest(key=key):
                self.conn.execute('UPDATE job_source_content_captures SET ' + key + '=? WHERE id=?', (value, capture['id']))
                self.assertEqual(self.load(), [])
                self.conn.execute('UPDATE job_source_content_captures SET ' + key + '=? WHERE id=?', (capture[key], capture['id']))
        run = dict(self.conn.execute('SELECT * FROM crawl_runs WHERE id=?', (capture['crawl_run_id'],)).fetchone())
        for key, value in [('status', 'failed'), ('used_sample_data', 1), ('company_id', 9999)]:
            with self.subTest(parent_key=key):
                # Cross-company fixture uses an existing company, preserving FK integrity.
                if key == 'company_id':
                    value = self.conn.execute("INSERT INTO companies(name,slug,careers_url,source_tier,inventory_model,market_count_policy) VALUES ('Other','other','https://example.test','core','live_feed','count_live')").lastrowid
                self.conn.execute('UPDATE crawl_runs SET ' + key + '=? WHERE id=?', (value, run['id']))
                self.assertEqual(self.load(), [])
                self.conn.execute('UPDATE crawl_runs SET ' + key + '=? WHERE id=?', (run[key], run['id']))

    def test_inactive_job_and_canonical_are_excluded(self):
        self.observe(['one'])
        row = self.f.rows()['one']
        for table, identity in [('jobs', row['id']), ('canonical_opportunities', row['canonical_opportunity_id'])]:
            self.conn.execute('UPDATE ' + table + ' SET is_active=0 WHERE id=?', (identity,))
            self.assertEqual(self.load(), [])
            self.conn.execute('UPDATE ' + table + ' SET is_active=1 WHERE id=?', (identity,))

    def test_accepted_content_geography_overrides_generic_worldwide(self):
        self.f.observe([self.f.listing_for('restricted', location='Remote — worldwide',
            eligibleLocation=['USA'], eligibleResidenceLocation=['USA'])])
        job = self.load()[0]
        self.assertEqual(job['_catalog_filter_values']['location'], {'united states'})
        self.assertEqual(catalog.build_catalog([job], {'location': 'Brazil'})['result_count'], 0)
        selected = detail.load_public_job(self.conn, job['path'], now=self.now)
        self.assertEqual(detail.candidate_job_eligibility(selected)['countries'], ('United States',))
        html = detail.render_public_job_page(selected, public_origin='http://example.test')
        self.assertNotIn('<dd>Remote — worldwide</dd>', html)
        self.assertIn('Current location', html)
        self.assertIn('Residence', html)
        self.assertIn('Employer’s location wording:', html)


class GeographyDimensionTests(unittest.TestCase):
    setUp = browse.BrowseFilterCorrectionTests.setUp
    load = browse.BrowseFilterCorrectionTests.load

    def job(self, clauses):
        job = deepcopy(self.load()[0])
        job.update(company_slug='mercor', source_location='Remote — worldwide',
                   applicant_country_requirements=clauses,
                   applicant_geography_evidence=dict(job_id=job['job_id'], source_url=job['listing_url']))
        return job

    def clause(self, dimension, countries, mode='allow', **extra):
        return dict(dimension=dimension, mode=mode, countries=countries, unresolved=False,
                    source_field='fixture.' + dimension, **extra)

    def test_location_and_residence_are_not_unioned_or_intersected(self):
        job = self.job([self.clause('location', ['Canada', 'United States']),
                        self.clause('residence', ['United States'])])
        catalog.prepare_catalog_presentation(job)
        self.assertEqual(job['_catalog_filter_values']['location'], {'canada', 'united states'})
        model = job['_catalog_location_model']
        self.assertEqual(model['country_filter_dimension'], 'location')
        self.assertEqual(model['applicant_country_dimensions']['residence']['allowed'], ('United States',))
        self.assertIn('residence requirements also apply', job['catalog_location'])

    def test_residence_only_is_labelled_and_exclusions_do_not_create_worldwide(self):
        job = self.job([self.clause('residence', ['Brazil', 'Canada']),
                        self.clause('residence', ['Canada'], mode='exclude')])
        catalog.prepare_catalog_presentation(job)
        self.assertEqual(job['_catalog_filter_values']['location'], {'brazil'})
        self.assertIn('Required residence', job['catalog_location'])
        self.assertEqual(job['_catalog_location_model']['country_filter_dimension'], 'residence')
        only_exclusion = self.job([self.clause('location', ['Canada'], mode='exclude')])
        catalog.prepare_catalog_presentation(only_exclusion)
        self.assertEqual(only_exclusion['_catalog_filter_values']['location'], set())
        self.assertNotIn('anywhere', only_exclusion['catalog_location'])

    def test_unclear_conflicting_or_wrong_identity_never_grants_country(self):
        cases = [
            [dict(self.clause('location', ['Canada']), unresolved=True)],
            [dict(self.clause('location', ['Canada']), source_conflict=True)],
            [self.clause('location', ['Canada']), self.clause('location', ['Canada'], mode='exclude')],
        ]
        for clauses in cases:
            job = self.job(clauses)
            catalog.prepare_catalog_presentation(job)
            self.assertEqual(job['_catalog_filter_values']['location'], set())
            self.assertIn('confirmation', job['catalog_location'])
        job = self.job([self.clause('location', ['Canada'])])
        job['applicant_geography_evidence']['job_id'] = -1
        catalog.prepare_catalog_presentation(job)
        self.assertEqual(job['_catalog_filter_values']['location'], set())

    def test_preferences_and_experience_alternatives_are_not_country_permissions(self):
        for wording in ('Remote — preferred: Brazil', 'US-based or deep US-market experience', 'Remote'):
            job = self.job([])
            job['source_location'] = wording
            catalog.prepare_catalog_presentation(job)
            self.assertEqual(job['_catalog_filter_values']['location'], set())
            self.assertIn('unconfirmed', job['catalog_location'])


if __name__ == '__main__':
    unittest.main()
