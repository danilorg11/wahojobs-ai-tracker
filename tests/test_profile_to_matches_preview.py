import json
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

import matching_quality_report as benchmark
import profile_match_digest as matcher
import profile_to_matches_preview as preview
from wahojobs.profiles.canonical import validate_canonical_profile


class ProfileToMatchesPreviewTests(unittest.TestCase):
    def test_inline_preview_json_contains_valid_canonical_profile(self):
        data = run_preview_json(
            "--input-text",
            "I speak English and Spanish, no college degree, looking for remote beginner AI data tasks with no phone calls.",
            "--input-style",
            "short_paragraph",
        )

        self.assertEqual(data["normalizer"], "baseline")
        self.assertTrue(validate_canonical_profile(data["canonical_profile"]))
        self.assertEqual(data["canonical_profile"]["identity"]["profile_id"], "preview_profile")
        self.assertIn("BaselineHeuristicProfileNormalizer", data["disclaimer"])
        self.assertIn("matches", data)
        self.assertTrue(set(preview.SECTION_ORDER) <= set(data["matches"]))
        self.assertEqual(data["canonical_profile"]["location"]["remote_eligibility"], "unknown")
        self.assertTrue(data["canonical_profile"]["preferences"]["remote"])
        self.assertEqual(data["canonical_profile"]["preferences"]["phone_preference"], "non-phone preferred")
        self.assertTrue(any("Location is missing" in warning for warning in data["warnings"]))

    def test_input_file_preview_json_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "profile.txt"
            path.write_text(
                "Senior Software Engineer, 8 years. Python, TypeScript, React. Remote contract preferred.",
                encoding="utf-8",
            )
            data = run_preview_json(
                "--input-file",
                str(path),
                "--input-style",
                "resume_or_linkedin_style",
            )

        canonical = data["canonical_profile"]
        self.assertTrue(validate_canonical_profile(canonical))
        self.assertEqual(canonical["experience"]["total_years"], 8)
        self.assertIn("software engineering", canonical["education"]["fields_or_domains"])
        self.assertIn("python", canonical["skills"]["normalized"])

    def test_messy_sparse_input_surfaces_missing_and_ambiguous_fields(self):
        context = preview.build_preview_context(
            "remote tasks pls. can write review. no college. not coding. not calls",
            "messy_sparse_input",
            limit=2,
        )

        self.assertIn("languages", context["missing_fields"])
        self.assertIn("messy_input", context["ambiguous_fields"])
        self.assertIn("no college degree", context["canonical_profile"]["constraints"]["hard_constraints"])
        self.assertEqual(context["canonical_profile"]["preferences"]["phone_preference"], "non-phone preferred")

    def test_text_and_html_renderers_include_demo_warning(self):
        context = preview.build_preview_context(
            "Lawyer with contract and IP experience, interested in legal AI training. Remote work preferred.",
            "short_paragraph",
            limit=1,
        )
        text = preview.render_context(context, "text")
        html = preview.render_context(context, "html")

        self.assertIn("heuristic/demo-only", text)
        self.assertIn("Canonical Profile Preview", text)
        self.assertIn("heuristic/demo-only", html)
        self.assertIn("Recommended Opportunities", html)
        self.assertIn("Remote preference", html)

    def test_preview_shows_unconfirmed_language_metadata_gap(self):
        context = preview.build_preview_context(
            "I speak English and Spanish, no college degree, looking for remote beginner AI data tasks.",
            "short_paragraph",
            limit=5,
        )

        warnings = "\n".join(context["warnings"])
        self.assertIn("unconfirmed language requirements", warnings)
        flagged = [
            match
            for section in ("excluded",)
            for match in context["matches"][section]
            if any("Detected unsupported language requirement" in diagnostic for diagnostic in match["preview_diagnostics"])
        ]
        self.assertTrue(flagged)

    def test_preview_does_not_change_matcher_benchmark(self):
        fixture = benchmark.load_fixture()
        profiles = benchmark.load_benchmark_profiles(fixture)
        rows = benchmark.load_benchmark_db_rows()
        evaluated = [
            benchmark.evaluate_case(case, profiles[case["profile_id"]], rows, matcher)
            for case in fixture["cases"]
            if case.get("label_source") == "human_reviewed"
        ]
        metrics = benchmark.human_reviewed_agreement(evaluated)

        self.assertEqual(
            (
                metrics["label_agreement"],
                metrics["section_agreement"],
                metrics["full_agreement"],
                metrics["total"],
            ),
            (26, 29, 26, 30),
        )


def run_preview_json(*args):
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(ROOT / "scripts" / "profile_to_matches_preview.py"),
            *args,
            "--format",
            "json",
            "--limit",
            "2",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


if __name__ == "__main__":
    unittest.main()
