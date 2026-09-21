"""Fixture coverage for proof-bound Browse fixes; real snapshot audit is separate."""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
import sqlite3
import unittest

from wahojobs import public_job_page as detail, public_jobs_catalog as catalog
from wahojobs.opportunity_enrichment import blank_document, apply_override_value, make_variant_fact
from tests import test_public_jobs_catalog as fixtures
from tests.test_public_jobs_catalog import NOW, ORIGIN
from tests.test_public_job_page import JOB_PATH


class BrowseFilterCorrectionTests(unittest.TestCase):
    setUp = fixtures.PublicJobsCatalogTests.setUp
    load = fixtures.PublicJobsCatalogTests.load

    def role_evidence(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            evidence = detail.load_public_job_evidence(db, JOB_PATH)
        row = dict(evidence['rows'][0], source_title='Financial Analyst',
                   source_department='Finance', source_expertise='Finance')
        other = dict(row, job_id=9005, source_hash='other-source', source_title='Physicist',
                     source_department='Physics', source_expertise='Physics', source_location='Remote — Portugal')
        document = blank_document()
        for field, value in [('professional_domains', 'finance'), ('work_activities', 'research_analysis')]:
            document['attributes']['role'][field] = [value]
            document['field_evidence'].append(dict(field_path='attributes.role.' + field,
                basis='deterministic_classification', source_ref='role_text',
                evidence_text='Financial Analyst | Finance', confidence='medium',
                variant_refs=['source_hash:' + row['source_hash'], 'source_hash:other-source']))
        evidence['rows'] = [row, other]
        evidence['effective']['document'] = document
        return evidence

    def test_recover_only_exact_source_proof_and_preserve_variant_conjunctions(self):
        evidence = self.role_evidence()
        original = deepcopy(evidence)
        jobs = detail.prepare_public_job_variants(evidence, now=NOW)
        for job in jobs:
            catalog.prepare_catalog_presentation(job)
        self.assertEqual(jobs[0]['_catalog_filter_values']['field'], {'finance'})
        self.assertEqual(jobs[1]['_catalog_filter_values']['field'], set())
        self.assertEqual(catalog.build_catalog(jobs, {'field': 'Finance', 'location': 'Portugal'})['result_count'], 0)
        selected = detail.prepare_public_job(evidence, selected_job_id=9003, now=NOW)
        self.assertEqual(selected['enrichment']['attributes'], jobs[0]['enrichment']['attributes'])
        self.assertEqual(selected['enrichment']['field_evidence'][0]['variant_refs'], ['source_hash:' + evidence['rows'][0]['source_hash']])
        self.assertEqual(evidence, original)

    def test_aggregate_title_stale_reference_and_override_cannot_supply_fallback(self):
        for case in ('aggregate', 'stale_hash', 'changed_title', 'override_set', 'override_unknown'):
            with self.subTest(case=case):
                evidence = self.role_evidence()
                document = evidence['effective']['document']
                if case == 'aggregate':
                    for proof in document['field_evidence']:
                        proof['evidence_text'] = 'Financial Analyst | Physicist | Finance | Physics'
                elif case == 'stale_hash':
                    evidence['rows'][0]['source_hash'] = 'changed-source'
                elif case == 'changed_title':
                    evidence['rows'][0]['source_title'] = 'Changed title'
                else:
                    for field in ('professional_domains', 'work_activities'):
                        apply_override_value(document, 'attributes.role.' + field,
                            'set_unknown' if case == 'override_unknown' else 'set', ['legal'])
                selected = detail.prepare_public_job(evidence, selected_job_id=9003, now=NOW)
                self.assertEqual(selected['enrichment']['attributes']['role']['professional_domains'], [])
                self.assertEqual(selected['enrichment']['attributes']['role']['work_activities'], [])

    def test_explicit_empty_and_scoped_values_take_precedence_over_canonical_fallback(self):
        for empty in (False, True):
            evidence = self.role_evidence()
            document = evidence['effective']['document']
            reference = 'source_hash:' + evidence['rows'][0]['source_hash']
            document['variant_facts'] = [make_variant_fact('attributes.role.professional_domains',
                None if empty else 'biology', [reference], [], knowledge_state='known_empty' if empty else 'known_value')]
            result = detail.prepare_public_job(evidence, selected_job_id=9003, now=NOW)
            self.assertEqual(result['enrichment']['attributes']['role']['professional_domains'], [] if empty else ['biology'])

    def invitation_job(self):
        job = deepcopy(self.load()[0])
        identity = '11111111-1111-4111-8111-111111111111'
        url = 'https://www.alignerr.com/jobs/' + identity
        quote = "We're looking for transcriptionists based in Spain to transcribe audio."
        body = '# About the role\n\n' + quote
        job.update(company_slug='alignerr', rich_provider='alignerr', external_id=identity,
            rich_external_id=identity, listing_url=url, official_url=url, rich_source_url=url,
            rich_body=body, rich_body_format='text/markdown', source_location='Remote',
            enrichment=blank_document())
        metadata = dict(wahojobs_source_detail_v1=dict(version=1, provider='alignerr', external_id=identity,
            url=url, field='props.pageProps.job.longDescription', observed_at=NOW.isoformat(),
            record=dict(id=identity, longDescription=body, location='United States'),
            applicant_location_support=dict(version=1, body_sha256=sha256(body.encode()).hexdigest(), clauses=[dict(
                dimension='location', modality='invitation', mode='allow', countries=['Spain'],
                unresolved=False, source_field='props.pageProps.job.longDescription:line 3', source_quote=quote)])))
        job['rich_metadata_json'] = json.dumps(metadata)
        return job

    def test_invitation_is_positive_exact_variant_evidence_without_exclusivity(self):
        job = self.invitation_job()
        job['enrichment']['attributes']['requirements']['languages'] = [dict(language='Spanish', locale=None, requirement_mode='single')]
        catalog.prepare_catalog_presentation(job)
        other = deepcopy(job)
        other.update(job_id=9005, rich_metadata_json='{}')
        other['enrichment']['attributes']['requirements']['languages'][0]['language'] = 'Hindi'
        catalog.prepare_catalog_presentation(other)
        jobs = [dict(job, _catalog_variants=(job, other))]
        result = catalog.build_catalog(jobs, {'location': 'Spain'})
        self.assertEqual(result['result_count'], 1)
        self.assertEqual(result['jobs'][0]['job_id'], 9003)
        self.assertEqual(job['catalog_location'], 'Applicants in Spain mentioned')
        self.assertEqual(job['_catalog_location_model']['scope'], 'unknown')
        self.assertEqual(catalog.build_catalog(jobs, {'location': 'Spain', 'language': 'Hindi'})['result_count'], 0)
        self.assertEqual(catalog.build_catalog(jobs, {'location': 'Spain', 'language': 'Spanish'})['result_count'], 1)
        for location in ('United States', 'Worldwide', 'Remote'):
            self.assertEqual(catalog.build_catalog(jobs, {'location': location})['result_count'], 0)
        page = detail.render_public_job_page(job, public_origin=ORIGIN, catalog_return_to='/jobs?location=Spain')
        self.assertIn('Applicants in Spain mentioned', page)
        self.assertNotIn('Eligible in Spain', page)
        self.assertNotIn('>United States<', page)

    def test_invitation_rejects_bad_binding_conflicts_and_generic_page_tags(self):
        for case in ('listing_url', 'provider', 'external_id', 'body', 'quote', 'body_hash', 'conflict', 'restriction', 'missing_packet'):
            with self.subTest(case=case):
                job = self.invitation_job()
                metadata = json.loads(job['rich_metadata_json'])
                packet = metadata['wahojobs_source_detail_v1']['applicant_location_support']
                if case == 'listing_url': job['listing_url'] += '-changed'
                elif case == 'provider': job['rich_provider'] = 'unrelated'
                elif case == 'external_id': job['rich_external_id'] = 'unrelated'
                elif case == 'body': job['rich_body'] += '\nChanged'
                elif case == 'quote': packet['clauses'][0]['source_quote'] = 'Not present in this body'
                elif case == 'body_hash': packet['body_sha256'] = 'bad'
                elif case == 'conflict': packet['clauses'][0]['source_conflict'] = True
                elif case == 'restriction': packet['clauses'][0]['modality'] = 'mandatory'
                else: del metadata['wahojobs_source_detail_v1']['applicant_location_support']
                job['rich_metadata_json'] = json.dumps(metadata)
                catalog.prepare_catalog_presentation(job)
                self.assertEqual(job['_catalog_filter_values']['location'], set())
                self.assertIn('unconfirmed', job['catalog_location'])

    def test_umbrella_is_not_relabelled_as_a_specific_activity(self):
        job = self.invitation_job()
        job['enrichment']['attributes']['role']['work_activities'] = ['ai_training_evaluation', 'audio_speech']
        catalog.prepare_catalog_presentation(job)
        self.assertEqual(job['_catalog_filter_values']['work'], {'audio speech'})
        self.assertEqual(job['enrichment']['attributes']['role']['work_activities'], ['ai_training_evaluation', 'audio_speech'])

    def test_published_engagement_requires_exact_source_binding(self):
        job = self.invitation_job()
        job['source_commitment'] = None
        metadata = json.loads(job['rich_metadata_json'])
        metadata['wahojobs_source_detail_v1']['record']['jobType'] = 'CONTRACT'
        job['rich_metadata_json'] = json.dumps(metadata)
        catalog.prepare_catalog_presentation(job)
        self.assertEqual(job['_catalog_filter_values']['arrangement'], {'contract'})
        job['listing_url'] += '-changed'
        catalog.prepare_catalog_presentation(job)
        self.assertEqual(job['_catalog_filter_values']['arrangement'], set())


if __name__ == '__main__':
    unittest.main()
