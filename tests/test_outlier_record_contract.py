"""Outlier contract tests; real captures stay in the operational evidence store."""

import json
import gc
import hashlib
import os
import tempfile
from datetime import datetime
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.request import Request

from wahojobs.crawler.companies.outlier import crawl_outlier, OUTLIER_API_URL
from wahojobs.crawler.providers import outlier
from wahojobs.crawler.types import (
    ProviderOutcome, evaluate_removal_authorization, crawl_run_status_for_result,
)
from wahojobs.source_capture import (
    SourceCaptureContext, prepare_record_promotion_attestation, prepare_source_capture,
    decide_source_promotion_v2, PROMOTION_DECISION_PROMOTED,
)
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import (
    create_crawl_run, finish_crawl_run, initialize_database, install_base_schema,
)
from wahojobs.tracking.service import track_crawl_result
from wahojobs.public_jobs_catalog import load_public_jobs
from wahojobs.daily_source_policy import daily_source, observed_outlier_details, validate_request


class OutlierRecordContractTests(unittest.TestCase):
    def setUp(self):
        location = os.environ.get('WAHOJOBS_OUTLIER_EVIDENCE_DIR')
        if not location:
            self.skipTest('Set WAHOJOBS_OUTLIER_EVIDENCE_DIR for the retained beta-host replay.')
        self.evidence = Path(location)
        self.index = (self.evidence / 'response-001.raw').read_text(encoding='utf-8')
        client = (self.evidence / 'response-003.raw').read_bytes()
        self.assertEqual(hashlib.sha256(client).hexdigest(), outlier.PUBLIC_PAGE_CHUNK_SHA256)
        self.details = {}
        for ordinal in (4, 6, 7, 8, 9, 10, 11, 12):
            record = json.loads((self.evidence / f'response-{ordinal:03}.raw').read_bytes())
            self.details[record['id']] = record
        self.assertEqual(len(self.details), 8)

    def _crawl(self):
        def read(request):
            if request.full_url == OUTLIER_API_URL:
                return self.index
            identity = int(request.full_url.rsplit('/', 1)[-1])
            return json.dumps(self.details[identity], ensure_ascii=False)
        with patch.object(outlier, '_read_json', side_effect=read):
            return crawl_outlier(OUTLIER_API_URL)

    def test_real_capture_individual_promotion_and_no_absence_closure(self):
        result = self._crawl()
        self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
        self.assertEqual((result.raw_record_count, len(result.jobs)), (8, 8))
        self.assertEqual(sum(j.opportunity_kind == 'evergreen_application' for j in result.jobs), 2)
        self.assertFalse(evaluate_removal_authorization(result).authorized)
        context = SourceCaptureContext.from_crawl_result(1, result)
        for candidate in result.jobs:
            prepared = prepare_source_capture(candidate)
            attested = prepare_record_promotion_attestation(
                candidate, prepared, context, provider='outlier', source_type=result.source_type)
            decision = decide_source_promotion_v2(
                prepared, context, None, record_attestation=attested)
            self.assertEqual(decision.decision, PROMOTION_DECISION_PROMOTED)
            self.assertEqual(candidate.source_metadata['allowed_countries'],
                             self.details[int(candidate.external_id)]['allowedCountries'])
            index_row = candidate.source_metadata['index_row']
            self.assertEqual(candidate.url, index_row['absolute_url'])
            self.assertEqual(candidate.source_metadata['source_location_label'],
                             index_row['location']['name'])
            self.assertEqual(candidate.location, 'Remote - ' + ', '.join(
                index_row['allowedCountries']))

    def test_real_capture_rejects_changed_identity_content_and_signup(self):
        result = self._crawl()
        candidate = result.jobs[0]
        context = SourceCaptureContext.from_crawl_result(1, result)
        for mutation in (
            lambda d: d.update(id=999),
            lambda d: d.update(signupFlowId=None),
            lambda d: d.update(content='<h1>Generic opportunity shell</h1>'),
            lambda d: d.update(allowedCountries=['United States']),
        ):
            with self.subTest(mutation=mutation.__code__.co_firstlineno):
                detail = dict(candidate.source_metadata['detail_record'])
                mutation(detail)
                metadata = dict(candidate.source_metadata, detail_record=detail)
                altered = replace(candidate, source_metadata=metadata)
                with self.assertRaises(ValueError):
                    prepare_record_promotion_attestation(
                        altered, prepare_source_capture(altered), context,
                        provider='outlier', source_type=result.source_type)
        index_row = dict(candidate.source_metadata['index_row'],
                         title='Android AI Evaluator - Unreviewed Language')
        detail = dict(candidate.source_metadata['detail_record'], title=index_row['title'])
        with self.assertRaises(ValueError):
            outlier.qualify_index_detail(index_row, detail, self.index)

    def test_index_empty_and_generic_page_are_not_inventory(self):
        with self.assertRaises(ValueError):
            outlier.parse_index('{"jobs":[],"totalCount":0,"page":1,"totalPages":1}')
        with self.assertRaises(ValueError):
            outlier.parse_index('<html>Opportunity</html>')
        with self.assertRaises(ValueError):
            outlier.parse_index('{"jobs":[],"error":"access denied"}')

    def test_real_capture_to_isolated_catalog_and_repeat(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = get_connection(Path(temp) / 'outlier.sqlite')
            try:
                install_base_schema(conn)
                company_id = conn.execute("""INSERT INTO companies
                    (name, slug, careers_url, source_tier, inventory_model, market_count_policy)
                    VALUES ('Outlier', 'outlier', ?, 'core', 'mixed', 'report_separately')""",
                    ('https://app.outlier.ai/opportunities',)).lastrowid
                result = self._crawl()
                # Repeat the same captured instant; do not fabricate a later refresh.
                for instant in ('2026-09-24T23:14:40+00:00',) * 2:
                    run_id = create_crawl_run(conn, company_id, instant)
                    summary = track_crawl_result(conn, company_id, run_id, result, instant,
                                                 model_enrichment=False)
                    self.assertEqual(summary.jobs_removed, 0)
                    self.assertFalse(summary.removals_authorized)
                    self.assertEqual(conn.execute(
                        'SELECT COUNT(*) FROM jobs WHERE company_id=?', (company_id,)).fetchone()[0], 8)
                    finish_crawl_run(conn, run_id, summary, instant,
                        status=crawl_run_status_for_result(result, evaluate_removal_authorization(result)))
                    conn.commit()
                # Negative fixture: only one observed record in a partial
                # response. It is not another employer capture or freshness renewal.
                partial = replace(result, jobs=result.jobs[:1], normalized_record_count=1,
                                  filtered_record_count=7)
                run_id = create_crawl_run(conn, company_id, '2026-09-24T23:14:40+00:00')
                summary = track_crawl_result(conn, company_id, run_id, partial,
                    '2026-09-24T23:14:40+00:00', model_enrichment=False)
                self.assertEqual(summary.jobs_removed, 0)
                self.assertEqual(conn.execute(
                    'SELECT COUNT(*) FROM jobs WHERE company_id=? AND is_active=1',
                    (company_id,)).fetchone()[0], 8)
                finish_crawl_run(conn, run_id, summary, '2026-09-24T23:14:40+00:00',
                    status=crawl_run_status_for_result(partial, evaluate_removal_authorization(partial)))
                conn.commit()
                catalog = load_public_jobs(conn, now=datetime.fromisoformat('2026-09-24T23:14:40+00:00'))
                self.assertEqual(sum(item['company_slug'] == 'outlier' for item in catalog), 8)
                self.assertEqual(sum(item['opportunity_kind'] == 'evergreen_application'
                                     for item in catalog), 2)
            finally:
                conn.close()


class OutlierRequestBoundaryTests(unittest.TestCase):
    def test_initialized_company_keeps_collector_endpoint_and_mixed_model(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'seed.sqlite'
            initialize_database(path)
            conn = get_connection(path)
            try:
                row = conn.execute("SELECT careers_url, inventory_model, market_count_policy "
                                   "FROM companies WHERE slug='outlier'").fetchone()
                self.assertEqual(tuple(row), (OUTLIER_API_URL, 'mixed', 'report_separately'))
            finally:
                conn.close()
                gc.collect()  # initialize_database's context releases SQLite on GC on Windows

    def test_only_exact_index_and_observed_details_allowed(self):
        detail = outlier.DETAIL_PREFIX + '4729398005'
        with self.assertRaises(ValueError):
            with observed_outlier_details((outlier.DETAIL_PREFIX + '9999',)):
                pass
        with daily_source('outlier'), observed_outlier_details((detail,)):
            validate_request(Request(outlier.INDEX_URL, data=b'{}', method='POST'))
            validate_request(Request(detail))
            for request in (
                Request(outlier.DETAIL_PREFIX + '9999'),
                Request('https://other.example/internal/experts/job-board/jobs/4729398005'),
                Request(detail + '?debug=1'),
                Request(outlier.INDEX_URL, data=b'{"page":2}', method='POST'),
            ):
                with self.subTest(url=request.full_url), self.assertRaises(ValueError):
                    validate_request(request)


if __name__ == '__main__':
    unittest.main()
