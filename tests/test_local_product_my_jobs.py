import re
import unittest

import scripts.local_product_app as app


VALID_STATUSES = (
    "recommended",
    "saved",
    "remind_later",
    "applied",
    "waiting",
    "assessment_invited",
    "assessment_started",
    "assessment_completed",
    "accepted",
    "active_worker",
    "paid_task_received",
    "not_interested",
    "rejected",
    "expired",
)


def record(status, index=1, *, reminder_date=""):
    return {
        "id": index,
        "pipeline_item_id": f"pipeline-{index}",
        "profile_id": "local_user",
        "source": "Fixture Source",
        "title": f"Fixture Opportunity {index}",
        "url": f"https://example.test/jobs/{index}",
        "status": status,
        "status_date": "2026-07-12",
        "notes": "",
        "user_priority": "medium",
        "reminder_date": reminder_date,
        "last_user_action": "",
        "updated_at": f"2026-07-12 12:00:{index:02d}",
        "match_score": None,
        "next_action": app.lightweight_next_action({"status": status}),
    }


def match_for_record(item):
    return {
        "source": item["source"],
        "display_title": item["title"],
        "url": item["url"],
    }


class MyJobsStateModelTests(unittest.TestCase):
    def test_exact_status_labels_cover_every_valid_status(self):
        self.assertEqual(
            app.STATUS_LABELS,
            {
                "recommended": "Recommended",
                "saved": "Saved",
                "remind_later": "Saved",
                "applied": "Applied",
                "waiting": "Waiting for update",
                "assessment_invited": "Assessment ready",
                "assessment_started": "Assessment in progress",
                "assessment_completed": "Waiting for result",
                "accepted": "Accepted",
                "active_worker": "Active",
                "paid_task_received": "Paid task received",
                "not_interested": "Not interested",
                "rejected": "Not selected",
                "expired": "No longer available",
            },
        )
        self.assertEqual(set(app.STATUS_LABELS), set(VALID_STATUSES))

    def test_exact_action_labels_and_transitions_remain_stable(self):
        self.assertEqual(
            app.ACTION_LABELS,
            {
                "show_again": "Show again",
                "save": "Save",
                "applied": "Mark as applied",
                "assessment_started": "Mark assessment started",
                "assessment_completed": "Mark assessment complete",
                "remind_later": "Remind me in 7 days",
                "not_interested": "Not interested",
                "accepted": "Mark as accepted",
                "rejected": "Mark as not selected",
            },
        )
        self.assertEqual(
            app.ACTION_STATUSES,
            {
                "show_again": "saved",
                "save": "saved",
                "applied": "applied",
                "assessment_started": "assessment_started",
                "assessment_completed": "assessment_completed",
                "remind_later": "remind_later",
                "not_interested": "not_interested",
                "accepted": "accepted",
                "rejected": "rejected",
            },
        )
        self.assertEqual(
            app.STATUS_ACTIONS,
            {
                None: ("save", "applied", "not_interested"),
                "recommended": ("save", "applied", "not_interested"),
                "saved": ("applied", "remind_later", "not_interested"),
                "remind_later": ("applied", "not_interested"),
                "applied": ("assessment_started", "remind_later", "not_interested"),
                "waiting": ("assessment_started", "remind_later", "not_interested"),
                "assessment_invited": ("assessment_started", "remind_later", "not_interested"),
                "assessment_started": ("assessment_completed", "remind_later", "not_interested"),
                "assessment_completed": ("remind_later", "accepted", "rejected"),
                "accepted": (),
                "active_worker": (),
                "paid_task_received": (),
                "rejected": (),
                "not_interested": ("show_again",),
                "expired": (),
            },
        )

    def test_identical_action_labels_render_across_matches_and_my_jobs(self):
        for status in VALID_STATUSES:
            item = record(status)
            matches_html = app.render_preview_full_forms(
                match_for_record(item),
                item,
                "run-labels",
                "card-labels",
                "best_matches",
            )
            jobs_html = app.render_my_jobs_forms(item, "run-labels", "card-labels")
            for action in app.actions_for_status(status):
                label = app.ACTION_LABELS[action]
                with self.subTest(status=status, action=action):
                    self.assertIn(f">{label}</button>", matches_html)
                    self.assertIn(f">{label}</button>", jobs_html)

    def test_every_status_renders_as_a_non_clickable_current_state(self):
        for index, status in enumerate(VALID_STATUSES, start=1):
            html = app.render_my_jobs_card(record(status, index), "run-statuses")
            label = app.STATUS_LABELS[status]
            with self.subTest(status=status):
                self.assertIn("Current status:", html)
                self.assertIn(label, html)
                self.assertNotIn(f">{label}</button>", html)

    def test_unknown_status_is_visible_and_read_only(self):
        item = record("future_state")
        html = app.render_my_jobs_card(item, "run-unknown")

        self.assertEqual(app.actions_for_status("future_state"), ())
        self.assertIn("Status unavailable", html)
        self.assertNotIn("js-inline-action", html)
        self.assertIn("View job", html)

    def test_terminal_states_have_no_workflow_actions(self):
        for status in ("accepted", "active_worker", "paid_task_received", "rejected", "expired"):
            html = app.render_my_jobs_card(record(status), "run-terminal")
            with self.subTest(status=status):
                self.assertNotIn("js-inline-action", html)
                self.assertIn("View job", html)
        self.assertIn(
            "This job is no longer available.",
            app.render_my_jobs_card(record("expired"), "run-terminal"),
        )

    def test_reminder_status_uses_saved_badge_and_date_metadata(self):
        html = app.render_my_jobs_card(
            record("remind_later", reminder_date="2026-07-19"),
            "run-reminder",
        )

        self.assertIn("Current status: </span>Saved", html)
        self.assertIn("Reminder set for 2026-07-19.", html)
        self.assertIn("Mark as applied", html)

    def test_filter_mapping_and_hidden_exclusion(self):
        records = [record(status, index) for index, status in enumerate(VALID_STATUSES, start=1)]

        self.assertNotIn("not_interested", {item["status"] for item in app.tracker_records_for_view(records, "all")})
        self.assertEqual(
            {item["status"] for item in app.tracker_records_for_view(records, "saved")},
            {"recommended", "saved", "remind_later"},
        )
        self.assertEqual(
            {item["status"] for item in app.tracker_records_for_view(records, "in_progress")},
            app.TRACKER_FILTER_STATUSES["in_progress"],
        )
        self.assertEqual(
            {item["status"] for item in app.tracker_records_for_view(records, "active")},
            app.ACCEPTED_STATUSES,
        )
        self.assertEqual(
            {item["status"] for item in app.tracker_records_for_view(records, "closed")},
            app.CLOSED_STATUSES,
        )
        self.assertEqual(
            [item["status"] for item in app.tracker_records_for_view(records, "hidden")],
            ["not_interested"],
        )

    def test_workspace_filters_preserve_run_and_expose_hidden_accessibly(self):
        records = [record("saved", 1), record("not_interested", 2)]
        html = app.render_my_jobs_workspace(records, "run-filter", "saved")

        self.assertIn('aria-label="Filter My Jobs"', html)
        self.assertIn('/tracker?run=run-filter&amp;view=saved', html)
        self.assertIn('aria-current="true">Saved</a>', html)
        self.assertIn("Show hidden (1)", html)
        self.assertNotIn("Fixture Opportunity 2", html)

        hidden = app.render_my_jobs_workspace(records, "run-filter", "hidden")
        self.assertIn("Fixture Opportunity 2", hidden)
        self.assertIn("Show again", hidden)

    def test_empty_workspace_and_empty_filter_have_distinct_copy(self):
        empty = app.render_my_jobs_workspace([], "run-empty")
        filtered = app.render_my_jobs_workspace([record("saved")], "run-filtered", "active")

        self.assertIn("You haven&apos;t saved any jobs yet.", empty)
        self.assertIn("Find matches", empty)
        self.assertIn("/find-matches?run=run-empty", empty)
        self.assertIn("No jobs in this view.", filtered)

    def test_my_jobs_removes_internal_copy_and_repeated_navigation(self):
        context = {
            "profile": {"profile_id": "local_user", "display_name": "My Profile"},
            "records": [record("saved")],
        }
        html = app.render_lightweight_tracker(context, "run-clean")

        self.assertIn("<h1>My Jobs</h1>", html)
        self.assertIn("Track saved jobs, applications, assessments, and follow-ups.", html)
        for forbidden in (
            "Application Tracker",
            "Active tracker profile",
            "local profile",
            "tracker status",
            "Open full opportunity dashboard",
            "Back to Matches",
            ">Open<",
        ):
            self.assertNotIn(forbidden, html)

    def test_card_ids_are_unique_and_mobile_structure_is_bounded(self):
        records = [record("saved", 1), record("applied", 2), record("expired", 3)]
        html = app.render_my_jobs_workspace(records, "run-layout")
        ids = re.findall(r'<article[^>]+id="([^"]+)"', html)

        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(".card.my-job-card", app.CSS)
        self.assertIn("overflow-x: auto", app.CSS)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", app.CSS)
        self.assertIn("min-height: 44px", app.CSS)

    def test_feedback_and_match_count_presentation_copy(self):
        script = app.render_inline_action_script()
        context = {
            "matches": {section: [] for section in app.profile_preview.SECTION_ORDER},
        }
        trusted = {
            "source": "Fixture",
            "display_title": "Trusted role",
            "title": "Trusted role",
            "url": "https://example.test/trusted",
            "location": "Remote",
            "expertise": "General",
            "primary_recommendation_eligible": True,
            "affirmative_fit_status": "supported",
            "opportunity_trust_status": "trusted",
        }
        refreshing = dict(trusted, display_title="Refreshing role", title="Refreshing role")
        refreshing["opportunity_trust_status"] = "stale_source"
        context["matches"]["best_matches"] = [trusted, refreshing]

        header = app.render_preview_results_header(context)

        self.assertIn("We couldn't update this job. Try again.", script)
        self.assertIn('<p class="results-summary"><strong>1 verified match</strong></p>', header)
        self.assertIn('<p class="refresh-summary">1 more match is being refreshed.</p>', header)


if __name__ == "__main__":
    unittest.main()
