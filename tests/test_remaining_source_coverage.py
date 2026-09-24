"""Isolated contract fixtures. These are not retained employer responses."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wahojobs.crawler.companies import outlier as outlier_company
from wahojobs.crawler.companies import handshake as handshake_company
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

    def test_dataforce_daily_rotation_stays_in_index_and_does_not_starve_known_roles(self):
        known = [SimpleNamespace(url='https://dataforcecommunity.transperfect.com'+path,
                    title='Thyme', commitment='Remote')
                 for path in sorted(dataforce.QUALIFIED_DETAIL_PATHS)]
        backlog = [SimpleNamespace(
            url=f'https://dataforcecommunity.transperfect.com/project/other-{i}',
            title=f'Other {i}', commitment='Remote') for i in range(12)]
        excluded = SimpleNamespace(
            url='https://dataforcecommunity.transperfect.com/study/onsite',
            title='Onsite collection', commitment='On Site')
        jobs = known + backlog + [excluded]
        days = [dataforce.select_daily_detail_pages(jobs, 12, 739883+i)
                for i in range(3)]
        self.assertTrue(all(len(day) == 12 and day[:8] == known for day in days))
        self.assertEqual(set(map(id, sum((day[8:] for day in days), []))),
                         set(map(id, backlog)))
        reduced = [dataforce.select_daily_detail_pages(jobs, 7, 739883+i)
                   for i in range(8)]
        self.assertEqual(set(map(id, sum(reduced, []))), set(map(id, known)))
        self.assertEqual(dataforce.select_daily_detail_pages(jobs, 0, 739883), [])

    def test_dataforce_failed_exploratory_detail_keeps_known_records(self):
        known = [SimpleNamespace(url='https://dataforcecommunity.transperfect.com'+path,
                    title='Thyme', commitment='Remote')
                 for path in sorted(dataforce.QUALIFIED_DETAIL_PATHS)]
        extra = SimpleNamespace(
            url='https://dataforcecommunity.transperfect.com/project/new-remote',
            title='New remote', commitment='Remote')
        def fetch(url):
            if url == extra.url:
                raise TimeoutError('inspection-only timeout')
            return '<html>known detail</html>'
        with patch.object(dataforce, 'remaining_http_requests', return_value=9), \
                patch.object(dataforce, 'fetch_page', side_effect=fetch), \
                patch.object(dataforce, 'qualify_detail_record', side_effect=lambda job, page: job):
            qualified, requested, failures, known_failures = dataforce.collect_index_linked_details(known+[extra])
        self.assertEqual(qualified, known)
        self.assertEqual((requested, failures, known_failures), (9, 1, 0))

    def test_dataforce_failed_known_detail_does_not_renew_its_variant_or_siblings_absence(self):
        known = [SimpleNamespace(url='https://dataforcecommunity.transperfect.com'+path,
                    title='Thyme', commitment='Remote')
                 for path in sorted(dataforce.QUALIFIED_DETAIL_PATHS)]
        failed = known[3]
        def fetch(url):
            if url == failed.url:
                raise TimeoutError('one role unavailable')
            return '<html>known detail</html>'
        with patch.object(dataforce, 'remaining_http_requests', return_value=8), \
                patch.object(dataforce, 'fetch_page', side_effect=fetch), \
                patch.object(dataforce, 'qualify_detail_record', side_effect=lambda job, page: job):
            qualified, requested, inspections, verifications = dataforce.collect_index_linked_details(known)
        self.assertEqual(qualified, [job for job in known if job != failed])
        self.assertEqual((requested, inspections, verifications), (8, 0, 1))

    def test_handshake_chunk_scope_and_coverage(self):
        module = ('new URL(`./file-chunk-default-0.framercms`,'
                  '`https://framerusercontent.com/modules/site/hash/file.js`)'
                  '.href.replace(`/modules/`,`/cms/`)'
                  'new URL(`./file-chunk-default-1.framercms`,'
                  '`https://framerusercontent.com/modules/site/hash/file.js`)'
                  '.href.replace(`/modules/`,`/cms/`)')
        self.assertEqual(len(handshake.extract_collection_chunk_urls(module)), 2)
        self.assertEqual(len(handshake.extract_collection_chunk_urls(
            module, linked_module_url="https://framerusercontent.com/sites/site/file.hash.mjs")), 2)
        self.assertEqual(len(handshake.extract_collection_chunk_urls(
            module, linked_module_url="https://framerusercontent.com/sites/site/CMS_Cleaning.hash.mjs")), 2)
        unrelated = ('new URL(`./other-chunk-default-0.framercms`,'
                     '`https://framerusercontent.com/modules/site/hash/other.js`)'
                     '.href.replace(`/modules/`,`/cms/`)')
        self.assertEqual(len(handshake.extract_collection_chunk_urls(
            module + unrelated,
            linked_module_url="https://framerusercontent.com/sites/site/file.hash.mjs")), 2)
        with self.assertRaises(ValueError):
            handshake.extract_collection_chunk_urls(module + unrelated)
        with self.assertRaises(ValueError):
            handshake.extract_collection_chunk_urls(
                module, linked_module_url="https://evil.example/sites/other/other.hash.mjs")
        with self.assertRaises(ValueError):
            handshake.extract_collection_chunk_urls(module.replace("-default-1", "-default-2"))
        with self.assertRaises(ValueError):
            handshake.extract_collection_chunk_urls(module.replace(
                "https://framerusercontent.com/modules/", "https://evil.example/modules/"))
        self.assertFalse(handshake.should_include_record({
            handshake.FIELD_ID: "1", handshake.FIELD_TITLE: "Role",
            handshake.FIELD_SLUG: "../escape", handshake.FIELD_SHOW_JOB: True}))
        visible = {handshake.FIELD_ID: 'cms-1', handshake.FIELD_TITLE: 'AI Evaluator',
                   handshake.FIELD_SLUG: 'ai-evaluator', handshake.FIELD_SHOW_JOB: True,
                   handshake.FIELD_WORK_LOCATION: 'Remote',
                   handshake.FIELD_DESCRIPTION: 'Remote AI evaluation of language models.',
                   handshake.FIELD_APPLICATION:
                       'https://app.joinhandshake.com/signup?destination_hai_path=%2Fauth&hai_job_id=123'}
        self.assertTrue(handshake.qualified_public_record(visible))
        self.assertFalse(handshake.qualified_public_record({**visible,
            handshake.FIELD_WORK_LOCATION: 'New York, NY'}))
        self.assertFalse(handshake.qualified_public_record({**visible,
            handshake.FIELD_APPLICATION: 'https://app.joinhandshake.com/signup'}))
        with patch.object(handshake_company, 'fetch_handshake_jobs',
                          return_value=([handshake.parse_opportunity_record(visible, {}, {})], 2, 0)):
            held = handshake_company.crawl_handshake('https://joinhandshake.com/ai/opportunities/')
        self.assertEqual(held.jobs, [])
        self.assertEqual(held.raw_record_count, 2)
        self.assertFalse(held.snapshot_complete)

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
                                       {"title": "Specialist", "the-role": "AI model evaluation"},
                                       "Specialist Remote")
        with self.assertRaises(RuntimeError):
            surge.parse_workforce_detail(record, "<h1>Surge workforce</h1>")
        with self.assertRaises(RuntimeError):
            surge.parse_workforce_detail(record, '<h1 data-job="title">Specialist</h1>')
        detail = ('<link href="https://surgehq.ai/workforce/specialist" rel="canonical">'
                  '<h1 data-job="title">Specialist</h1>'
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
