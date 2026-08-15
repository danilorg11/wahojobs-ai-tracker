import unittest
from unittest import mock

import scripts.local_product_app as app
from wahojobs import authenticated_profile_matches
from wahojobs.profiles.normalizer import BaselineHeuristicProfileNormalizer


def make_match(title, source="Fixture Source"):
    return {
        "source": source,
        "display_title": title,
        "title": title,
        "url": f"https://example.test/{title.lower().replace(' ', '-')}",
        "location": "Remote",
        "expertise": "AI evaluation",
        "score": 30,
        "reasons": ["Relevant evaluation background"],
        "preview_diagnostics": [],
        "primary_recommendation_eligible": True,
        "actionability_cap_reasons": [],
        "affirmative_fit_status": "supported",
        "opportunity_trust_status": "trusted",
        "opportunity_trust_reasons": [],
        "opportunity_trust": {
            "status": "trusted",
            "reasons": [],
            "job_is_active": True,
            "canonical_is_active": True,
            "selected_variant_id": 1,
        },
        "affirmative_fit_why": ["Your relevant experience aligns with this opportunity."],
        "affirmative_fit": {
            "required_groups": [],
            "satisfied_groups": ["Relevant evaluation work"],
            "supported_evidence": [],
            "adjacencies_used": [],
            "missing_requirements": [],
            "unmodeled_requirements": [],
            "conflicting_requirements": [],
            "location_and_locale_evidence": [],
            "why_fit_statements": ["Your relevant experience aligns with this opportunity."],
        },
    }


def make_context(section_counts=None):
    section_counts = section_counts or {}
    normalization = BaselineHeuristicProfileNormalizer().normalize(
        "I speak English and Spanish and want remote AI evaluation work.",
        "short_paragraph",
        {"profile_id": "preview_profile", "display_name": "Preview Profile"},
    )
    matches = {section: [] for section in app.profile_preview.SECTION_ORDER}
    for section, count in section_counts.items():
        matches[section] = [
            make_match(f"{section} role {index + 1}")
            for index in range(count)
        ]
    return {
        "canonical_profile": normalization.canonical_profile,
        "matches": matches,
        "warnings": normalization.warnings,
        "missing_fields": normalization.missing_fields,
        "ambiguous_fields": normalization.ambiguous_fields,
        "normalizer": "baseline",
        "extraction_quality": normalization.extraction_quality,
        "metadata_overlay": {"enabled": False, "records_loaded": 0, "rows_enriched": 0},
    }


