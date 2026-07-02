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
            limit=80,
        )

        warnings = "\n".join(context["warnings"])
        self.assertIn("unconfirmed language requirements", warnings)
        visible_flagged = [
            match
            for section in ("do_these_first", "best_matches", "also_worth_reviewing")
            for match in context["matches"][section]
            if any(
                "Possible unconfirmed language requirement" in diagnostic
                for diagnostic in match["preview_diagnostics"]
            )
        ]
        self.assertEqual(visible_flagged, [])
        capped_flagged = [
            match
            for match in all_preview_matches(context)
            if any(
                "Possible unconfirmed language requirement" in diagnostic
                for diagnostic in match["preview_diagnostics"]
            )
        ]
        self.assertTrue(capped_flagged)
        self.assertTrue(
            all(match["preview_section"] == "explore_only" for match in capped_flagged),
            capped_flagged[:3],
        )
        flagged = [
            match
            for section in ("excluded",)
            for match in context["matches"][section]
            if any("Detected unsupported language requirement" in diagnostic for diagnostic in match["preview_diagnostics"])
        ]
        self.assertTrue(flagged)

    def test_software_preview_caps_science_coding_roles_when_credentials_are_absent(self):
        context = preview.build_preview_context(
            "Senior Software Engineer, 8 years. Python, TypeScript, React, APIs, test automation. "
            "I don't have biology or medical credentials, but I can evaluate coding tasks and tests. "
            "Looking for remote AI coding evaluator work.",
            "resume_or_linkedin_style",
            limit=100,
        )

        canonical = context["canonical_profile"]
        self.assertIn("software engineering", canonical["education"]["fields_or_domains"])
        self.assertNotIn("biology", canonical["education"]["fields_or_domains"])
        self.assertIn("no biology or medical credentials", canonical["constraints"]["hard_constraints"])

        best_titles = {match["display_title"] for match in context["matches"]["best_matches"]}
        self.assertIn("Backend Engineer (Coding Agent Experience)", best_titles)

        visible_science_coding = [
            match
            for section in ("do_these_first", "best_matches", "also_worth_reviewing")
            for match in context["matches"][section]
            if title_has_any(match, ("biology", "biologist", "chemistry", "material science", "materials science"))
            and title_has_any(match, ("python", "coding", "software", "code"))
        ]
        self.assertEqual(visible_science_coding, [])

        capped = [
            match
            for match in context["matches"]["explore_only"]
            if any(
                "no biology or medical credentials" in diagnostic
                for diagnostic in match["preview_diagnostics"]
            )
        ]
        self.assertTrue(capped)

    def test_biology_preview_preserves_research_signals_without_overpromoting_licensed_roles(self):
        context = preview.build_preview_context(
            "PhD microbiologist with biology research, academic writing, and scientific writing experience. "
            "I can review biology and medicine-related AI outputs, but I am not a licensed physician. "
            "Remote work preferred.",
            "long_paragraph",
            limit=100,
        )

        canonical = context["canonical_profile"]
        self.assertIn("microbiology", canonical["education"]["fields_or_domains"])
        self.assertIn("academic writing", canonical["skills"]["normalized"])
        self.assertIn("scientific writing", canonical["skills"]["normalized"])
        self.assertIn("no medical license", canonical["constraints"]["hard_constraints"])
        signal_names = {signal[0] for signal in context["matcher_profile"]["signals"]}
        self.assertIn("Microbiology/research writing signal", signal_names)

        microbio_matches = [
            match
            for match in all_preview_matches(context)
            if "microbiology" in match["display_title"].lower()
        ]
        self.assertTrue(microbio_matches)
        self.assertTrue(
            any("Microbiology/research writing signal" in "; ".join(match["reasons"]) for match in microbio_matches)
        )

        licensed_visible = [
            match
            for section in ("do_these_first", "best_matches", "also_worth_reviewing")
            for match in context["matches"][section]
            if title_has_any(match, ("registered nurse", "licensed physician", "medical doctor", "physician"))
        ]
        self.assertEqual(licensed_visible, [])

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


def all_preview_matches(context):
    return [
        match
        for section in preview.SECTION_ORDER
        for match in context["matches"][section]
    ]


def title_has_any(match, terms):
    title = match["display_title"].lower()
    return any(term in title for term in terms)


if __name__ == "__main__":
    unittest.main()
