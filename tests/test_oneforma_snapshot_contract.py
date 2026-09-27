"""Synthetic source envelopes exercise lifecycle safety; no employer/model calls."""
from contextlib import closing
import gc
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.evidence_maintenance_support import BytesResponse
from wahojobs.crawler import pipeline
from wahojobs.crawler.providers import oneforma
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import initialize_database

URL = "https://www.oneforma.com/wp-json/wp/v2/job?per_page=100&_embed=wp:term"
AT = "2026-09-27T06:00:00+00:00"


def post(identity=42):
    return dict(id=identity, title={"rendered": "AI Speech Review"},
                content={"rendered": "<p>Review speech for AI models remotely.</p>"},
                link=f"https://www.oneforma.com/job/{identity}",
                acf={"apply_job": [
                    {"language": "English", "apply_url": "https://my.oneforma.com/jobs/1"},
                    {"language": "Portuguese", "apply_url": "https://my.oneforma.com/jobs/2"}]})


def response(rows, *, total=None, pages="1"):
    result = BytesResponse(json.dumps(rows).encode(), URL + "&page=1")
    result.headers["X-WP-Total"] = str(len(rows)) if total is None else total
    result.headers["X-WP-TotalPages"] = pages
    return result


class OneFormaSnapshotContractTests(unittest.TestCase):
    def test_truncated_response_cannot_claim_complete_snapshot(self):
        with patch.object(oneforma, "urlopen", return_value=response([post()], total="2")):
            with self.assertRaisesRegex(ValueError, "page record count"):
                oneforma.fetch_oneforma_jobs(URL)

    def test_headers_must_prove_consistent_exact_count(self):
        for name in ("X-WP-Total", "X-WP-TotalPages"):
            for value in (None, "-1", "1.0", "true", " 1"):
                observed = response([post()])
                del observed.headers[name]
                if value is not None:
                    observed.headers[name] = value
                with self.subTest(name=name, value=value), patch.object(oneforma, "urlopen", return_value=observed):
                    with self.assertRaises(ValueError):
                        oneforma.fetch_oneforma_jobs(URL)
        with patch.object(oneforma, "urlopen", return_value=response([post()], pages="2")):
            with self.assertRaisesRegex(ValueError, "total page count disagrees"):
                oneforma.fetch_oneforma_jobs(URL)

    def test_invalid_wordpress_post_identity_cannot_enter_lifecycle(self):
        for identity in (None, True, 0, -1, "42", 1.5):
            with self.subTest(identity=identity), patch.object(
                    oneforma, "urlopen", return_value=response([post(identity)])):
                with self.assertRaisesRegex(ValueError, "without an identifier"):
                    oneforma.fetch_oneforma_jobs(URL)

    def test_total_changes_and_duplicates_cannot_authorize_closure(self):
        with patch.object(oneforma, "fetch_page", side_effect=[([post(1)], 2, 2), ([post(2)], 2, 3)]):
            with self.assertRaisesRegex(ValueError, "total record count changed"):
                oneforma.fetch_all_posts(URL)
        with patch.object(oneforma, "fetch_page", side_effect=[([post(1)], 2, 2), ([post(1)], 2, 2)]):
            with self.assertRaisesRegex(ValueError, "duplicate post"):
                oneforma.fetch_all_posts(URL)

    def test_complete_multiple_pages_and_source_application_anchor_remain_supported(self):
        first = [post(index) for index in range(1, 101)]
        second = [post(101)]
        second[0]["acf"]["apply_job"] = [{"language": "English", "apply_url": "#apply"}]
        with patch.object(oneforma, "urlopen", side_effect=[response(first, total="101", pages="2"),
                                                           response(second, total="101", pages="2")]):
            jobs = oneforma.fetch_oneforma_jobs(URL)
        self.assertEqual(len(jobs), 201)
        self.assertEqual(jobs[-1].external_id, "oneforma::101::english")

    def test_empty_wordpress_collection_does_not_implicitly_authorize_closure(self):
        from wahojobs.crawler.companies.oneforma import crawl_oneforma
        from wahojobs.crawler.types import evaluate_removal_authorization
        with patch.object(oneforma, "urlopen", return_value=response([], total="0", pages="0")):
            result = crawl_oneforma(URL)
        self.assertEqual(result.jobs, [])
        self.assertFalse(evaluate_removal_authorization(result).authorized)

    def test_malformed_variants_preserve_existing_inventory_and_confirmed_removal_works(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "inventory.sqlite3"
            initialize_database(target)
            def rows():
                with closing(get_connection(target)) as conn:
                    return [dict(row) for row in conn.execute(
                        "SELECT j.* FROM jobs j JOIN companies c ON c.id=j.company_id "
                        "WHERE c.slug='oneforma' ORDER BY j.id")]
            def crawl(observed):
                with patch.object(oneforma, "urlopen", return_value=observed), \
                        patch.object(pipeline, "utc_now", return_value=AT), \
                        patch("wahojobs.tracking.service.tracking_openai_client", return_value=None), \
                        patch("socket.create_connection", side_effect=AssertionError("Network forbidden")):
                    return pipeline.run_crawl("oneforma", db_path=target)
            crawl(response([post()]))
            before = rows()
            self.assertEqual(len(before), 2)
            malformed = [None, {}, {"apply_job": None}, {"apply_job": []},
                         {"apply_job": "not rows"}, {"apply_job": [None]},
                         {"apply_job": [{"language": "English"}]},
                         {"apply_job": [{"language": 7, "apply_url": "https://example.test"}]},
                         {"apply_job": [{"language": "English", "apply_url": ""}]}]
            for acf in malformed:
                bad = post()
                bad["acf"] = acf
                with self.subTest(acf=acf), self.assertRaises(ValueError):
                    crawl(response([bad]))
                self.assertEqual(rows(), before)
            with self.assertRaises(ValueError):
                crawl(response([post()], total="2"))
            self.assertEqual(rows(), before)
            current = post()
            current["acf"]["apply_job"].pop()
            _, summary = crawl(response([current]))
            self.assertEqual(summary.jobs_removed, 1)
            self.assertEqual(sum(row["is_active"] for row in rows()), 1)
            gc.collect()


if __name__ == "__main__":
    unittest.main()
