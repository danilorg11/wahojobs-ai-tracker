"""Synthetic contract negatives; beta-host responses are replayed separately."""
from dataclasses import replace
import unittest
from urllib.request import Request

from wahojobs.crawler.providers.surge import extract_workforce_records, parse_workforce_detail
from wahojobs.crawler.types import CompanyCrawlResult, ProviderOutcome
from wahojobs.daily_source_policy import (
    daily_source, default_sources, observed_surge_details, validate_request,
)
from wahojobs.matching.opportunity_trust import (
    TRUSTED, UNVERIFIED_SOURCE, assess_opportunity_trust,
)
from wahojobs.source_capture import (
    SourceCaptureContext, prepare_record_promotion_attestation,
    prepare_source_capture_v1,
)


URL = 'https://surgehq.ai/workforce/ai-specialist'
DETAIL = (
    f'<link href="{URL}" rel="canonical">'
    '<h1 data-job="title">AI Specialist</h1>'
    '<a href="https://surgehq.ai/apply/ai-specialist">Apply</a>'
)
INDEX = (
    '<div data-slug="ai-specialist" role="listitem">'
    f'<a href="{URL}">AI Specialist Remote</a>'
    '<span data-job="title">AI Specialist</span>'
    '<span data-job="the-role">Evaluate AI models</span>'
    '</div>'
)
RECORD = extract_workforce_records(INDEX, 'https://surgehq.ai/workforce')[0]


class SurgeRecordContractV1Tests(unittest.TestCase):
    def test_public_inventory_requires_exact_accepted_record(self):
        row = dict(company_slug='surge', job_is_active=True,
            canonical_is_active=True, inventory_model='public_inventory',
            market_count_policy='report_separately', source_run_id=17,
            source_run_qualifies=False)
        self.assertEqual(assess_opportunity_trust(row, 'eligible').status,
                         UNVERIFIED_SOURCE)
        row['source_run_qualifies'] = True
        self.assertEqual(assess_opportunity_trust(row, 'eligible').status, TRUSTED)
        row['company_slug'] = 'handshake'
        row['source_run_qualifies'] = False
        self.assertEqual(assess_opportunity_trust(row, 'eligible').status,
                         UNVERIFIED_SOURCE)

    def candidate_and_context(self):
        job = parse_workforce_detail(RECORD, DETAIL, index_page_html=INDEX)
        result = CompanyCrawlResult(jobs=[job], used_sample_data=False,
            source_message='fixture', source_type='public-worker-pages',
            outcome=ProviderOutcome.PARTIAL, raw_record_count=2,
            normalized_record_count=1,
            payload_shape='surge_remote_workforce_record_v1',
            schema_fingerprint='surge_remote_workforce_record_v1')
        return job, SourceCaptureContext.from_crawl_result(1, result)

    def validate(self, job, context):
        return prepare_record_promotion_attestation(job,
            prepare_source_capture_v1(job), context,
            provider='surge', source_type='public-worker-pages')

    def test_exact_index_and_detail_attestation(self):
        job, context = self.candidate_and_context()
        self.assertEqual(self.validate(job, context).contract_id,
                         'surge_remote_workforce_record_v1')
        self.assertEqual(job.location, 'Remote')
        self.assertFalse(job.include_in_live_market_estimate)
        with self.assertRaises(ValueError):
            self.validate(replace(job, title='Fabricated role'), context)
        metadata = dict(job.source_metadata, detail_page_html=DETAIL.replace('Apply', 'Visit'))
        with self.assertRaises(ValueError):
            self.validate(replace(job, source_metadata=metadata), context)
        metadata = dict(job.source_metadata, index_page_html=INDEX.replace('Remote', 'Onsite'))
        with self.assertRaises(ValueError):
            self.validate(replace(job, source_metadata=metadata), context)

    def test_generic_or_nonremote_page_cannot_promote(self):
        with self.assertRaises(RuntimeError):
            parse_workforce_detail(RECORD, '<h1 data-job="title">AI Specialist</h1>')
        with self.assertRaises(RuntimeError):
            parse_workforce_detail(replace(RECORD, index_text='AI Specialist'), DETAIL)
        with self.assertRaises(RuntimeError):
            parse_workforce_detail(RECORD, DETAIL.replace('ai-specialist" rel', 'other" rel'))

    def test_daily_scope_is_disabled_and_exact_index_linked(self):
        self.assertFalse(default_sources()['surge']['enabled'])
        with daily_source('surge'):
            validate_request(Request('https://surgehq.ai/workforce'))
            with self.assertRaises(ValueError):
                validate_request(Request(URL))
            with observed_surge_details((URL,)):
                validate_request(Request(URL))
                with self.assertRaises(ValueError):
                    validate_request(Request('https://surgehq.ai/workforce/other'))


if __name__ == '__main__':
    unittest.main()
