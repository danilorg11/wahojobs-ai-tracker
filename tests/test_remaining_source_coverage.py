"""Isolated contract fixtures. These are not retained employer responses."""
import unittest
from unittest.mock import patch

from wahojobs.crawler.companies import outlier as outlier_company
from wahojobs.crawler.providers import dataannotation, dataforce, handshake, outlier, surge


class RemainingSourceBoundaries(unittest.TestCase):
    def test_dataannotation_canonical_redirect_scope_and_evidence(self):
        start = "https://www.dataannotation.tech/coding"
        self.assertTrue(dataannotation._allowed_destination(
            start, "https://www.dataannotation.tech/job-board/software-engineer"))
        self.assertTrue(dataannotation._allowed_destination(
            "https://www.dataannotation.tech/law",
            "https://www.dataannotation.tech/job-board/legal-expert"))
        for target in ("https://evil.example/job-board/software-engineer",
                       "https://www.dataannotation.tech/job-board/other",
                       "https://www.dataannotation.tech/job-board/software-engineer?next=1"):
            self.assertFalse(dataannotation._allowed_destination(start, target))
        self.assertFalse(dataannotation._allowed_destination(
            "https://www.dataannotation.tech/law",
            "https://www.dataannotation.tech/job-board/law-expert", observed=True))
        self.assertFalse(dataannotation.has_application_surface(
            "<title>403 Forbidden</title> Apply DataAnnotation coding app.dataannotation.tech", "coding"))
        self.assertTrue(dataannotation.has_application_surface(
            "<h1>Coding expert</h1> Apply at https://app.dataannotation.tech DataAnnotation", "coding"))

    def test_dataforce_requires_recognizable_inventory_and_terminal_state(self):
        page = '<div class="view view-projects"><div class="view-empty">No projects</div></div>'
        dataforce.validate_inventory_page(page)
        for invalid in ("<html>200 OK</html>", "<h1>Access denied</h1>"+page,
                        '<div class="view view-projects"></div>'):
            with self.assertRaises(ValueError):
                dataforce.validate_inventory_page(invalid)

    def test_handshake_chunk_scope_and_coverage(self):
        module = ('new URL(`./Opportunities-chunk-default-0.framercms`,'
                  '`https://framerusercontent.com/modules/site/hash/file.js`)'
                  '.href.replace(`/modules/`,`/cms/`)'
                  'new URL(`./Opportunities-chunk-default-1.framercms`,'
                  '`https://framerusercontent.com/modules/site/hash/file.js`)'
                  '.href.replace(`/modules/`,`/cms/`)')
        self.assertEqual(len(handshake.extract_collection_chunk_urls(module)), 2)
        self.assertEqual(len(handshake.extract_collection_chunk_urls(
            module, linked_module_url="https://framerusercontent.com/sites/site/file.hash.mjs")), 2)
        with self.assertRaises(ValueError):
            handshake.extract_collection_chunk_urls(
                module, linked_module_url="https://framerusercontent.com/sites/other/other.hash.mjs")
        with self.assertRaises(ValueError):
            handshake.extract_collection_chunk_urls(module.replace("-default-1", "-default-2"))
        with self.assertRaises(ValueError):
            handshake.extract_collection_chunk_urls(module.replace(
                "https://framerusercontent.com/modules/", "https://evil.example/modules/"))
        self.assertFalse(handshake.should_include_record({
            handshake.FIELD_ID: "1", handshake.FIELD_TITLE: "Role",
            handshake.FIELD_SLUG: "../escape", handshake.FIELD_SHOW_JOB: True}))

    def test_outlier_no_sample_on_failure_or_empty(self):
        with patch.object(outlier_company, "fetch_outlier_jobs", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                outlier_company.crawl_outlier(outlier_company.OUTLIER_API_URL)
        with patch.object(outlier_company, "fetch_outlier_jobs", return_value=[]):
            result = outlier_company.crawl_outlier(outlier_company.OUTLIER_API_URL)
        self.assertFalse(result.jobs)
        self.assertFalse(result.used_sample_data)
        self.assertFalse(result.snapshot_complete)
        self.assertFalse(outlier.should_include_job({"id": "1", "title": "Role"}))
        self.assertFalse(outlier.should_include_job({
            "id": "1", "title": "Role", "isPublic": True,
            "absolute_url": "https://example.test/en/expert/opportunities/1"}))
        fixture = {"id": "1", "title": "Role", "isPublic": True,
                   "absolute_url": "https://app.outlier.ai/en/expert/opportunities/1"}
        self.assertTrue(outlier.should_include_job(fixture))
        candidate = outlier.parse_outlier_job(fixture)
        self.assertEqual(candidate.location, "Unknown")
        self.assertFalse(candidate.include_in_live_market_estimate)

    def test_surge_generic_detail_and_fellowship_are_rejected(self):
        record = surge.WorkforceRecord("specialist", "https://surgehq.ai/workforce/specialist",
                                       {}, "")
        with self.assertRaises(RuntimeError):
            surge.parse_workforce_detail(record, "<h1>Surge workforce</h1>")
        with self.assertRaises(RuntimeError):
            surge.parse_workforce_detail(record, '<h1 data-job="title">Specialist</h1>')
        detail = ('<h1 data-job="title">Specialist</h1>'
                  '<a href="https://surgehq.ai/apply/specialist">Apply</a>')
        job = surge.parse_workforce_detail(record, detail)
        self.assertEqual(job.title, "Specialist")
        self.assertFalse(job.include_in_live_market_estimate)
        with self.assertRaises(RuntimeError):
            surge.parse_fellowship_page("https://surgehq.ai/fellowship",
                "Surge research fellowship researchfellows@surgehq.ai")
        fellowship = surge.parse_fellowship_page(
            "https://surgehq.ai/fellowship",
            '<h1>Research Fellowship</h1><a href="mailto:researchfellows@surgehq.ai">Apply: researchfellows@surgehq.ai</a>')
        self.assertEqual(fellowship.title, "Research Fellowship")
        self.assertFalse(fellowship.include_in_live_market_estimate)


if __name__ == "__main__":
    unittest.main()
