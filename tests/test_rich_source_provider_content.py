import unittest

from wahojobs.crawler.providers.dataannotation import (
    DOMAIN_PAGES,
    parse_domain_page,
)
from wahojobs.crawler.providers.dataforce import parse_job_block
from wahojobs.crawler.providers.greenhouse import parse_greenhouse_job
from wahojobs.crawler.providers.handshake import (
    FIELD_DEGREE_FILTERS,
    FIELD_ID,
    FIELD_SALARY,
    FIELD_SHOW_JOB,
    FIELD_SLUG,
    FIELD_SUBJECT_FILTERS,
    FIELD_TITLE,
    parse_opportunity_record,
)
from wahojobs.crawler.providers.lever import parse_lever_posting
from wahojobs.crawler.providers.mercor import parse_mercor_listing
from wahojobs.crawler.providers.oneforma import parse_oneforma_post
from wahojobs.crawler.providers.outlier import parse_outlier_job
from wahojobs.crawler.providers.surge import WorkforceRecord, parse_workforce_detail
from wahojobs.crawler.providers.turing import parse_turing_job
from wahojobs.crawler.providers.workable_markdown import parse_workable_row


class RichSourceProviderContentTests(unittest.TestCase):
    def test_legacy_greenhouse_path_preserves_public_job_content(self):
        candidate = parse_greenhouse_job(
            {
                "id": 123,
                "title": "Applied AI Engineer",
                "absolute_url": "https://job-boards.example.test/jobs/123",
                "location": {"name": "Remote"},
                "content": "<p>Build applied AI systems and review deployments.</p>",
                "updated_at": "2026-08-16T12:00:00-03:00",
                "offices": [{"name": "Remote"}],
            }
        )
        self.assertEqual(
            candidate.source_body,
            "<p>Build applied AI systems and review deployments.</p>",
        )
        self.assertEqual(candidate.source_body_format, "text/html")
        self.assertEqual(candidate.source_metadata["offices"], [{"name": "Remote"}])
        self.assertEqual(candidate.source_updated_at, "2026-08-16T12:00:00-03:00")

    def test_lever_preserves_plain_description_and_public_metadata(self):
        posting = {
            "id": "lever-1",
            "text": "Python Reviewer",
            "hostedUrl": "https://jobs.example.test/lever-1",
            "descriptionPlain": "Review Python code and explain defects.",
            "additionalPlain": "Python is required.",
            "categories": {
                "location": "Remote",
                "department": "AI",
                "commitment": "Contract",
            },
            "lists": [{"text": "Responsibilities", "content": "Review code"}],
            "workplaceType": "remote",
        }
        candidate = parse_lever_posting(posting)
        self.assertEqual(candidate.source_body, posting["descriptionPlain"])
        self.assertEqual(candidate.source_body_format, "text/plain")
        self.assertEqual(candidate.source_metadata["additionalPlain"], "Python is required.")
        self.assertEqual(candidate.source_metadata["lists"], posting["lists"])

        without_description = parse_lever_posting(
            {key: value for key, value in posting.items() if key != "descriptionPlain"}
        )
        self.assertIsNone(without_description.source_body)
        self.assertIsNone(without_description.source_body_format)

    def test_oneforma_preserves_rendered_body_for_each_real_variant(self):
        candidates = parse_oneforma_post(
            {
                "id": 42,
                "title": {"rendered": "Speech Review Project"},
                "content": {"rendered": "<p>Record and review natural speech.</p>"},
                "excerpt": {"rendered": "<p>Speech project</p>"},
                "link": "https://www.oneforma.com/job/42",
                "acf": {"apply_job": []},
                "_embedded": {"wp:term": []},
                "modified_gmt": "2026-08-16T00:00:00",
            }
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0].source_body,
            "<p>Record and review natural speech.</p>",
        )
        self.assertEqual(candidates[0].source_body_format, "text/html")
        self.assertEqual(candidates[0].source_updated_at, "2026-08-16T00:00:00")

    def test_public_page_and_card_sources_forward_only_content_they_expose(self):
        dataannotation_html = "<main><h1>Coding</h1><p>Review code remotely.</p></main>"
        domain_candidate = parse_domain_page(
            DOMAIN_PAGES[0],
            "https://www.dataannotation.tech/coding",
            dataannotation_html,
        )
        self.assertEqual(domain_candidate.source_body, dataannotation_html)
        self.assertEqual(domain_candidate.source_body_format, "text/html")

        block = (
            '<div class="views-field views-field-title"><h2 class="field-content">'
            'Speech Project</h2></div><a href="/project/speech">Apply</a>'
            '<p><strong>Category</strong><br>Audio</p>'
            '<p><strong>Type</strong><br>Remote</p>'
            '<p><strong>Country</strong><br>Brazil</p>'
        )
        card_candidate = parse_job_block(
            block,
            "https://dataforcecommunity.transperfect.com/projects",
        )
        self.assertIn("Speech Project", card_candidate.source_body)
        self.assertEqual(card_candidate.source_metadata["Category"], "Audio")

    def test_detail_page_and_marketplace_records_forward_body_and_metadata(self):
        surge = parse_workforce_detail(
            WorkforceRecord(
                slug="python-reviewer",
                url="https://surgehq.ai/workforce/python-reviewer",
                fields={"title": "Python Reviewer", "pay-rate": "$30/hr"},
                index_text="Python reviewer",
            ),
            "<main>Review Python code. Work remotely with technical teams.</main>",
        )
        self.assertIn("Review Python code", surge.source_body)
        self.assertEqual(surge.source_metadata["pay-rate"], "$30/hr")

        records = (
            parse_mercor_listing(
                {
                    "listingId": "mercor-1",
                    "title": "Code Reviewer",
                    "listingDomain": "Software",
                    "description": "Review production Python services.",
                    "skills": ["Python"],
                    "updatedAt": "2026-08-16",
                }
            ),
            parse_outlier_job(
                {
                    "id": "outlier-1",
                    "title": "AI Code Evaluator",
                    "description": "Evaluate model-generated code.",
                    "skillNames": ["Python"],
                }
            ),
            parse_turing_job(
                {
                    "id": "turing-1",
                    "jobCode": "T-1",
                    "title": "Python Engineer",
                    "description": "Build and test Python APIs.",
                    "skills": ["Python"],
                }
            ),
            parse_workable_row(
                "toloka-ai",
                {
                    "shortcode": "workable-1",
                    "title": "AI Reviewer",
                    "state": "published",
                    "isInternal": False,
                    "description": "Review AI answers against written criteria.",
                    "skills": ["Critical thinking"],
                },
            ),
        )
        self.assertEqual(
            [item.source_body for item in records],
            [
                "Review production Python services.",
                "Evaluate model-generated code.",
                "Build and test Python APIs.",
                "Review AI answers against written criteria.",
            ],
        )
        self.assertEqual(records[0].source_metadata["skills"], ["Python"])
        self.assertEqual(records[1].source_metadata["skillNames"], ["Python"])
        self.assertEqual(records[2].source_metadata["skills"], ["Python"])
        self.assertEqual(records[3].source_metadata["skills"], ["Critical thinking"])

    def test_handshake_preserves_available_cms_metadata_without_making_a_description(self):
        candidate = parse_opportunity_record(
            {
                FIELD_ID: "cms-1",
                FIELD_TITLE: "Math Expert",
                FIELD_SLUG: "math-expert",
                FIELD_SHOW_JOB: True,
                FIELD_SALARY: 35,
                FIELD_SUBJECT_FILTERS: ["subject-1"],
                FIELD_DEGREE_FILTERS: ["degree-1"],
            },
            {"subject-1": "Mathematics"},
            {"degree-1": "Bachelor's"},
        )
        self.assertIsNone(candidate.source_body)
        self.assertIsNone(candidate.source_body_format)
        self.assertEqual(candidate.source_metadata["subjects"], ["Mathematics"])
        self.assertEqual(candidate.source_metadata["degrees"], ["Bachelor's"])


if __name__ == "__main__":
    unittest.main()
