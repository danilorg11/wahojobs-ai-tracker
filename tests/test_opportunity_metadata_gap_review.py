import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import opportunity_metadata_gap_review as review


class OpportunityMetadataGapReviewTests(unittest.TestCase):
    def test_build_candidates_contains_evidence_and_unique_stable_ids(self):
        candidates = review.select_review_batch(review.build_candidates(sample_rows()), max_rows=20)
        rows = [candidate.data for candidate in candidates]
        review_ids = [row["review_id"] for row in rows]

        self.assertEqual(len(review_ids), len(set(review_ids)))
        self.assertTrue(all(row["review_decision"] == "pending_review" for row in rows))
        self.assertTrue(all(row["evidence_text"] for row in rows))
        self.assertTrue(all(row["risk_notes"] for row in rows))

    def test_ambiguous_candidates_are_not_apply_eligible(self):
        candidates = review.build_candidates(sample_rows())
        ambiguous = [
            candidate.data
            for candidate in candidates
            if candidate.data["candidate_confidence"] == "ambiguous"
        ]

        self.assertTrue(ambiguous)
        self.assertTrue(all(row["apply_eligible"] == "no" for row in ambiguous))

    def test_high_confidence_candidates_are_separate_from_ambiguous(self):
        candidates = review.build_candidates(sample_rows())
        high = [candidate for candidate in candidates if candidate.confidence == "high"]
        ambiguous = [candidate for candidate in candidates if candidate.confidence == "ambiguous"]

        self.assertTrue(high)
        self.assertTrue(ambiguous)
        self.assertTrue(all(candidate.candidate_type != "ambiguous_metadata" for candidate in high))
        self.assertTrue(all(candidate.candidate_type == "ambiguous_metadata" for candidate in ambiguous))

    def test_write_artifacts_to_temporary_prefix(self):
        candidates = review.select_review_batch(review.build_candidates(sample_rows()), max_rows=20)
        with tempfile.TemporaryDirectory() as tmpdir:
            prefix = Path(tmpdir) / "opportunity_metadata_gap_review"
            review.write_artifacts(candidates, prefix)
            csv_path = prefix.with_suffix(".csv")
            html_path = prefix.with_suffix(".html")
            summary_path = review.summary_path(prefix)

            self.assertTrue(csv_path.exists())
            self.assertTrue(html_path.exists())
            self.assertTrue(summary_path.exists())

            with csv_path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(rows)
            self.assertTrue({row["review_decision"] for row in rows} <= review.REVIEW_DECISIONS)
            self.assertIn("Opportunity Metadata Gap Review", html_path.read_text(encoding="utf-8"))
            self.assertIn("Nothing has been applied", summary_path.read_text(encoding="utf-8"))

    def test_cli_generates_temp_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefix = Path(tmpdir) / "review"
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "scripts" / "opportunity_metadata_gap_review.py"),
                    "--output-prefix",
                    str(prefix),
                    "--max-rows",
                    "12",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertIn("Review artifact only", result.stdout)
            self.assertTrue(prefix.with_suffix(".csv").exists())
            self.assertTrue(prefix.with_suffix(".html").exists())
            self.assertTrue(review.summary_path(prefix).exists())


def sample_rows():
    return [
        row(1, "oneforma", "Acceptability Raters - English (US) to Norwegian (Norway)"),
        row(9, "oneforma", "Acceptability Raters - English (US) to Bulgarian (Bulgaria)", canonical_id="same-canonical"),
        row(10, "oneforma", "Acceptability Raters - English (US) to Catalan (Spain)", canonical_id="same-canonical"),
        row(2, "mercor", "English (US) Audio Generalist Evaluator Expert"),
        row(3, "mercor", "Generalist - English & Assamese"),
        row(4, "mercor", "UK-Based Legal Experts: US Finance"),
        row(5, "meridial", "Spanish Language Specialist - Freelance AI Trainer Project", language=""),
        row(6, "meridial", "American Sign Language (ASL) - Freelance AI Trainer Project"),
        row(7, "outlier", "Portuguese (Brazil) Freelance Writer", location="Remote - Brazil"),
        row(8, "welocalize", "Ads Quality Rater - Spanish (Mexico)", language="Spanish"),
    ]


def row(job_id, source, title, location="Remote", language="", locale="", canonical_id=""):
    return {
        "job_id": job_id,
        "external_id": f"external-{job_id}",
        "source_hash": f"hash-{job_id}",
        "title": title,
        "location": location,
        "url": f"https://example.com/{job_id}",
        "department": "Unknown",
        "expertise": "Unknown",
        "commitment": "",
        "canonical_opportunity_id": canonical_id,
        "company": source.title(),
        "source": source,
        "current_language": language,
        "current_language_locale": locale,
        "canonical_title": "",
        "source_category": "Unknown",
    }


if __name__ == "__main__":
    unittest.main()
