"""Public employer links cannot disguise credentials or browser host parsing."""
import unittest
from scripts.local_product_app import safe_job_url


class BetaExternalLinkTests(unittest.TestCase):
    def test_credential_and_backslash_host_ambiguity_never_becomes_link(self):
        for value in (
            'https://trusted.example@evil.example/job',
            'https://user:password@jobs.example.test/job',
            'https://@jobs.example.test/job',
            'https://trusted.example\\@evil.example/job',
            'https:\\jobs.example.test\\job',
            'https://jobs.example.test/job\\path',
        ):
            with self.subTest(value=value):
                self.assertIsNone(safe_job_url(value))

    def test_existing_unsafe_schemes_controls_and_malformed_authorities_stay_rejected(self):
        for value in (
            'javascript:alert(1)', 'data:text/html,hello', '//jobs.example.test/job',
            ' https://jobs.example.test/job', 'https://jobs.example.test/job\n',
            'https://jobs.example.test/\x00job', 'https://jobs.example.test/\tjob',
            'https://jobs.example.test:bad/job', 'https://jobs .example.test/job',
            'https://[invalid/job', '', None,
        ):
            with self.subTest(value=value):
                self.assertIsNone(safe_job_url(value))

    def test_normal_exact_source_http_https_paths_queries_and_fragments_are_preserved(self):
        for value in (
            'https://www.alignerr.com/jobs/2037',
            'https://jobs.example.test/portugu%C3%AAs?source=wahojobs&job=123#details',
            'http://jobs.example.test:8080/job/123',
            'https://jobs.example.test/jobs/evaluation',
        ):
            with self.subTest(value=value):
                self.assertEqual(safe_job_url(value), value)


if __name__ == '__main__':
    unittest.main()
