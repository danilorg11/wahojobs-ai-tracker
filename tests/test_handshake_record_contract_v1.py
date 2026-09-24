"""Synthetic contract negatives. Real beta-host CMS bodies are replayed separately."""
from dataclasses import replace
import unittest
from unittest.mock import patch
from urllib.request import Request

from wahojobs.crawler.providers import handshake as h
from wahojobs.crawler.types import CompanyCrawlResult, ProviderOutcome
from wahojobs.source_capture import (
    SourceCaptureContext, prepare_record_promotion_attestation,
    prepare_source_capture_v1,
)
from wahojobs.daily_source_policy import (
    daily_source, default_sources, observed_handshake_assets, validate_request,
)


CHUNK = 'https://framerusercontent.com/cms/site/build/Opportunities-chunk-default-0.framercms'
SHA = 'a' * 64
RECORD = {
    h.FIELD_ID: 'cms-1',
    h.FIELD_SLUG: 'ai-evaluator',
    h.FIELD_TITLE: 'AI Evaluator',
    h.FIELD_SHOW_JOB: True,
    h.FIELD_WORK_LOCATION: 'Remote',
    h.FIELD_DESCRIPTION: 'Remote AI model evaluation.',
    h.FIELD_APPLICATION: (
        'https://app.joinhandshake.com/signup?'
        'destination_hai_path=%2Fauth&hai_job_id=123'
    ),
}


