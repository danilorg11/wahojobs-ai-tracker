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

    def test_apply_dry_run_reads_reviewed_csv(self):
        rows = review.read_review_csv(ROOT / "exports" / "opportunity_metadata_gap_review.csv")
        plan = review.build_apply_plan(rows, reviewed_source="exports/opportunity_metadata_gap_review.csv")

        self.assertEqual(86, plan.rows_read)
        self.assertEqual(60, plan.approved_rows)
        self.assertFalse(plan.validation_errors)
        self.assertFalse(plan.conflicts)
        self.assertEqual(16, plan.skipped_by_decision["needs_more_research"])
        self.assertEqual(10, plan.skipped_by_decision["keep_diagnostic_only"])

    def test_apply_accepts_assamese_human_override(self):
        rows = review.read_review_csv(ROOT / "exports" / "opportunity_metadata_gap_review.csv")
        plan = review.build_apply_plan(rows, reviewed_source="exports/opportunity_metadata_gap_review.csv")
        assamese_records = [
            record
            for record in plan.overlay_records
            if "Assamese" in record["required_languages"]
        ]

        self.assertEqual(1, len(assamese_records))
        record = assamese_records[0]
        self.assertIn("English", record["required_languages"])
        self.assertIn("Assamese", record["required_languages"])
        self.assertTrue(any("ambiguous pattern" in warning for warning in record["warnings"]))
        self.assertTrue(
            any(
                provenance["review_id"] == "omgr_mercor_ambiguous-metadata_ampersand-language-list_986"
                for provenance in record["provenance"]
            )
        )

    def test_apply_keeps_farsi_persian_as_language_not_locale(self):
        rows = review.read_review_csv(ROOT / "exports" / "opportunity_metadata_gap_review.csv")
        plan = review.build_apply_plan(rows, reviewed_source="exports/opportunity_metadata_gap_review.csv")
        records = [
            record
            for record in plan.overlay_records
            if "Farsi" in record["required_languages"]
        ]

        self.assertEqual(1, len(records))
        record = records[0]
        self.assertIn("English", record["required_languages"])
        self.assertIn("Farsi", record["required_languages"])
        self.assertIn("English (United States)", record["language_locale"])
        self.assertFalse(any("Farsi" in locale or "Persian" in locale for locale in record["language_locale"]))

    def test_apply_translation_pair_preserves_both_languages(self):
        plan = review.build_apply_plan(
            [
                apply_row(
                    "pair-1",
                    title="English (United States) <> French (France) Lyric Translation Reviewer",
                    human_required_languages="English, French",
                    human_language_locale="English (United States), French (France)",
                    candidate_pattern="translation_or_translator",
                )
            ],
            reviewed_source="review.csv",
        )

        self.assertFalse(plan.validation_errors)
        record = plan.overlay_records[0]
        self.assertEqual(["English", "French"], record["required_languages"])
        self.assertEqual(["English (United States)", "French (France)"], record["language_locale"])

    def test_apply_merges_multiple_rows_for_same_opportunity(self):
        rows = [
            apply_row("merge-lang", job_id="42", human_required_languages="English, Norwegian"),
            apply_row(
                "merge-locale",
                job_id="42",
                human_required_languages="English",
                human_language_locale="English (US), Norwegian (Norway)",
                candidate_pattern="language_parenthetical_locale",
            ),
        ]
        plan = review.build_apply_plan(rows, reviewed_source="review.csv")

        self.assertFalse(plan.validation_errors)
        self.assertFalse(plan.conflicts)
        self.assertEqual(2, plan.approved_rows)
        self.assertEqual(1, len(plan.overlay_records))
        self.assertEqual(1, plan.rows_merged)
        record = plan.overlay_records[0]
        self.assertEqual(["English", "Norwegian"], record["required_languages"])
        self.assertEqual(["English (US)", "Norwegian (Norway)"], record["language_locale"])
        self.assertEqual(2, len(record["provenance"]))

    def test_apply_conflicting_location_restriction_fails_conservatively(self):
        rows = [
            apply_row("conflict-1", job_id="42", human_location_restriction="United States"),
            apply_row("conflict-2", job_id="42", human_location_restriction="Canada"),
        ]
        plan = review.build_apply_plan(rows, reviewed_source="review.csv")

        self.assertTrue(plan.conflicts)
        self.assertIn("location_restriction conflict", plan.conflicts[0])

    def test_apply_dry_run_writes_no_output_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "review.csv"
            output_path = Path(tmpdir) / "overlay.json"
            write_apply_csv(input_path, [apply_row("apply-1")])
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "scripts" / "opportunity_metadata_gap_review.py"),
                    "apply",
                    "--input",
                    str(input_path),
                    "--dry-run",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertIn("No files or database rows were written.", result.stdout)
            self.assertFalse(output_path.exists())

    def test_real_apply_requires_yes_and_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "review.csv"
            write_apply_csv(input_path, [apply_row("apply-1")])
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "scripts" / "opportunity_metadata_gap_review.py"),
                    "apply",
                    "--input",
                    str(input_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("write-protected", result.stderr)

    def test_real_apply_writes_deterministic_json_to_temp_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "review.csv"
            output_path = Path(tmpdir) / "overlay.json"
            write_apply_csv(input_path, [apply_row("apply-1")])

            command = [
                sys.executable,
                "-B",
                str(ROOT / "scripts" / "opportunity_metadata_gap_review.py"),
                "apply",
                "--input",
                str(input_path),
                "--yes",
                "--output",
                str(output_path),
            ]
            subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
            first = output_path.read_text(encoding="utf-8")
            subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
            second = output_path.read_text(encoding="utf-8")

            self.assertEqual(first, second)
            self.assertIn('"schema_version": 1', first)
            self.assertIn('"records"', first)


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


def apply_row(
    review_id,
    job_id="123",
    source="mercor",
    title="English Audio Generalist Evaluator Expert",
    human_required_languages="English",
    human_language_locale="",
    human_location_restriction="",
    candidate_pattern="audio_language_role",
    candidate_confidence="high",
    review_decision="approve_inferred_metadata",
    apply_eligible="yes",
):
    row = {column: "" for column in review.CSV_COLUMNS}
    row.update(
        {
            "review_id": review_id,
            "candidate_type": "language_requirement",
            "source": source,
            "company": source.title(),
            "job_id": job_id,
            "external_id": f"external-{job_id}",
            "source_hash": f"hash-{job_id}",
            "title": title,
            "url": f"https://example.com/{job_id}",
            "location": "Remote",
            "candidate_pattern": candidate_pattern,
            "candidate_confidence": candidate_confidence,
            "evidence_text": f"title={title}; pattern={candidate_pattern}",
            "risk_notes": "Reviewed in test fixture.",
            "review_decision": review_decision,
            "human_required_languages": human_required_languages,
            "human_language_locale": human_language_locale,
            "human_location_restriction": human_location_restriction,
            "human_notes": "Human reviewed metadata.",
            "apply_eligible": apply_eligible,
        }
    )
    return row


def write_apply_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=review.CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
