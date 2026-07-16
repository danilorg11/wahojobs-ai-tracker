import unittest

import scripts.local_product_app as app
from scripts.product_demo_report import build_tracked_index
from tests.test_local_product_ranked_matches import make_context, make_match


class LocalProductVisualMilestoneTests(unittest.TestCase):
    def render_empty(self):
        return app.render_profile_preview_page(
            input_text="",
            input_style="short_paragraph",
            match_run_id="",
            demo_mode=False,
        )

    def render_results(self, context, tracked=None):
        stable_id = 1
        for section in app.profile_preview.SECTION_ORDER:
            for match in context["matches"].get(section, []):
                match["job_id"] = stable_id
                match["canonical_opportunity_id"] = stable_id
                match.setdefault("opportunity_trust", {})["selected_variant_id"] = stable_id
                stable_id += 1
        return app.render_profile_preview_page(
            input_text="English and Spanish reviewer",
            input_style="short_paragraph",
            context=context,
            match_run_id="run-visual",
            demo_mode=False,
            tracked=tracked,
        )

    def test_empty_matches_page_has_one_clear_onboarding_intro(self):
        page = self.render_empty()

        self.assertEqual(page.count('class="page-intro onboarding-intro"'), 1)
        self.assertIn("Find AI work that fits you", page)
        self.assertIn(
            "Tell us about your background. We'll show the best current opportunities for you.",
            page,
        )
        self.assertIn('aria-describedby="profile-input-help"', page)
        self.assertIn("Find my matches", page)
        self.assertNotIn('<div class="profile-box">', page)
        self.assertNotIn("Paste your background to see a focused set", page)

    def test_populated_page_uses_results_header_and_dynamic_counts(self):
        context = make_context({"best_matches": 1})
        stale = make_match("Verification overdue role")
        stale["opportunity_trust_status"] = "stale_source"
        stale["primary_recommendation_eligible"] = False
        stale["opportunity_trust"]["source_age_hours"] = 200
        context["matches"]["best_matches"].append(stale)

        page = self.render_results(context)

        self.assertIn("Your matches", page)
        self.assertIn("1 match", page)
        self.assertNotIn("source verification", page.lower())
        self.assertNotIn("recently cached", page.lower())
        self.assertNotIn("Find AI work that fits you", page)
        self.assertEqual(page.count('class="match-rank"'), 1)

    def test_results_header_shows_only_total_usable_count(self):
        context = make_context({"best_matches": 2})
        for title in ("Verification overdue one", "Verification overdue two"):
            match = make_match(title)
            match["opportunity_trust_status"] = "stale_source"
            match["primary_recommendation_eligible"] = False
            match["opportunity_trust"]["source_age_hours"] = 100
            context["matches"]["also_worth_reviewing"].append(match)

        page = self.render_results(context)

        self.assertIn("4 matches", page)
        self.assertNotIn("verified match", page.lower())
        self.assertNotIn("recently cached", page.lower())
        self.assertNotIn("source verification", page.lower())

    def test_profile_summary_omits_unavailable_values(self):
        context = make_context({"best_matches": 1})
        context["canonical_profile"]["location"] = {
            "country": None,
            "region": None,
            "city": None,
        }

        page = self.render_results(context)

        self.assertIn('class="profile-summary-line"', page)
        self.assertNotIn("Not specified", page)
        self.assertNotIn("Location: None", page)

    def test_ranked_card_preserves_action_hooks_and_hides_generic_status(self):
        context = make_context({"do_these_first": 1})

        page = self.render_results(context)

        self.assertIn('id="ranked-', page)
        self.assertIn("data-action-card", page)
        self.assertIn('class="js-inline-action action-form', page)
        self.assertIn('name="match_run_id" value="run-visual"', page)
        self.assertIn('name="opportunity_key"', page)
        self.assertIn('class="js-card-controls"', page)
        self.assertIn('class="pill card-status js-card-status"></p>', page)
        self.assertNotIn("Ready to review", page)
        self.assertIn('class="open button-primary"', page)

    def test_real_tracked_status_remains_visible(self):
        context = make_context({"best_matches": 1})
        match = context["matches"]["best_matches"][0]
        record = {
            "id": 7,
            "match_key": match["url"].lower(),
            "source": match["source"],
            "title": match["display_title"],
            "url": match["url"],
            "status": "saved",
            "reminder_date": "",
        }

        page = self.render_results(context, build_tracked_index([record]))

        self.assertIn('class="pill card-status js-card-status">Saved</p>', page)

    def test_navigation_and_my_jobs_copy_remain_available(self):
        nav = app.render_product_nav("run-visual", current="matches")
        tracker = app.render_lightweight_tracker_header([])

        self.assertIn("Wahojobs", nav)
        self.assertIn("Matches", nav)
        self.assertIn("My Jobs", nav)
        self.assertIn("Track saved jobs, applications, assessments, and follow-ups.", tracker)
        self.assertNotIn("Application Tracker", tracker)

    def test_css_locks_ranked_grid_focus_and_mobile_controls(self):
        self.assertIn(".card.ranked-card", app.CSS)
        self.assertIn("grid-template-columns: 44px minmax(0, 1fr) 176px", app.CSS)
        self.assertIn("button:focus-visible", app.CSS)
        self.assertIn("min-height: 44px", app.CSS)
        self.assertIn("--focus: #2563EB", app.CSS)


if __name__ == "__main__":
    unittest.main()
