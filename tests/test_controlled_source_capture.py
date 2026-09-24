"""Controlled DA/DF observation scope; no network requests."""
import unittest
from types import SimpleNamespace
from urllib.request import Request
from unittest.mock import patch

from wahojobs import daily_source_policy as policy
from wahojobs.crawler.providers import dataannotation as da


class ControlledObservationScope(unittest.TestCase):
    def test_only_two_blocked_sources_are_observable_without_activation(self):
        for source in ("dataannotation", "dataforce"):
            with self.assertRaises(ValueError):
                with policy.daily_source(source):
                    pass
            self.assertNotIn(source, policy.READY_SOURCES)
        with self.assertRaises(ValueError):
            with policy.controlled_validation_source("handshake"):
                pass

    def test_dataannotation_exact_fixed_and_canonical_paths(self):
        with policy.controlled_validation_source("dataannotation"):
            for path in ("/coding", "/job-board/software-engineer", "/biology"):
                policy.validate_request(Request("https://www.dataannotation.tech"+path))
            for url in ("https://www.dataannotation.tech/job-board/other",
                        "https://dataannotation.tech/coding",
                        "https://www.dataannotation.tech/coding?next=1",
                        "http://www.dataannotation.tech/coding"):
                with self.assertRaises(ValueError):
                    policy.validate_request(Request(url))
        self.assertEqual(policy.POLICY["dataannotation"]["http_max"],11)

    def test_observed_dataannotation_redirect_is_one_exact_scoped_destination(self):
        target = "https://www.dataannotation.tech/job-board/generalist"
        with policy.controlled_validation_source("dataannotation"):
            with policy.observed_dataannotation_redirect("https://www.dataannotation.tech/generalist", target):
                policy.validate_request(Request(target))
                with self.assertRaises(ValueError):
                    policy.validate_request(Request("https://www.dataannotation.tech/job-board/other"))
            with self.assertRaises(ValueError):
                policy.validate_request(Request(target))
            for destination in ("https://evil.example/job-board/law-expert",
                                "https://www.dataannotation.tech/job-board/law-expert",
                                "https://www.dataannotation.tech/job-board/law-expert?next=1",
                                "https://www.dataannotation.tech/jobs/law-expert"):
                with self.assertRaises(ValueError):
                    with policy.observed_dataannotation_redirect(
                            "https://www.dataannotation.tech/law", destination):
                        pass

    def test_dataforce_exact_ordered_page_scope(self):
        with policy.controlled_validation_source("dataforce"):
            for url in ("https://dataforcecommunity.transperfect.com/projects",
                        "https://dataforcecommunity.transperfect.com/projects?project_type=All&page=1",
                        "https://dataforcecommunity.transperfect.com/projects?project_type=All&page=19"):
                policy.validate_request(Request(url))
            for url in ("https://dataforcecommunity.transperfect.com/projects?page=20&project_type=All",
                        "https://dataforcecommunity.transperfect.com/projects?page=1",
                        "https://dataforcecommunity.transperfect.com/studies",
                        "https://evil.example/projects"):
                with self.assertRaises(ValueError):
                    policy.validate_request(Request(url))

    def test_dataforce_detail_scope_requires_exact_observed_index_urls(self):
        url = "https://dataforcecommunity.transperfect.com/project/thyme-example"
        with policy.controlled_validation_source("dataforce"):
            with policy.observed_dataforce_details((url,)):
                policy.validate_request(Request(url))
                with self.assertRaises(ValueError):
                    policy.validate_request(Request(url + "?next=1"))
            with self.assertRaises(ValueError):
                policy.validate_request(Request(url))
            for bad in ("https://evil.example/project/thyme-example",
                        "https://dataforcecommunity.transperfect.com/admin/thyme-example",
                        "https://dataforcecommunity.transperfect.com/project/../admin"):
                with self.assertRaises(ValueError):
                    with policy.observed_dataforce_details((bad,)):
                        pass

    def test_rejected_dataannotation_route_does_not_skip_independent_fixed_page(self):
        pages = (da.DataAnnotationDomain('generalist', 'Generalist', 'Generalist'),
                 da.DataAnnotationDomain('coding', 'Coding', 'Coding'))
        observed = []
        def fetch(url):
            observed.append(url)
            if url.endswith('/generalist'):
                raise ValueError('unsupported canonical route')
            return {'ok': True, 'text': 'coding role application', 'url': url}
        with (patch.object(da, 'DOMAIN_PAGES', pages),
              patch.object(da, 'fetch_page', side_effect=fetch),
              patch.object(da, 'has_application_surface', return_value=True),
              patch.object(da, 'parse_domain_page', return_value=SimpleNamespace(external_id='dataannotation::coding'))):
            jobs, skipped = da.fetch_dataannotation_jobs('https://www.dataannotation.tech')
        self.assertEqual([job.external_id for job in jobs], ['dataannotation::coding'])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(observed, ['https://www.dataannotation.tech/generalist',
                                    'https://www.dataannotation.tech/coding'])

    def test_controlled_capture_cannot_enter_generic_publication(self):
        from wahojobs.crawler import pipeline
        observation = SimpleNamespace(controlled_validation=True)
        with (patch.object(pipeline, 'assert_production_dispatch_allowed', return_value=None),
              patch('wahojobs.crawler.staged_observation.validate_observation', return_value=observation)):
            with self.assertRaisesRegex(ValueError, 'controlled_validation_requires_explicit'):
                pipeline.run_crawl('dataannotation', db_path='isolated.sqlite3',
                                   ownership=object(), observation=observation)

    def test_unsupported_role_body_does_not_skip_later_fixed_page(self):
        pages = (da.DataAnnotationDomain('coding', 'Coding', 'Coding'),
                 da.DataAnnotationDomain('generalist', 'Generalist', 'Generalist'))
        seen = []
        def fetch(url):
            seen.append(url)
            return {'ok': True, 'text': 'generic', 'url': url}
        with (patch.object(da, 'DOMAIN_PAGES', pages),
              patch.object(da, 'fetch_page', side_effect=fetch),
              patch.object(da, 'has_application_surface', return_value=True),
              patch.object(da, 'parse_domain_page', side_effect=ValueError('role link mismatch'))):
            jobs, skipped = da.fetch_dataannotation_jobs('https://www.dataannotation.tech')
        self.assertEqual(jobs, [])
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(skipped), 2)


if __name__ == "__main__":
    unittest.main()