class HandshakeRecordContractV1Tests(unittest.TestCase):
    def candidate_and_context(self):
        candidate = h.parse_opportunity_record(RECORD, {}, {},
            chunk_url=CHUNK, chunk_sha256=SHA)
        result = CompanyCrawlResult(jobs=[candidate], used_sample_data=False,
            source_message='synthetic fixture', source_type='framer-public-inventory',
            outcome=ProviderOutcome.PARTIAL, raw_record_count=2,
            normalized_record_count=1, filtered_record_count=1,
            payload_shape='handshake_public_cms_record_v1',
            schema_fingerprint='handshake_public_cms_record_v1')
        return candidate, SourceCaptureContext.from_crawl_result(1, result)

    def validate(self, candidate, context):
        return prepare_record_promotion_attestation(candidate,
            prepare_source_capture_v1(candidate), context,
            provider='handshake', source_type='framer-public-inventory')

    def test_exact_cms_record_and_signup_binding(self):
        candidate, context = self.candidate_and_context()
        self.assertEqual(self.validate(candidate, context).contract_id,
                         'handshake_public_cms_record_v1')
        self.assertEqual(candidate.external_id, 'handshake::cms-1')
        self.assertIn('Remote AI model evaluation', candidate.source_body)
        self.assertFalse(candidate.include_in_live_market_estimate)
        for changed in (
            replace(candidate, title='Fabricated title'),
            replace(candidate, url='https://joinhandshake.com/ai/opportunities/other'),
            replace(candidate, source_body='Generic page'),
            replace(candidate, location='Worldwide'),
        ):
            with self.subTest(changed=changed.title):
                with self.assertRaises(ValueError):
                    self.validate(changed, context)
        metadata = dict(candidate.source_metadata, application_job_id='999')
        with self.assertRaises(ValueError):
            self.validate(replace(candidate, source_metadata=metadata), context)
        metadata = dict(candidate.source_metadata, cms_record={**RECORD, h.FIELD_SHOW_JOB: False})
        with self.assertRaises(ValueError):
            self.validate(replace(candidate, source_metadata=metadata), context)

    def test_generic_signup_and_non_ai_or_nonremote_records_are_excluded(self):
        self.assertFalse(h.qualified_public_record({**RECORD,
            h.FIELD_APPLICATION: 'https://app.joinhandshake.com/signup'}))
        self.assertFalse(h.qualified_public_record({**RECORD,
            h.FIELD_DESCRIPTION: 'Remote administrative work.'}))
        self.assertFalse(h.qualified_public_record({**RECORD,
            h.FIELD_WORK_LOCATION: 'New York'}))
        self.assertFalse(h.qualified_public_record({**RECORD,
            h.FIELD_SHOW_JOB: False}))
        self.assertFalse(h.qualified_public_record({**RECORD,
            h.FIELD_DESCRIPTION: 'AI evaluation. Must be based in Canada.'}))
        self.assertFalse(h.qualified_public_record({**RECORD,
            h.FIELD_DESCRIPTION: 'AI evaluation. Residents of the United States only.'}))
        self.assertFalse(h.qualified_public_record({**RECORD,
            h.FIELD_APPLICATION: RECORD[h.FIELD_APPLICATION] + '&next='}))
        self.assertFalse(default_sources()['handshake']['enabled'])

    def test_ambiguous_assets_and_unresolved_facets_fail_closed(self):
        for url in ('https://framerusercontent.com/sites/site/../other.mjs',
                    'https://framerusercontent.com/cms/site/../x-chunk-default-0.framercms'):
            with self.assertRaises(ValueError):
                h._validate_asset_url(url)
            with self.assertRaises(ValueError):
                with observed_handshake_assets((url,)):
                    pass
        with self.assertRaises(ValueError):
            h.resolve_labels(['degree-id'], {})
        with self.assertRaises(ValueError):
            h.resolve_labels('degree-id', {'degree-id': 'Bachelor'})

    def test_collection_name_selects_its_declared_chunks_not_module_filename(self):
        def declaration(prefix):
            return ('new URL(`./' + prefix + '-chunk-default-0.framercms`,'
                    '`https://framerusercontent.com/modules/site/build/' + prefix + '.js`)'
                    '.href.replace(`/modules/`,`/cms/`)')
        module = ('collectionByLocaleId:{default:new ht({chunks:[' + declaration('degree')
                  + '],id:`collection`,indexes:[],schema:Ct})},displayName:`Degree Filters`'
                  + 'collectionByLocaleId:{default:new ht({chunks:[' + declaration('other')
                  + '],id:`other`,indexes:[],schema:Ct})},displayName:`Other`')
        urls = h.extract_collection_chunk_urls(module,
            linked_module_url='https://framerusercontent.com/sites/site/other.hash.mjs',
            collection_name='Degree Filters')
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].endswith('/degree-chunk-default-0.framercms'))
        duplicated = declaration('degree') + declaration('degree')
        with self.assertRaises(ValueError):
            h.extract_collection_chunk_urls(duplicated)

    def test_hidden_cms_identity_collision_blocks_promotion(self):
        module = 'https://framerusercontent.com/sites/site/Opportunities.hash.mjs'
        chunk = CHUNK
        with (patch.object(h, 'fetch_text', return_value='page'),
              patch.object(h, 'extract_framer_module_urls', return_value=[module]),
              patch.object(h, 'discover_cms_urls', return_value={h.OPPORTUNITIES_COLLECTION:[chunk]}),
              patch.object(h, 'read_framercms_rows', return_value=[
                  (RECORD,chunk,SHA), ({**RECORD, h.FIELD_SHOW_JOB:False},chunk,SHA)]),
              patch.object(h, 'read_label_map', return_value=({}, {}))):
            with self.assertRaisesRegex(ValueError, 'duplicate identity'):
                h.fetch_handshake_jobs('https://joinhandshake.com/ai/opportunities')

    def test_missing_label_collection_holds_only_records_referencing_it(self):
        module = 'https://framerusercontent.com/sites/site/Opportunities.hash.mjs'
        restricted = {**RECORD, h.FIELD_ID:'cms-2', h.FIELD_SLUG:'ai-evaluator-two',
                      h.FIELD_DEGREE_FILTERS:['degree-1']}
        with (patch.object(h, 'fetch_text', return_value='page'),
              patch.object(h, 'extract_framer_module_urls', return_value=[module]),
              patch.object(h, 'discover_cms_urls', return_value={h.OPPORTUNITIES_COLLECTION:[CHUNK]}),
              patch.object(h, 'read_framercms_rows', return_value=[
                  (RECORD,CHUNK,SHA), (restricted,CHUNK,SHA)]),
              patch.object(h, 'read_label_map', return_value=({}, {}))):
            jobs, raw, rejected = h.fetch_handshake_jobs('https://joinhandshake.com/ai/opportunities')
        self.assertEqual((len(jobs), raw, rejected), (1, 2, 1))
        self.assertEqual(jobs[0].external_id, 'handshake::cms-1')

    def test_source_remains_partial_even_with_qualified_record(self):
        candidate, context = self.candidate_and_context()
        self.assertFalse(context.snapshot_complete)
        self.assertFalse(context.pagination_complete)
        self.assertEqual(self.validate(candidate, context).contract_id,
                         'handshake_public_cms_record_v1')
        complete = replace(context, provider_outcome='success',
                           snapshot_complete=True, pagination_complete=True)
        with self.assertRaises(ValueError):
            self.validate(candidate, complete)

    def test_referenced_degree_and_subject_conditions_are_attested(self):
        record = {**RECORD, h.FIELD_SUBJECT_FILTERS:['subject-1'],
                  h.FIELD_DEGREE_FILTERS:['degree-1']}
        subject = {'subject-1':dict(cms_record={h.FIELD_ID:'subject-1',
                   h.SUBJECT_TITLE_FIELD:'Computer Science'},
                   cms_chunk_url=CHUNK, cms_chunk_sha256=SHA)}
        degree = {'degree-1':dict(cms_record={h.FIELD_ID:'degree-1',
                  h.DEGREE_TITLE_FIELD:"Bachelor's"},
                  cms_chunk_url=CHUNK, cms_chunk_sha256=SHA)}
        candidate = h.parse_opportunity_record(record, {'subject-1':'Computer Science'},
            {'degree-1':"Bachelor's"}, chunk_url=CHUNK, chunk_sha256=SHA,
            subject_label_evidence=subject, degree_label_evidence=degree)
        base, context = self.candidate_and_context()
        self.assertEqual(self.validate(candidate, context).contract_id,
                         'handshake_public_cms_record_v1')
        self.assertEqual(candidate.department, 'Computer Science')
        self.assertIn("Bachelor's", candidate.commitment)
        with self.assertRaises(ValueError):
            self.validate(replace(candidate, commitment='Unknown'), context)
        metadata = dict(candidate.source_metadata,
                        degree_label_evidence={'degree-1':{**degree['degree-1'],
                            'cms_record':{h.FIELD_ID:'degree-1',h.DEGREE_TITLE_FIELD:'Doctorate'}}})
        with self.assertRaises(ValueError):
            self.validate(replace(candidate, source_metadata=metadata), context)

    def test_daily_network_scope_requires_observed_exact_assets(self):
        page = 'https://joinhandshake.com/ai/opportunities/'
        module = 'https://framerusercontent.com/sites/site/Opportunities.mjs'
        with daily_source('handshake'):
            validate_request(Request(page))
            with self.assertRaises(ValueError):
                validate_request(Request(CHUNK))
            with observed_handshake_assets((module,)):
                validate_request(Request(module))
                with self.assertRaises(ValueError):
                    validate_request(Request(CHUNK))
            with observed_handshake_assets((CHUNK,)):
                validate_request(Request(CHUNK))
                with self.assertRaises(ValueError):
                    validate_request(Request(CHUNK + '?next=1'))


if __name__ == '__main__':
    unittest.main()
