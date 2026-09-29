"""Exact retained rows and destination/country counterexamples; no network."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from tests import test_catalog_display_feedback as display_fixtures
from wahojobs import public_job_page as detail, public_jobs_catalog as catalog
from wahojobs.catalog_display import location_summary
from wahojobs.catalog_source_links import prepare_source_links
from wahojobs.public_catalog_reader import prepare_publication

FIXTURES = json.loads((Path(__file__).parent/'fixtures/public_catalog_oneforma.json').read_text(encoding='utf8'))


class OneFormaPublicTests(unittest.TestCase):
    setUp = display_fixtures.CatalogDisplayTests.setUp

    def retained(self, jid=7039):
        self.job.update(deepcopy(next(v for v in FIXTURES if v['job_id'] == jid)))

    def metadata(self, **values):
        data = json.loads(self.job['rich_metadata_json']); data.update(values)
        self.job['rich_metadata_json'] = json.dumps(data)

    def prepared(self):
        catalog.prepare_catalog_presentation(self.job)
        before = deepcopy(self.job)
        groups, _ = prepare_publication([self.job])
        self.assertEqual(before, self.job)
        return groups[0]

    def test_exact_retained_remote_country_and_destinations(self):
        self.retained(); job = self.prepared()
        self.assertEqual(location_summary(job), 'Remote · United States')
        self.assertEqual(job['_public_geography']['countries'], ('United States',))
        self.assertEqual(job['_catalog_location_model']['scope'], 'remote_restricted')
        self.assertEqual(catalog.build_catalog([job], {'location':'Malaysia'})['result_count'], 0)
        self.assertEqual(catalog.build_catalog([job], {'location':'United States'})['result_count'], 1)
        self.assertEqual(catalog.build_catalog([job], {'location':'Worldwide'})['result_count'], 0)
        page = detail.render_public_job_page(job, public_origin='https://www.wahojobs.com', public_reader=True)
        self.assertIn('Apply on OneForma', page)
        self.assertIn('View original listing on OneForma', page)
        self.assertIn('requestId=1827625416331265', page)
        self.assertIn('project-juniper-paid-audio-video-recording-study-5/', page)
        self.assertIn('after signup is unverified', page)
        self.assertNotIn('8 countries', page)
        self.assertIn('3.5 hours', page)
        self.assertIn('completion and quality acceptance', page)
        self.assertIn('September 28, 2026', page)
        card = catalog.render_job_card(job, return_to='/jobs?q=recording', include_variant=True)
        self.assertIn('Remote · United States', card)
        self.assertIn('return_to=%2Fjobs%3Fq%3Drecording', card)

    def test_siblings_keep_exact_country_and_apply_pair(self):
        for jid, country in ((7040,'Malaysia'), (7031,'Belgium'), (7087,'United Arab Emirates')):
            with self.subTest(jid=jid):
                self.retained(jid); job = self.prepared()
                self.assertEqual(location_summary(job), 'Remote · ' + country)
                self.assertEqual(job['_public_links']['apply'], self.job['listing_url'])
                self.assertEqual(job['_public_geography']['countries'], (country,))

    def test_multiple_options_do_not_create_country_language_cross_product(self):
        variants = []
        for jid in (7039,7040):
            self.retained(jid); variants.append(self.prepared())
        group = dict(variants[0], _catalog_variants=tuple(variants))
        self.assertEqual(catalog.build_catalog([group], {'location':'Malaysia','language':'Vietnamese'})['result_count'], 0)
        self.assertEqual(catalog.build_catalog([group], {'location':'Malaysia','language':'Chinese'})['result_count'], 1)

    def test_absent_mode_is_unknown_even_when_country_is_known(self):
        self.retained(); self.job['rich_body'] = '<p>Apply in the country selected during application.</p>'
        job = self.prepared()
        self.assertEqual(job['_public_geography']['mode'], 'unknown')
        self.assertEqual(location_summary(job), 'United States')

    def test_locale_or_translation_pair_without_country_instruction_is_not_residence(self):
        self.retained(); self.job['rich_body'] = '<p>Work Mode: remote</p><p>Evaluate language samples.</p>'
        job = self.prepared()
        self.assertEqual(len(job['_public_geography']['countries']), 8)
        self.retained(); self.metadata(variant_language='English (United States) - Spanish (Chile)')
        self.assertEqual(len(self.prepared()['_public_geography']['countries']), 8)

    def test_manual_or_evidenced_scoped_geography_wins(self):
        for field in ('workplace_mode','eligible_countries','location_scope'):
            self.retained(); self.job['overridden_fields'] = ['attributes.work_arrangement.' + field]
            job = self.prepared()
            if field == 'workplace_mode': self.assertEqual(job['_public_geography']['mode'], 'unknown')
            else: self.assertEqual(len(job['_public_geography']['countries']), 8)
        self.retained()
        self.job['enrichment']['variant_facts'][1]['evidence'] = [{'evidence_text':'A reviewed condition.'}]
        self.assertEqual(len(self.prepared()['_public_geography']['countries']), 8)

    def test_unbound_variant_metadata_cannot_replace_source_or_geography(self):
        self.retained(); self.metadata(variant_apply_url='https://my.oneforma.com/webapp/nv/signup?requestId=other')
        job = self.prepared()
        self.assertEqual(job['_public_geography']['mode'], 'unknown')
        self.assertEqual(len(job['_public_geography']['countries']), 8)
        self.assertIsNone(job['_public_links']['source'])

    def test_application_only_has_one_honest_control_and_no_invented_source(self):
        self.retained(); self.metadata(public_url=None)
        job = self.prepared(); page = detail.render_public_job_page(job, public_origin='https://www.wahojobs.com', public_reader=True)
        self.assertEqual(page.count("href='" + job['official_url'] + "'"), 1)
        self.assertIn('application/registration page (via Apply)', page)
        self.assertNotIn('original listing', page)

    def test_invalid_or_homepage_source_is_not_substituted(self):
        for url in ('javascript:alert(1)','https://www.oneforma.com/','https://other.test/projects/unrelated/',
                    'https://www.oneforma.com@other.test/projects/unrelated/'):
            self.retained(); self.metadata(public_url=url)
            self.assertIsNone(prepare_source_links(self.job)['source'])

    def test_existing_separate_actions_and_shared_pages(self):
        for provider in ('dataforce','dataannotation','handshake'):
            self.retained(); self.job.update(company_slug=provider, rich_provider=provider,
                listing_url='https://employer.test/jobs/role', rich_source_url='https://employer.test/jobs/role',
                official_url='https://employer.test/jobs/role')
            self.metadata(application_url='https://employer.test/signup?project=exact&variant=two')
            links = prepare_source_links(self.job)
            self.assertEqual(links['source'], 'https://employer.test/jobs/role')
            self.assertEqual(links['apply'], 'https://employer.test/signup?project=exact&variant=two')
            self.metadata(application_url='javascript:alert(1)')
            links = prepare_source_links(self.job)
            self.assertEqual(links['apply'], links['source'])
            self.assertFalse(links['registration'])

    def test_unrelated_providers_and_private_renderer_keep_existing_behavior(self):
        self.retained(); self.job.update(company_slug='acme',rich_provider='acme')
        self.metadata(public_url='https://www.oneforma.com/projects/other/',application_url='https://other.test/apply')
        self.assertEqual(prepare_source_links(self.job)['apply'], self.job['official_url'])
        self.retained(); page = detail.render_public_job_page(self.prepared(), public_origin='https://app.test')
        self.assertIn('Apply on company site',page)
        self.assertNotIn('View original listing',page)


if __name__ == '__main__': unittest.main()
