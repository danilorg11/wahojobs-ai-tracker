"""Public-only facts from exact retained roles and bounded counterexamples."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from tests import test_catalog_display_feedback as display_fixtures
from wahojobs import public_jobs_catalog as catalog, public_job_page as detail
from wahojobs.catalog_display import location_summary
from wahojobs.catalog_source_presentation import contribution_context
from wahojobs.public_catalog_reader import prepare_publication
from wahojobs.opportunity_enrichment import source_body_paragraphs

FIXTURES = json.loads((Path(__file__).parent/'fixtures/public_catalog_presentation.json').read_text(encoding='utf8'))


class PublicPresentationTests(unittest.TestCase):
    setUp = display_fixtures.CatalogDisplayTests.setUp

    def prepared(self):
        catalog.prepare_catalog_presentation(self.job)
        before = deepcopy(self.job)
        groups, _ = prepare_publication([self.job])
        self.assertEqual(self.job, before)
        return groups[0]

    def retained(self, provider):
        row = next(r for r in FIXTURES if r['company_slug'] == provider)
        self.job.update({k:v for k,v in row.items() if k not in ('metadata','body','work_arrangement','variant_facts')})
        self.job.update(official_url=row['listing_url'], rich_body=row['body'],
                        rich_metadata_json=json.dumps(row['metadata']))
        self.job['enrichment']['attributes']['work_arrangement'] = deepcopy(row['work_arrangement'])
        self.job['enrichment']['variant_facts'] = deepcopy(row['variant_facts'])
        return self.prepared()

    def test_four_retained_records_share_card_detail_and_filter_facts(self):
        for provider, expected, activity in (
            ('rws','Remote · Malaysia','AI training & evaluation'),
            ('appen','Remote · Myanmar','Social media evaluation'),
            ('alignerr','Remote','AI security testing'),
            ('meridial','Remote · Worldwide','AI training & evaluation')):
            with self.subTest(provider=provider):
                self.setUp()
                job = self.retained(provider)
                self.assertEqual(location_summary(job), expected)
                self.assertEqual(job['_public_activity'], activity)
                for html in (catalog.render_job_card(job,return_to='/jobs?q=security',include_variant=True),
                             detail.render_public_job_page(job,public_origin='https://www.wahojobs.com',public_reader=True)):
                    self.assertIn(expected, html)
                    self.assertNotIn('Eligible in ', html)
                    self.assertNotIn('Advertised opportunity', html)
                    self.assertIn(job['official_url'] if '<h1' in html else 'return_to=', html)
                country = {'rws':'Malaysia','appen':'Myanmar'}.get(provider)
                if provider == 'appen':
                    self.assertTrue(job['catalog_summary'].startswith('Review and evaluate posts'),job['catalog_summary'])
                if country:
                    self.assertEqual(catalog.build_catalog([job], {'location':country})['result_count'],1)
                    self.assertEqual(catalog.build_catalog([job], {'location':'Brazil'})['result_count'],0)
                if provider == 'alignerr':
                    self.assertEqual(catalog.build_catalog([job], {'location':'Brazil'})['result_count'],0)
                    self.assertEqual(job['_public_state'],'limited')
                if provider == 'meridial':
                    self.assertIn('Project Overview We',job['catalog_summary'])
                    self.assertNotIn('OverviewWe',job['_public_source_text'])
                    self.assertEqual(catalog.build_catalog([job], {'location':'Brazil'})['result_count'],1)

    def test_remote_is_not_worldwide_and_absent_mode_stays_absent(self):
        self.job.update(source_location='Remote',rich_body='Role details are limited.')
        job = self.prepared()
        self.assertEqual(location_summary(job),'Remote')
        self.assertEqual(catalog.build_catalog([job],{'location':'Worldwide'})['result_count'],0)
        self.job['source_location'] = None
        job = self.prepared()
        self.assertFalse(location_summary(job))
        self.assertEqual(job['_catalog_location_model']['mode'],'unknown')

    def test_office_and_language_locale_do_not_supply_applicant_geography(self):
        self.job.update(source_location='Kuala Lumpur',source_title='Vietnamese (Malaysia)',
            rich_body='Our office in Malaysia supports Vietnamese users worldwide.',
            rich_metadata_json=json.dumps(dict(country='MY',offices=[{'location':'Malaysia'}])))
        job = self.prepared()
        self.assertFalse(location_summary(job))
        self.assertFalse(job['_catalog_location_model']['countries'])
        self.assertEqual(catalog.build_catalog([job],{'location':'Malaysia'})['result_count'],0)

    def test_unknown_mode_with_stated_country_does_not_become_remote_or_onsite(self):
        self.job['source_location']='Myanmar'
        job=self.prepared()
        self.assertEqual(location_summary(job),'Myanmar')
        self.assertEqual(job['_catalog_location_model']['mode'],'unknown')

    def test_worldwide_requires_role_evidence_and_restriction_wins(self):
        arrangement=self.job['enrichment']['attributes']['work_arrangement']
        arrangement.update(workplace_mode='remote',location_scope='remote_worldwide')
        self.job.update(source_location='Remote',rich_body='Our users are worldwide.')
        self.assertEqual(location_summary(self.prepared()),'Remote')
        self.job['source_location']='World Wide - Remote'
        self.assertEqual(location_summary(self.prepared()),'Remote · Worldwide')
        self.job['rich_body']='Candidates must be located in Canada.'
        job=self.prepared()
        self.assertEqual(location_summary(job),'Remote · Canada')
        self.assertEqual(catalog.build_catalog([job],{'location':'Brazil'})['result_count'],0)

    def test_scoped_or_manual_unknown_is_not_replaced_by_lever_metadata(self):
        self.retained('appen')
        self.job['overridden_fields']=['attributes.work_arrangement.workplace_mode']
        self.assertEqual(location_summary(self.prepared()),'Myanmar')
        self.job['source_location']='Remote'
        self.assertNotIn('Remote',location_summary(self.prepared()))

    def test_bare_company_location_is_not_a_mandatory_applicant_clause(self):
        self.retained('rws')
        self.job['rich_body']='About our company:\nBased in Malaysia.'
        job=self.prepared()
        self.assertEqual(location_summary(job),'Remote')
        self.assertEqual(catalog.build_catalog([job],{'location':'Malaysia'})['result_count'],0)

    def test_role_onsite_requirement_conflicting_with_remote_metadata_stays_unresolved(self):
        self.retained('appen')
        self.job['rich_body']='Engagement Type: Onsite Project Contractor\nFull-day onsite sessions, 9:00 AM to 6:00 PM.'
        job=self.prepared()
        self.assertEqual(location_summary(job),'Work mode needs confirmation · Myanmar')
        self.assertEqual(job['_catalog_location_model']['mode'],'unknown')

    def test_unsupported_exclusion_cannot_remain_worldwide(self):
        self.job.update(source_location='World Wide - Remote',rich_body='Candidates must not be located in Canada.')
        job=self.prepared()
        self.assertNotIn('Worldwide',location_summary(job))
        self.assertIn('Location requirements need confirmation',location_summary(job))
        self.assertEqual(catalog.build_catalog([job],{'location':'Canada'})['result_count'],0)

    def test_conflicting_manual_worldwide_is_not_rewritten_as_country_allowance(self):
        self.job['enrichment']['attributes']['work_arrangement'].update(workplace_mode='remote',location_scope='remote_worldwide')
        self.job.update(source_location='World Wide - Remote',rich_body='Candidates must be located in Canada.',
                        overridden_fields=['attributes.work_arrangement.location_scope'])
        job=self.prepared()
        self.assertEqual(job['enrichment']['attributes']['work_arrangement']['location_scope'],'remote_worldwide')
        self.assertFalse(job['_public_geography']['countries'])
        self.assertNotIn('Worldwide',location_summary(job))

    def test_accepted_country_constraint_overrides_public_worldwide_filter(self):
        self.retained('meridial')
        self.job['enrichment']['attributes']['work_arrangement']['eligible_countries']=['Canada']
        self.job['enrichment']['variant_facts'].append(dict(field_path='attributes.work_arrangement.eligible_countries',knowledge_state='known_value',value=['Canada']))
        job=self.prepared()
        self.assertEqual(location_summary(job),'Remote · Canada')
        self.assertEqual(catalog.build_catalog([job],{'location':'Brazil'})['result_count'],0)
        self.assertEqual(catalog.build_catalog([job],{'location':'Worldwide'})['result_count'],0)

    def test_scoped_empty_country_does_not_borrow_listing_location(self):
        self.job['source_location']='Malaysia'
        self.job['enrichment']['variant_facts']=[dict(field_path='attributes.work_arrangement.eligible_countries',knowledge_state='known_empty',value=[])]
        job=self.prepared()
        self.assertFalse(job['_public_geography']['countries'])
        self.assertEqual(catalog.build_catalog([job],{'location':'Malaysia'})['result_count'],0)

    def test_mercor_accepted_dimensions_are_not_replaced_by_older_generic_facts(self):
        self.job.update(company_slug='mercor',rich_provider='mercor',
            applicant_country_requirements=[dict(dimension='location',mode='allow',countries=['Canada'],unresolved=False)],
            applicant_geography_evidence=dict(job_id=self.job['job_id'],source_url=self.job['listing_url']))
        self.job['enrichment']['attributes']['work_arrangement']['eligible_countries']=['Brazil']
        self.job['enrichment']['variant_facts']=[dict(field_path='attributes.work_arrangement.eligible_countries',knowledge_state='known_value',value=['Brazil'])]
        job=self.prepared()
        self.assertEqual(job['_public_geography']['countries'],('Canada',))
        self.assertIn('Current location: Canada',location_summary(job))
        self.assertEqual(catalog.build_catalog([job],{'location':'Canada'})['result_count'],1)
        self.assertEqual(catalog.build_catalog([job],{'location':'Brazil'})['result_count'],0)

    def test_generic_context_is_omitted_with_no_empty_row(self):
        self.job.update(source_title='Operations Coordinator',rich_body='Our mission is to build AI for everyone.\n\nCoordinate calendars.')
        job=self.prepared()
        self.assertEqual(job['_public_kind'],'')
        card=catalog.render_job_card(job,return_to='/jobs')
        page=detail.render_public_job_page(job,public_origin='https://www.wahojobs.com',public_reader=True)
        self.assertNotIn("<p class='eyebrow'>",page)
        self.assertNotIn('Advertised opportunity',card+page)
        self.assertNotIn('AI training &amp; evaluation',card+page)

    def test_activity_needs_duties_not_provider_or_title_alone(self):
        for title in ('Social Media Evaluator','Security Expert'):
            self.job['source_title']=title
            self.assertEqual(contribution_context(self.job,'Our mission is to evaluate AI.\n\nDetails to follow.',None),('',''))
        self.job['source_title']='Social Media Evaluator'
        self.assertEqual(contribution_context(self.job,'Review and evaluate posts on various social media platforms.',None)[0], 'Social media evaluation')

    def test_lifecycle_labels_are_preserved_separately_from_activity(self):
        self.job.update(source_title='Security Expert Talent Network',rich_body='Use security expertise to probe, test, and harden AI systems.')
        self.assertEqual(self.prepared()['_public_kind'],'AI security testing · Talent network — future consideration')
        self.job.update(source_title='Data Contributor',rich_body='One-time payment: $60 one-time.',rich_metadata_json=json.dumps({'pay':'$60 one-time'}))
        self.assertEqual(self.prepared()['_public_kind'],'Paid data contribution · One-time participation')
        self.job.update(source_title='Reviewer',rich_body='This is a standing listing.',rich_metadata_json='{}')
        self.assertEqual(self.prepared()['_public_kind'],'Ongoing application opportunity')

    def test_inline_whitespace_is_preserved_without_inserting_spaces_in_words(self):
        self.assertEqual(source_body_paragraphs('&lt;p&gt;&lt;strong&gt;Project Overview&lt;/strong&gt;&lt;span&gt;&amp;nbsp;&lt;/span&gt;We review AI.&lt;/p&gt;', 'text/html'),['Project Overview We review AI.'])
        self.assertEqual(source_body_paragraphs('<p>co<strong>author</strong> work</p><script>unsafe</script><p>Next</p>', 'text/html'),['coauthor work','Next'])


if __name__ == '__main__': unittest.main()
