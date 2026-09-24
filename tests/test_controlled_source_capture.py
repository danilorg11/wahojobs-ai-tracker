"""Controlled DA/DF observation scope; no network requests."""
import unittest
from urllib.request import Request

from wahojobs import daily_source_policy as policy


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


if __name__ == "__main__":
    unittest.main()
