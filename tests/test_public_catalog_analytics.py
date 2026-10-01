"""Anonymous renderer boundaries plus the actual consent bootstrap in Node."""
from pathlib import Path
import json
import shutil
import subprocess
import unittest

from tests import test_public_catalog_reader as reader_tests
from wahojobs import public_catalog_analytics as analytics
from wahojobs import public_jobs_catalog as catalog, public_job_page as detail


class PublicCatalogAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = reader_tests.PublicCatalogReaderTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_public_documents_include_one_bootstrap_without_request_values(self):
        for path in ('/jobs', '/jobs?q=Python', '/jobs/opportunity-9002',
                     '/jobs/opportunity-9002?variant=9003&return_to=%2Fjobs%3Fq%3DPython'):
            with self.subTest(path=path):
                response = self.fixture.get(path)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.body.decode().count(analytics.HEAD), 1)
                self.assertEqual(response.body.decode().count('id="wahojobs-public-analytics"'), 1)
                csp = dict(response.headers)['Content-Security-Policy']
                self.assertIn(analytics.CSP, csp)
                self.assertIn("connect-src 'self'", csp)
                self.assertNotIn('*.google', csp)
                self.assertNotIn('unsafe-eval', csp)

    def test_private_legacy_errors_and_sitemap_have_no_bootstrap(self):
        pages = (catalog.render_public_jobs_page(catalog.build_catalog(self.fixture.jobs, {}),
                    public_origin='https://beta.wahojobs.com'),
                 detail.render_public_job_page(self.fixture.jobs[0],
                    public_origin='https://beta.wahojobs.com', authenticated=True))
        for page in pages:
            self.assertNotIn(analytics.MEASUREMENT_ID, page)
        for path in ('/jobs/sitemap.xml', '/jobs/opportunity-999999', '/jobs?invalid=x',
                     '/login', '/candidate/auth/callback?code=fake'):
            self.assertNotIn(analytics.HEAD.encode(), self.fixture.get(path).body)

    def test_empty_measurement_document_has_no_personal_content_or_query(self):
        response = self.fixture.get(analytics.FRAME_PATH)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body.decode(), analytics.FRAME_DOCUMENT)
        self.assertEqual(dict(response.headers)['Content-Security-Policy'], analytics.FRAME_CSP)
        self.assertEqual(dict(response.headers)['X-Robots-Tag'], 'noindex,follow')
        self.assertNotIn(b'<form', response.body)
        self.assertNotIn(b'Acme', response.body)
        self.assertNotIn(b'candidate', response.body)
        self.assertEqual(self.fixture.get(analytics.FRAME_PATH + '?variant=9003').status, 400)
        self.assertEqual(self.fixture.reader.handle('POST', analytics.FRAME_PATH).status, 405)

    def test_consent_and_event_payload_client_contract(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node is required for the catalog JavaScript contract')
        run = subprocess.run([node, '--preserve-symlinks-main', str(Path(__file__).parent / 'client_dom' /
                              'public_catalog_analytics.test.cjs')], input=json.dumps({
                                  'bootstrap': analytics.SCRIPT, 'frame': analytics.FRAME_DOCUMENT}),
                             text=True, capture_output=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_candidate_controls_are_never_injected_into_measurement_document(self):
        reader = self.fixture.make_reader(candidate_enabled=True)
        frame = self.fixture.get(analytics.FRAME_PATH, reader=reader)
        self.assertEqual(frame.body.decode(), analytics.FRAME_DOCUMENT)
        public = self.fixture.get('/jobs', reader=reader)
        self.assertIn(b"href='/my-jobs'", public.body)
        self.assertIn(analytics.HEAD.encode(), public.body)


if __name__ == '__main__':
    unittest.main()
