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

    def test_html_preview_has_product_friendly_profile_summary(self):
        context = preview.build_preview_context(
            "I speak English and Spanish, no college degree, looking for remote beginner AI data tasks with no phone calls.",
            "short_paragraph",
            limit=12,
        )
        html = preview.render_context(context, "html")

        self.assertIn("AI Work Match Preview", html)
        self.assertIn("Profile Understood", html)
        self.assertIn("What We Still Need To Know", html)
        self.assertIn("These are the main signals we extracted", html)
        self.assertIn("Your country or work location", html)

    def test_html_preview_limits_visible_opportunity_cards(self):
        context = preview.build_preview_context(
            "I speak English and Spanish, no college degree, looking for remote beginner AI data tasks.",
            "short_paragraph",
            limit=40,
        )
        html = preview.render_context(context, "html")

        self.assertLessEqual(html.count('<article class="match">'), sum(preview.HTML_SECTION_LIMITS.values()))
        self.assertIn("Showing 8 of", html)

    def test_html_collapses_explore_and_excluded_as_diagnostic_sections(self):
        context = preview.build_preview_context(
            "I speak English and Spanish, no college degree, looking for remote beginner AI data tasks.",
            "short_paragraph",
            limit=20,
        )
        html = preview.render_context(context, "html")

        self.assertIn('<details class="diagnostic"><summary>Explore Only', html)
        self.assertIn('<details class="diagnostic"><summary>Excluded / Not Personalized', html)
        self.assertIn("broader browse and diagnostic results", html)

    def test_html_keeps_technical_diagnostics_collapsed(self):
        context = preview.build_preview_context(
            "Senior Software Engineer, 8 years. Python, TypeScript, React. Remote contract preferred.",
            "resume_or_linkedin_style",
            limit=4,
        )
        html = preview.render_context(context, "html")

        self.assertIn("<summary>Technical details</summary>", html)
        self.assertIn("Score:", html)
        self.assertIn("Metadata overlay:", html)
        self.assertNotIn(" pts</p>", html)

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
            all(match["preview_section"] in {"explore_only", "excluded"} for match in capped_flagged),
            capped_flagged[:3],
        )
        flagged = [
            match
            for section in ("excluded",)
            for match in context["matches"][section]
            if any("Detected unsupported language requirement" in diagnostic for diagnostic in match["preview_diagnostics"])
        ]
        self.assertTrue(flagged)

    def test_beginner_bilingual_caps_locale_specific_language_roles(self):
        context = preview.build_preview_context(
            "I speak English and Spanish, no college degree, looking for remote beginner AI data tasks.",
            "short_paragraph",
            limit=140,
        )

        locale_terms = (
            "english (malta)",
            "english (singapore)",
            "english (australia)",
            "english (us)",
            "spanish (andean",
            "spanish (colombia)",
            "spanish (chile)",
            "el salvador",
            "spanish (mexico)",
            "spanish (spain)",
        )
        primary = context["matches"]["do_these_first"] + context["matches"]["best_matches"]
        for term in locale_terms:
            matches = matches_with_title_terms(context, (term,))
            self.assertTrue(matches, term)
            self.assertFalse(any(match in primary for match in matches), term)
            self.assertTrue(
                any(
                    diagnostic.startswith("Specific language locale/accent may be required")
                    for match in matches
                    for diagnostic in match["preview_diagnostics"]
                ),
                term,
            )

        mexico_expert_matches = matches_with_title_terms(context, ("spanish language expert (mexico)",))
        self.assertTrue(mexico_expert_matches)
        self.assertFalse(any(match in context["matches"]["do_these_first"] for match in mexico_expert_matches))
        self.assertTrue(
            any(match["preview_section"] == "also_worth_reviewing" for match in mexico_expert_matches)
        )
        self.assertTrue(
            any(
                diagnostic.startswith("Specific language locale/accent may be required")
                for match in mexico_expert_matches
                for diagnostic in match["preview_diagnostics"]
            )
        )

        excluded_titles = " ".join(match["display_title"].lower() for match in context["matches"]["excluded"])
        self.assertIn("assamese", excluded_titles)
        self.assertIn("japanese", excluded_titles)
        self.assertIn("dutch", excluded_titles)

    def test_beginner_bilingual_caps_title_only_dialect_and_language_roles(self):
        context = preview.build_preview_context(
            "I speak English and Spanish, no college degree, looking for remote beginner AI data tasks.",
            "short_paragraph",
            limit=160,
        )

        primary = context["matches"]["do_these_first"] + context["matches"]["best_matches"]
        terms = (
            "alexandrian dialect",
            "bedawi dialect",
            "belarusian language specialist",
            "british sign language",
            "cebuano language specialist",
            "chichewa language specialist",
            "guaraní language expert",
            "guaraní language specialist",
            "k'iche' (mayan) language expert",
            "kaqchikel (mayan) language expert",
            "kurdish (kurmanji) language expert",
            "kurdish (sorani) language expert",
            "generalist - english & odia",
        )
        for term in terms:
            matches = matches_with_title_terms(context, (term,))
            self.assertTrue(matches, term)
            self.assertFalse(any(match in primary for match in matches), term)
            self.assertTrue(
                any(
                    diagnostic.startswith("Unsupported title-only language or dialect")
                    for match in matches
                    for diagnostic in match["preview_diagnostics"]
                ),
                term,
            )

        odia_matches = matches_with_title_terms(context, ("generalist - english & odia",))
        self.assertTrue(odia_matches)
        self.assertIn("Odia", preview.user_caution_note(odia_matches[0]))

        do_these_first_titles = " ".join(
            match["display_title"].lower()
            for match in context["matches"]["do_these_first"]
        )
        self.assertTrue(
            any(
                title in do_these_first_titles
                for title in (
                    "english language data contributor",
                    "spanish audio specialist",
                    "english language expert",
                )
            ),
            do_these_first_titles,
        )

        visible_titles = " ".join(
            match["display_title"].lower()
            for section in ("do_these_first", "best_matches", "also_worth_reviewing")
            for match in context["matches"][section]
        )
        self.assertIn("english language data contributor", visible_titles)
        self.assertIn("spanish audio specialist", visible_titles)
        self.assertIn("english language expert", visible_titles)

    def test_explicit_profile_locale_does_not_cap_matching_locale_roles(self):
        context = preview.build_preview_context(
            "I am fluent in English (US) and Spanish (Mexico), no college degree, and want remote AI data tasks.",
            "short_paragraph",
            limit=140,
        )

        for term in ("english (us)", "spanish (mexico)"):
            matches = matches_with_title_terms(context, (term,))
            self.assertTrue(matches, term)
            self.assertFalse(
                any(
                    diagnostic.startswith("Specific language locale/accent may be required")
                    for match in matches
                    for diagnostic in match["preview_diagnostics"]
                ),
                term,
            )

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

        primary_titles = " ".join(
            match["display_title"].lower()
            for match in context["matches"]["do_these_first"] + context["matches"]["best_matches"]
        )
        self.assertNotIn("pavement condition index", primary_titles)
        self.assertNotIn("building code & permitting specialists", primary_titles)
        self.assertNotIn("customer success engineer (india)", primary_titles)
        self.assertNotIn("customer success engineer (latam)", primary_titles)

        pci_match = guarded_synthetic_match(
            context["matcher_profile"],
            "Pavement Condition Index (PCI) Survey & Annotation Specialist",
            expertise="Data Annotation",
        )
        self.assertEqual(pci_match["preview_section"], "explore_only")
        self.assertTrue(
            any(
                diagnostic.startswith("Specialized annotation or survey domain does not match")
                for diagnostic in pci_match["preview_diagnostics"]
            )
        )

        regional_customer_success = matches_with_title_terms(
            context,
            ("customer success engineer (india)", "customer success engineer (latam)"),
        )
        self.assertTrue(regional_customer_success)
        self.assertTrue(
            all(match["preview_section"] == "explore_only" for match in regional_customer_success),
            regional_customer_success,
        )

        building_code = matches_with_title_terms(context, ("building code & permitting specialists",))
        self.assertTrue(building_code)
        self.assertTrue(all(match["preview_section"] == "explore_only" for match in building_code))
        self.assertTrue(
            any(
                diagnostic.startswith("Location or regional eligibility needs confirmation")
                for match in building_code
                for diagnostic in match["preview_diagnostics"]
            )
        )

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

        best_titles = " ".join(match["display_title"].lower() for match in context["matches"]["best_matches"])
        for term in ("advanced math", "computational chemistry", "computational physics", "material science"):
            self.assertNotIn(term, best_titles)

        capped_science = [
            match
            for match in all_preview_matches(context)
            if title_has_any(match, ("advanced math", "computational chemistry", "computational physics", "material science"))
        ]
        self.assertTrue(capped_science)
        self.assertTrue(
            any(
                diagnostic.startswith("Science subdomain appears outside profile specialty")
                for match in capped_science
                for diagnostic in match["preview_diagnostics"]
            )
        )

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

        credential_match = guarded_synthetic_match(
            context["matcher_profile"],
            "Medical Doctor Biology Review Specialist",
            expertise="Science",
        )
        self.assertIn("degree, seniority, or license", preview.user_caution_note(credential_match))

    def test_specificity_guardrail_cautions_are_user_facing(self):
        beginner = preview.build_preview_context(
            "I speak English and Spanish, no college degree, looking for remote beginner AI data tasks.",
            "short_paragraph",
            limit=140,
        )
        locale_match = matches_with_title_terms(beginner, ("english (us)",))[0]
        self.assertIn("specific language locale or accent", preview.user_caution_note(locale_match))

        software = preview.build_preview_context(
            "Senior Software Engineer, 8 years. Python, TypeScript, React, APIs, test automation. "
            "I don't have biology or medical credentials. Looking for remote AI coding evaluator work.",
            "resume_or_linkedin_style",
            limit=120,
        )
        pci_match = guarded_synthetic_match(
            software["matcher_profile"],
            "Pavement Condition Index (PCI) Survey & Annotation Specialist",
            expertise="Data Annotation",
        )
        self.assertIn("specialized annotation task", preview.user_caution_note(pci_match))

        science_python = guarded_synthetic_match(
            software["matcher_profile"],
            "Material Science Expert with Python",
            expertise="STEM",
        )
        self.assertIn("domain expertise", preview.user_caution_note(science_python))

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


def matches_with_title_terms(context, terms):
    return [
        match
        for match in all_preview_matches(context)
        if title_has_any(match, terms)
    ]


def guarded_synthetic_match(profile, title, expertise="Unknown", location="Remote"):
    row = {
        "title": title,
        "canonical_title": title,
        "source_category": expertise,
        "department": expertise,
        "expertise": expertise,
        "description": "",
        "location": location,
    }
    match = {
        "score": 30,
        "display_title": title,
        "source": "Synthetic",
        "source_slug": "synthetic",
        "location": location,
        "expertise": expertise,
        "url": "",
        "effective_product_section": "best_matches",
        "eligible_for_personalized": True,
        "preview_diagnostics": [],
        "reasons": ["Generalist AI-work signal"],
    }
    return preview.apply_preview_guardrails(profile, row, match)


if __name__ == "__main__":
    unittest.main()