class RankedPresentationTests(unittest.TestCase):
    def test_persisted_results_render_one_continuous_ranked_list(self):
        ranked_matches = []
        for index, section in enumerate(app.ACTIONABLE_PRESENTATION_SECTIONS, start=1):
            match = make_match(f"Ranked opportunity {index}")
            match["presentation_rank"] = index
            match["presentation_source_section"] = section
            match["affirmative_fit_why"] = [f"Fit explanation {index}."]
            ranked_matches.append(match)

        with mock.patch.object(
            app,
            "build_browser_presentation_matches",
            return_value=ranked_matches,
        ):
            page = authenticated_profile_matches._render_match_results(
                {}, inventory_count=3
            )

        self.assertEqual(page.count("class='match-list'"), 1)
        self.assertEqual(page.count("class='match-card'"), 3)
        positions = [
            page.index(f"Ranked opportunity {index}")
            for index in range(1, 4)
        ]
        self.assertEqual(positions, sorted(positions))
        for heading in (
            "Do These First",
            "Best Matches",
            "Also Worth Reviewing",
        ):
            self.assertNotIn(heading, page)
        for index in range(1, 4):
            self.assertIn(f"Fit explanation {index}.", page)

    def test_ranked_matches_preserve_actionable_section_order_and_cap_at_ten(self):
        context = make_context(
            {
                "do_these_first": 3,
                "best_matches": 5,
                "also_worth_reviewing": 5,
                "explore_only": 4,
                "excluded": 2,
            }
        )

        ranked = app.build_ranked_presentation_matches(context)

        self.assertEqual(len(ranked), 10)
        self.assertEqual(
            [match["presentation_rank"] for match in ranked],
            list(range(1, 11)),
        )
        self.assertEqual(
            [match["presentation_source_section"] for match in ranked],
            ["do_these_first"] * 3 + ["best_matches"] * 5 + ["also_worth_reviewing"] * 2,
        )
        self.assertFalse(
            any(
                match["presentation_source_section"] in {"explore_only", "excluded"}
                for match in ranked
            )
        )

    def test_ranked_matches_do_not_pad_a_short_actionable_list(self):
        context = make_context(
            {
                "best_matches": 2,
                "explore_only": 12,
                "excluded": 12,
            }
        )

        ranked = app.build_ranked_presentation_matches(context)

        self.assertEqual([match["display_title"] for match in ranked], [
            "best_matches role 1",
            "best_matches role 2",
        ])

    def test_guardrail_demotions_do_not_pad_primary_list_but_natural_also_rows_do(self):
        context = make_context()
        primary = make_match("Natural best")
        natural_also = make_match("Natural also")
        demoted = make_match("Locale-capped role")
        demoted["primary_recommendation_eligible"] = False
        demoted["actionability_cap_reasons"] = ["unconfirmed_language_locale"]
        context["matches"]["best_matches"] = [primary]
        context["matches"]["also_worth_reviewing"] = [demoted, natural_also]

        ranked = app.build_ranked_presentation_matches(context)

        self.assertEqual(
            [match["display_title"] for match in ranked],
            ["Natural best", "Natural also"],
        )
        self.assertEqual([match["presentation_rank"] for match in ranked], [1, 2])

    def test_demo_qa_retains_omitted_title_while_normal_ranked_list_hides_it(self):
        context = make_context({"best_matches": 1})
        omitted = make_match("English (Australia) Audio Evaluator")
        omitted["primary_recommendation_eligible"] = False
        omitted["actionability_cap_reasons"] = ["unconfirmed_language_locale"]
        context["matches"]["also_worth_reviewing"] = [omitted]

        demo_page = app.render_profile_preview_page(
            input_text="English and Spanish reviewer",
            input_style="short_paragraph",
            sample_id="beginner_bilingual",
            context=context,
            owner_profile_id="beginner_bilingual_no_degree",
            match_run_id="run-demo",
            demo_mode=True,
        )
        normal_page = app.render_profile_preview_page(
            input_text="English and Spanish reviewer",
            input_style="short_paragraph",
            context=context,
            match_run_id="run-normal",
            demo_mode=False,
        )

        self.assertIn("Omitted from primary list", demo_page)
        self.assertIn("English (Australia) Audio Evaluator", demo_page)
        self.assertNotIn("English (Australia) Audio Evaluator", normal_page)
        self.assertEqual(demo_page.count('class="match-rank"'), 1)

    def test_normal_results_render_one_ranked_list_without_internal_bucket_copy(self):
        context = make_context(
            {
                "do_these_first": 2,
                "best_matches": 2,
                "also_worth_reviewing": 1,
                "explore_only": 3,
                "excluded": 1,
            }
        )

        page = app.render_profile_preview_page(
            input_text="English and Spanish reviewer",
            input_style="short_paragraph",
            context=context,
            match_run_id="run-1",
            demo_mode=False,
        )

        self.assertIn("Your matches", page)
        self.assertEqual(page.count('class="match-rank"'), 5)
        self.assertIn('aria-label="Rank 1">#1</div>', page)
        self.assertIn('aria-label="Rank 5">#5</div>', page)
        self.assertIn("do_these_first role 1", page)
        self.assertNotIn("explore_only role 1", page)
        for forbidden in (
            "Do These First",
            "Best Matches",
            "Also Worth Reviewing",
            "Explore Only",
            "Excluded",
            "Advanced QA parser mode",
            "Tracking profile",
            "Score:",
        ):
            self.assertNotIn(forbidden, page)

    def test_demo_personas_share_ranked_view_and_keep_collapsed_qa_details(self):
        context = make_context({"do_these_first": 1, "explore_only": 2})

        for persona in app.PREVIEW_SAMPLES:
            with self.subTest(persona=persona):
                page = app.render_profile_preview_page(
                    input_text=app.PREVIEW_SAMPLES[persona]["text"],
                    input_style=app.PREVIEW_SAMPLES[persona]["style"],
                    sample_id=persona,
                    context=context,
                    owner_profile_id=app.PREVIEW_SAMPLES[persona]["owner_profile_id"],
                    match_run_id=f"run-{persona}",
                    demo_mode=True,
                )
                self.assertIn("Your matches", page)
                self.assertIn("<summary><span>QA details</span>", page)
                self.assertIn("Do These First", page)
                self.assertIn("Explore Only", page)
                self.assertNotIn("explore_only role 1</h3>", page)


if __name__ == "__main__":
    unittest.main()
