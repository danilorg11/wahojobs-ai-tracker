import http.client
import json
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlparse

import scripts.local_product_app as app
import scripts.product_state as product_state
from wahojobs import pipeline_actions, pipeline_reconciliation, pipeline_state
from wahojobs.profiles.normalizer import BaselineHeuristicProfileNormalizer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pipeline_state_migration as pipeline_migration  # noqa: E402


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self._current = {"attrs": attrs, "fields": {}, "buttons": []}
            self.forms.append(self._current)
        elif self._current is not None and tag == "input":
            name = attrs.get("name")
            if name:
                self._current["fields"][name] = attrs.get("value", "")
        elif self._current is not None and tag == "button":
            self._current["buttons"].append(attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self._current = None


class LocalProductAppFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "wahojobs-test.sqlite"
        self._initialize_database()
        self.connection_patch = mock.patch.object(app, "get_connection", self.connect)
        self.product_connection_patch = mock.patch.object(
            product_state,
            "get_connection",
            self.connect,
        )
        self.demo_connection_patch = mock.patch.object(
            app.demo,
            "get_connection",
            self.connect,
        )
        self.preview_patch = mock.patch.object(
            app,
            "build_cached_preview_context",
            side_effect=self.preview_context,
        )
        self.signature_patch = mock.patch.object(
            app,
            "preview_data_signature",
            return_value=("test-data",),
        )
        self.connection_patch.start()
        self.product_connection_patch.start()
        self.demo_connection_patch.start()
        self.preview_patch.start()
        self.signature_patch.start()
        app.seed_local_product_profiles()
        self._seed_cross_profile_owner()
        self.registry = app.MatchRunRegistry(max_size=8)
        self.server = None
        self.thread = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.signature_patch.stop()
        self.preview_patch.stop()
        self.product_connection_patch.stop()
        self.demo_connection_patch.stop()
        self.connection_patch.stop()
        self.temp_dir.cleanup()

    def _initialize_database(self):
        schema_path = Path(__file__).resolve().parents[1] / "wahojobs" / "db" / "schema.sql"
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "wahojobs"
            / "db"
            / "migrations"
            / "001_pipeline_state.sql"
        )
        conn = self.connect()
        try:
            conn.executescript(schema_path.read_text(encoding="utf-8"))
            for statement in pipeline_migration.iter_sql_statements(
                migration_path.read_text(encoding="utf-8")
            ):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO wahojobs_schema_migrations (version) VALUES (?)",
                (pipeline_migration.MIGRATION_VERSION,),
            )
            conn.commit()
        finally:
            conn.close()

    def _seed_cross_profile_owner(self):
        profile = product_state.built_in_profiles_by_id()["portuguese_english_reviewer"]
        with self.connect() as conn:
            product_state.upsert_profile(conn, profile)

    def connect(self):
        conn = sqlite3.connect(
            self.db_path,
            timeout=10,
            factory=ClosingConnection,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def preview_context(raw_input, input_style, limit, _signature):
        normalization = BaselineHeuristicProfileNormalizer().normalize(
            raw_input,
            input_style,
            {"profile_id": "preview_profile", "display_name": "Preview Profile"},
        )
        lowered = raw_input.lower()
        if "software" in lowered or "python" in lowered:
            title = "Python Coding Evaluator"
            expertise = "Software"
        elif "biology" in lowered or "microbiology" in lowered:
            title = "Microbiology Specialist"
            expertise = "Biology"
        elif "spanish" in lowered:
            title = "Spanish Audio Reviewer"
            expertise = "Language"
        else:
            title = "General AI Reviewer"
            expertise = "General"
        match = {
            "source": "Fixture Source",
            "display_title": title,
            "title": title,
            "url": f"https://example.test/{title.lower().replace(' ', '-')}",
            "location": "Remote",
            "expertise": expertise,
            "score": 30,
            "reasons": ["Relevant background signal"],
            "preview_diagnostics": [],
            "primary_recommendation_eligible": True,
            "primary_admission_source": "affirmative_fit_supported",
            "primary_admission_reasons": [],
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
            "affirmative_fit_why": ["Your relevant background aligns with this opportunity."],
            "affirmative_fit": {
                "required_groups": [],
                "satisfied_groups": ["Relevant background"],
                "supported_evidence": [],
                "adjacencies_used": [],
                "missing_requirements": [],
                "unmodeled_requirements": [],
                "conflicting_requirements": [],
                "location_and_locale_evidence": [],
                "why_fit_statements": ["Your relevant background aligns with this opportunity."],
            },
        }
        matches = {section: [] for section in app.profile_preview.SECTION_ORDER}
        matches["do_these_first"] = [match]
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

    def start_server(self, demo_mode=False):
        handler = app.make_handler(registry=self.registry, demo_mode=demo_mode)
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method, path, fields=None, accept=None, extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        headers = {}
        body = None
        if fields is not None:
            body = urlencode(fields)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if accept:
            headers["Accept"] = accept
        headers.update(extra_headers or {})
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read().decode("utf-8")
        result = (response.status, dict(response.getheaders()), payload)
        connection.close()
        return result

    def request_pairs(self, path, pairs, *, wants_json=True):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=10
        )
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if wants_json:
            headers.update(
                {
                    "Accept": "application/json",
                    app.INLINE_ACTION_HEADER: "1",
                }
            )
        connection.request("POST", path, body=urlencode(pairs), headers=headers)
        response = connection.getresponse()
        payload = response.read().decode("utf-8")
        result = (response.status, dict(response.getheaders()), payload)
        connection.close()
        return result

    def create_run(self, fields):
        status, headers, _ = self.request("POST", "/find-matches", fields)
        self.assertEqual(status, 303)
        location = headers["Location"]
        run_id = parse_qs(urlparse(location).query)["run"][0]
        return run_id

    def first_action(self, html, action):
        parser = FormParser()
        parser.feed(html)
        for form in parser.forms:
            if form["attrs"].get("action") == "/action" and form["fields"].get("action") == action:
                return dict(form["fields"])
        self.fail(f"No {action} action form found.")

    def pipeline_rows(self, profile_id):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM user_pipeline_items WHERE profile_id = ? ORDER BY id",
                (profile_id,),
            ).fetchall()

    def seed_legacy_pipeline_item(self, status, reminder_date=""):
        pipeline_item_id = f"legacy-browser-{status}-{len(self.pipeline_rows('local_user')) + 1}"
        with self.connect() as conn:
            profile = product_state.require_profile(conn, "local_user")
            conn.execute(
                """
                INSERT INTO user_pipeline_items (
                  pipeline_item_id,user_id,profile_id,source,opportunity_title,
                  opportunity_url,status,status_date,user_priority,reminder_date,
                  notes,last_user_action,is_sample
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)
                """,
                (
                    pipeline_item_id,
                    profile["user_id"],
                    "local_user",
                    "Legacy Fixture",
                    f"Legacy {status}",
                    f"https://example.test/legacy/{status}",
                    status,
                    "2026-07-01",
                    "medium",
                    reminder_date,
                    "",
                    "",
                ),
            )
            pipeline_state.backfill_legacy_pipeline_state(conn, dry_run=False)
        return pipeline_item_id

    @staticmethod
    def action_names(html):
        parser = FormParser()
        parser.feed(html)
        return [
            form["fields"].get("action")
            for form in parser.forms
            if form["attrs"].get("action") == "/action"
        ]

    def assert_unique_card_ids(self, html):
        card_ids = re.findall(r'<article[^>]+id="([^"]+)"[^>]+data-action-card', html)
        self.assertTrue(card_ids)
        self.assertEqual(len(card_ids), len(set(card_ids)))

    def card_html(self, html, title):
        cards = re.findall(
            r'<article\b[^>]*data-action-card[^>]*>.*?</article>',
            html,
            flags=re.DOTALL,
        )
        matching = [card for card in cards if f"<h3>{title}</h3>" in card]
        self.assertEqual(len(matching), 1, f"Expected one action card for {title}.")
        return matching[0]

    def create_applied_dashboard_item(self, input_text="Software engineer seeking remote AI work."):
        run_id = self.create_run(
            {"input_text": input_text, "input_style": "short_paragraph"}
        )
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        payload = json.loads(
            self.request(
                "POST",
                "/action",
                self.first_action(html, "applied"),
                "application/json",
                {app.INLINE_ACTION_HEADER: "1"},
            )[2]
        )
        return run_id, payload["pipeline_item_id"]

    @staticmethod
    def replace_transition_fields(conn, transition_id, **fields):
        conn.execute("DROP TRIGGER trg_user_pipeline_transitions_no_update")
        assignments = ", ".join(f"{field}=?" for field in fields)
        conn.execute(
            f"UPDATE user_pipeline_transitions SET {assignments} WHERE transition_id=?",
            (*fields.values(), transition_id),
        )
        conn.executescript(
            """
            CREATE TRIGGER trg_user_pipeline_transitions_no_update
            BEFORE UPDATE ON user_pipeline_transitions
            BEGIN SELECT RAISE(ABORT, 'user_pipeline_transitions is append-only'); END;
            """
        )

    def assert_dashboard_corruption_blocks(self, corrupt, expected_reason):
        self.start_server(demo_mode=False)
        run_id, item_id = self.create_applied_dashboard_item()
        with self.connect() as conn:
            corrupt(conn, item_id)
        with self.connect() as conn:
            report = pipeline_reconciliation.reconcile_pipeline_state(conn)
        self.assertFalse(report["safe_for_normalized_reads"])
        self.assertIn(expected_reason, report["normalized_read_blocking_reasons"])
        before = self.db_path.read_bytes()
        for route in ("/dashboard", "/market-dashboard"):
            with self.subTest(route=route):
                status, _, body = self.request("GET", f"{route}?run={run_id}")
                self.assertEqual(status, 503)
                self.assertIn("needs reconciliation", body)
                self.assertNotIn("Python Coding Evaluator", body)
        self.assertEqual(self.db_path.read_bytes(), before)

    def create_tracked_run(self, input_text, starting_status):
        run_id = self.create_run(
            {"input_text": input_text, "input_style": "short_paragraph"}
        )
        _, _, html = self.request("GET", f"/find-matches?run={run_id}")
        initial_action = "save" if starting_status == "saved" else "applied"
        fields = self.first_action(html, initial_action)
        status, _, _ = self.request(
            "POST",
            "/action",
            fields,
            "application/json",
            {app.INLINE_ACTION_HEADER: "1"},
        )
        self.assertEqual(status, 200)
        if starting_status == "assessment_started":
            _, _, html = self.request("GET", f"/find-matches?run={run_id}")
            fields = self.first_action(html, "assessment_started")
            status, _, _ = self.request(
                "POST",
                "/action",
                fields,
                "application/json",
                {app.INLINE_ACTION_HEADER: "1"},
            )
            self.assertEqual(status, 200)
        _, _, html = self.request("GET", f"/find-matches?run={run_id}")
        self.assertEqual(self.pipeline_rows("local_user")[-1]["status"], starting_status)
        return run_id, html

    def assert_hidden_navigation(self, run_id, title):
        for view in ("all", "saved", "in_progress", "active", "closed"):
            suffix = "" if view == "all" else f"&view={view}"
            status, _, html = self.request("GET", f"/tracker?run={run_id}{suffix}")
            self.assertEqual(status, 200)
            self.assertIn("Show hidden (1)", html)
            self.assertIn(f"/tracker?run={run_id}&amp;view=hidden", html)
            self.assertNotIn(title, html)

        status, _, hidden = self.request("GET", f"/tracker?run={run_id}&view=hidden")
        self.assertEqual(status, 200)
        self.assertIn("Hidden (1)", hidden)
        self.assertIn(title, hidden)
        self.assertIn("Not interested", hidden)
        self.assertIn("View job", hidden)
        self.assertEqual(self.action_names(self.card_html(hidden, title)), ["show_again"])
        self.assert_unique_card_ids(hidden)
        return hidden

    def assert_restored_my_jobs(self, run_id, title, expected_status="saved"):
        status, _, html = self.request("GET", f"/tracker?run={run_id}")
        self.assertEqual(status, 200)
        self.assertIn(title, html)
        self.assertIn(app.readable_status(expected_status), html)
        self.assertIn("View job", html)
        card = self.card_html(html, title)
        self.assertEqual(self.action_names(card), list(app.STATUS_ACTIONS[expected_status]))
        self.assertNotIn("Show hidden", html)
        self.assert_unique_card_ids(html)

    def assert_restored_matches(self, run_id, title):
        status, _, html = self.request("GET", f"/find-matches?run={run_id}")
        self.assertEqual(status, 200)
        self.assertIn(title, html)
        self.assertIn("Saved", html)
        self.assertIn("View job", html)
        card = self.card_html(html, title)
        self.assertEqual(
            self.action_names(card),
            ["applied", "remind_later", "not_interested"],
        )
        self.assertNotIn('name="action" value="save"', card)
        self.assertNotIn('name="action" value="show_again"', card)
        self.assertNotIn('<article class="card tracker my-job-card"', html)
        self.assert_unique_card_ids(html)
        return html

    def test_normal_user_save_reload_and_tracker_preserve_run_owner(self):
        self.start_server(demo_mode=False)
        status, _, initial_matches = self.request("GET", "/find-matches")
        self.assertEqual(status, 200)
        self.assertNotIn("Development personas", initial_matches)
        status, _, direct_tracker = self.request(
            "GET",
            "/tracker?profile=portuguese_english_reviewer",
        )
        self.assertEqual(status, 200)
        self.assertIn("My Profile", direct_tracker)
        self.assertNotIn("Portuguese-English AI Reviewer", direct_tracker)
        self.assertNotIn("Switch", direct_tracker)

        run_id = self.create_run(
            {
                "input_text": "General reviewer seeking remote AI evaluation work.",
                "input_style": "short_paragraph",
                "profile": "portuguese_english_reviewer",
            }
        )
        self.assertEqual(self.registry.get(run_id).owner_profile_id, "local_user")

        status, _, html = self.request("GET", f"/find-matches?run={run_id}")
        self.assertEqual(status, 200)
        self.assertNotIn('name="profile"', html)
        self.assertIn(f"/tracker?run={run_id}", html)
        fields = self.first_action(html, "save")
        fields["profile"] = "portuguese_english_reviewer"
        status, response_headers, payload = self.request(
            "POST",
            "/action",
            fields,
            "application/json",
            {app.INLINE_ACTION_HEADER: "1"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(response_headers["Content-Type"].startswith("application/json"))
        self.assertNotIn("Location", response_headers)
        action_payload = json.loads(payload)
        self.assertTrue(action_payload["ok"])
        self.assertEqual(action_payload["card_id"], fields["return_to"])
        self.assertEqual(action_payload["opportunity_key"], fields["opportunity_key"])
        self.assertEqual(action_payload["title"], "General AI Reviewer")
        self.assertEqual(action_payload["message"], "Saved to My Jobs.")
        self.assertEqual(action_payload["status_label"], "Saved")
        self.assertIn("Mark as applied", action_payload["controls_html"])
        self.assertIn("Remind me in 7 days", action_payload["controls_html"])
        self.assertIn(run_id, action_payload["controls_html"])
        self.assertNotIn('name="profile"', action_payload["controls_html"])
        self.assertEqual(len(self.pipeline_rows("local_user")), 1)
        self.assertEqual(len(self.pipeline_rows("portuguese_english_reviewer")), 0)
        duplicate_status, duplicate_headers, duplicate_payload = self.request(
            "POST",
            "/action",
            fields,
            "application/json",
            {app.INLINE_ACTION_HEADER: "1"},
        )
        self.assertEqual(duplicate_status, 200)
        self.assertTrue(duplicate_headers["Content-Type"].startswith("application/json"))
        self.assertTrue(json.loads(duplicate_payload)["ok"])
        self.assertEqual(len(self.pipeline_rows("local_user")), 1)

        status, _, reloaded = self.request("GET", f"/find-matches?run={run_id}")
        self.assertEqual(status, 200)
        self.assertIn("Saved", reloaded)
        status, _, tracker = self.request("GET", f"/tracker?run={run_id}")
        self.assertEqual(status, 200)
        self.assertIn("General AI Reviewer", tracker)
        self.assertIn(f"/find-matches?run={run_id}", tracker)
        self.assertIn("<h1>My Jobs</h1>", tracker)
        self.assertIn('aria-label="Filter My Jobs"', tracker)
        self.assertNotIn("Application Tracker", tracker)
        self.assertNotIn("Switch", tracker)

    def test_demo_personas_have_separate_owners_and_state(self):
        self.start_server(demo_mode=True)
        expected = {
            "beginner_bilingual": "beginner_bilingual_no_degree",
            "software_engineer": "software_engineer",
            "biology_academic": "biology_or_medicine_academic",
        }
        for persona, owner in expected.items():
            run_id = self.create_run(
                {
                    "sample": persona,
                    "input_text": "tampered text must not select another owner",
                    "input_style": "messy_sparse_input",
                }
            )
            run = self.registry.get(run_id)
            self.assertEqual(run.owner_profile_id, owner)
            self.assertEqual(run.raw_input, app.PREVIEW_SAMPLES[persona]["text"])
            status, _, html = self.request("GET", f"/find-matches?run={run_id}")
            self.assertEqual(status, 200)
            parser = FormParser()
            parser.feed(html)
            action_forms = [form for form in parser.forms if form["attrs"].get("action") == "/action"]
            self.assertTrue(action_forms)
            self.assertTrue(all(form["fields"].get("match_run_id") == run_id for form in action_forms))
            self.assertTrue(all("profile" not in form["fields"] for form in action_forms))
            fields = self.first_action(html, "save")
            fields["profile"] = "portuguese_english_reviewer"
            action_status, _, _ = self.request(
                "POST",
                "/action",
                fields,
                "application/json",
                {app.INLINE_ACTION_HEADER: "1"},
            )
            self.assertEqual(action_status, 200)
            self.assertEqual(len(self.pipeline_rows(owner)), 1)

        self.assertEqual(len(self.pipeline_rows("portuguese_english_reviewer")), 0)
        self.assertEqual(self.pipeline_rows("software_engineer")[0]["opportunity_title"], "Python Coding Evaluator")

    def test_cross_profile_pipeline_item_and_unknown_run_are_rejected(self):
        self.start_server(demo_mode=True)
        software_run_id = self.create_run({"sample": "software_engineer"})
        with self.connect() as conn:
            foreign = pipeline_actions.perform_pipeline_action(
                conn,
                action="save",
                owner_profile_id="portuguese_english_reviewer",
                idempotency_key="foreign-owner-save-0000000001",
                match_run_id="foreign-run",
                source="Fixture Source",
                title="Foreign Reviewer",
                url="https://example.test/foreign",
            )
            foreign_id = foreign.pipeline_item["pipeline_item_id"]

        fields = {
            "match_run_id": software_run_id,
            "pipeline_item_id": foreign_id,
            "action": "applied",
            "idempotency_key": "cross-profile-action-000000001",
            "expected_version": str(foreign.state["version"]),
            "return_to": "application-tracker",
            "section": "tracker",
            "profile": "software_engineer",
        }
        status, headers, payload = self.request(
            "POST",
            "/action",
            fields,
            "application/json",
            {app.INLINE_ACTION_HEADER: "1"},
        )
        self.assertEqual(status, 403)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertNotIn("<!doctype", payload.lower())
        self.assertIn("unavailable for this profile", json.loads(payload)["error"])

        fields["match_run_id"] = "missing-run"
        status, headers, payload = self.request(
            "POST",
            "/action",
            fields,
            extra_headers={app.INLINE_ACTION_HEADER: "1"},
        )
        self.assertEqual(status, 410)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertNotIn("<!doctype", payload.lower())
        self.assertIn("unknown or has expired", json.loads(payload)["error"])

    def test_no_javascript_fallback_returns_to_same_match_run(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "General reviewer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        _, _, html = self.request("GET", f"/find-matches?run={run_id}")
        fields = self.first_action(html, "save")
        status, headers, _ = self.request("POST", "/action", fields)
        self.assertEqual(status, 303)
        location = headers["Location"]
        self.assertTrue(location.startswith("/find-matches?"))
        self.assertEqual(parse_qs(urlparse(location).query)["run"], [run_id])
        self.assertEqual(parse_qs(urlparse(location).query)["message"], ["Saved to My Jobs."])
        self.assertNotIn("/tracker", location)

    def test_applied_remind_and_skip_actions_keep_same_run_owner(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "General reviewer seeking remote AI work.", "input_style": "short_paragraph"}
        )

        _, _, html = self.request("GET", f"/find-matches?run={run_id}")
        applied_fields = self.first_action(html, "applied")
        status, _, payload = self.request(
            "POST",
            "/action",
            applied_fields,
            "application/json",
            {app.INLINE_ACTION_HEADER: "1"},
        )
        self.assertEqual(status, 200)
        applied_payload = json.loads(payload)
        self.assertEqual(applied_payload["status"], "applied")
        self.assertEqual(applied_payload["message"], "Marked as applied.")

        _, _, html = self.request("GET", f"/find-matches?run={run_id}")
        remind_fields = self.first_action(html, "remind_later")
        status, _, payload = self.request(
            "POST",
            "/action",
            remind_fields,
            "application/json",
            {app.INLINE_ACTION_HEADER: "1"},
        )
        self.assertEqual(status, 200)
        reminder_payload = json.loads(payload)
        self.assertEqual(reminder_payload["status"], "applied")
        row = self.pipeline_rows("local_user")[0]
        expected_reminder = (
            app.datetime.now(app.timezone.utc).date() + timedelta(days=7)
        ).isoformat()
        self.assertEqual(row["reminder_date"], expected_reminder)
        self.assertEqual(reminder_payload["message"], f"Reminder set for {expected_reminder}.")
        self.assertEqual(reminder_payload["status_label"], "Applied")

        _, _, html = self.request("GET", f"/find-matches?run={run_id}")
        skip_fields = self.first_action(html, "not_interested")
        status, _, payload = self.request(
            "POST",
            "/action",
            skip_fields,
            "application/json",
            {app.INLINE_ACTION_HEADER: "1"},
        )
        self.assertEqual(status, 200)
        skip_payload = json.loads(payload)
        self.assertEqual(skip_payload["status"], "not_interested")
        self.assertEqual(skip_payload["message"], "Marked not interested.")
        self.assertEqual(self.pipeline_rows("local_user")[0]["status"], "not_interested")
        self.assertEqual(self.registry.get(run_id).owner_profile_id, "local_user")

    def test_hidden_and_show_again_inline_for_each_supported_starting_status(self):
        self.start_server(demo_mode=False)
        cases = (
            ("saved", "General reviewer seeking remote AI work.", "General AI Reviewer"),
            ("applied", "Software engineer seeking remote AI work.", "Python Coding Evaluator"),
            ("assessment_started", "Biology researcher seeking remote AI work.", "Microbiology Specialist"),
        )
        for starting_status, input_text, title in cases:
            with self.subTest(starting_status=starting_status):
                run_id, matches = self.create_tracked_run(input_text, starting_status)
                fields = self.first_action(matches, "not_interested")
                status, headers, payload = self.request(
                    "POST",
                    "/action",
                    fields,
                    "application/json",
                    {app.INLINE_ACTION_HEADER: "1"},
                )
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith("application/json"))
                response = json.loads(payload)
                self.assertEqual(response["status"], "not_interested")
                self.assertIn("Show again", response["controls_html"])
                self.assertNotIn("Mark as applied", response["controls_html"])
                self.assertEqual(self.pipeline_rows("local_user")[-1]["status"], "not_interested")

                repeated_status, _, repeated_payload = self.request(
                    "POST",
                    "/action",
                    fields,
                    "application/json",
                    {app.INLINE_ACTION_HEADER: "1"},
                )
                self.assertEqual(repeated_status, 200)
                self.assertEqual(json.loads(repeated_payload)["status"], "not_interested")

                hidden = self.assert_hidden_navigation(run_id, title)
                show_again = self.first_action(hidden, "show_again")
                status, _, payload = self.request(
                    "POST",
                    "/action",
                    show_again,
                    "application/json",
                    {app.INLINE_ACTION_HEADER: "1"},
                )
                self.assertEqual(status, 200)
                response = json.loads(payload)
                self.assertEqual(response["status"], starting_status)
                self.assertIn(
                    app.ACTION_LABELS[app.STATUS_ACTIONS[starting_status][0]],
                    response["controls_html"],
                )
                self.assertNotIn("Show again", response["controls_html"])

                repeated_status, _, repeated_payload = self.request(
                    "POST",
                    "/action",
                    show_again,
                    "application/json",
                    {app.INLINE_ACTION_HEADER: "1"},
                )
                self.assertEqual(repeated_status, 200)
                self.assertEqual(json.loads(repeated_payload)["status"], starting_status)
                self.assertEqual(self.pipeline_rows("local_user")[-1]["status"], starting_status)
                self.assert_restored_my_jobs(run_id, title, starting_status)

    def test_hidden_and_show_again_no_javascript_for_each_supported_starting_status(self):
        self.start_server(demo_mode=False)
        cases = (
            ("saved", "General reviewer seeking remote AI work.", "General AI Reviewer"),
            ("applied", "Software engineer seeking remote AI work.", "Python Coding Evaluator"),
            ("assessment_started", "Biology researcher seeking remote AI work.", "Microbiology Specialist"),
        )
        for starting_status, input_text, title in cases:
            with self.subTest(starting_status=starting_status):
                run_id, matches = self.create_tracked_run(input_text, starting_status)
                fields = self.first_action(matches, "not_interested")
                status, headers, _ = self.request("POST", "/action", fields)
                self.assertEqual(status, 303)
                self.assertEqual(
                    parse_qs(urlparse(headers["Location"]).query)["run"],
                    [run_id],
                )
                self.assertEqual(self.pipeline_rows("local_user")[-1]["status"], "not_interested")

                repeated_status, repeated_headers, _ = self.request("POST", "/action", fields)
                self.assertEqual(repeated_status, 303)
                self.assertEqual(
                    parse_qs(urlparse(repeated_headers["Location"]).query)["run"],
                    [run_id],
                )
                self.assertEqual(self.pipeline_rows("local_user")[-1]["status"], "not_interested")

                hidden = self.assert_hidden_navigation(run_id, title)
                show_again = self.first_action(hidden, "show_again")
                status, headers, _ = self.request("POST", "/action", show_again)
                self.assertEqual(status, 303)
                self.assertTrue(headers["Location"].startswith("/find-matches?"))
                self.assertEqual(
                    parse_qs(urlparse(headers["Location"]).query)["run"],
                    [run_id],
                )
                self.assertEqual(self.pipeline_rows("local_user")[-1]["status"], starting_status)

                repeated_status, repeated_headers, _ = self.request("POST", "/action", show_again)
                self.assertEqual(repeated_status, 303)
                self.assertEqual(
                    parse_qs(urlparse(repeated_headers["Location"]).query)["run"],
                    [run_id],
                )
                self.assertEqual(self.pipeline_rows("local_user")[-1]["status"], starting_status)
                self.assert_restored_my_jobs(run_id, title, starting_status)

    def test_matches_reload_restores_saved_controls_after_inline_show_again(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "General reviewer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        _, _, matches = self.request("GET", f"/find-matches?run={run_id}")
        self.assertEqual(self.action_names(matches), ["save", "applied", "not_interested"])
        self.assertIn("View job", matches)

        save = self.first_action(matches, "save")
        self.request("POST", "/action", save, "application/json", {app.INLINE_ACTION_HEADER: "1"})
        matches = self.assert_restored_matches(run_id, "General AI Reviewer")

        hide = self.first_action(matches, "not_interested")
        self.request("POST", "/action", hide, "application/json", {app.INLINE_ACTION_HEADER: "1"})
        _, _, hidden_matches = self.request("GET", f"/find-matches?run={run_id}")
        self.assertIn("Not interested", hidden_matches)
        self.assertEqual(self.action_names(hidden_matches), ["show_again"])

        _, _, hidden_tracker = self.request("GET", f"/tracker?run={run_id}&view=hidden")
        show_again = self.first_action(hidden_tracker, "show_again")
        status, _, payload = self.request(
            "POST",
            "/action",
            show_again,
            "application/json",
            {app.INLINE_ACTION_HEADER: "1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["status"], "saved")
        first_reload = self.assert_restored_matches(run_id, "General AI Reviewer")
        second_reload = self.assert_restored_matches(run_id, "General AI Reviewer")
        self.assertEqual(self.action_names(first_reload), self.action_names(second_reload))

    def test_matches_reload_restores_saved_controls_after_no_javascript_show_again(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "General reviewer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        _, _, matches = self.request("GET", f"/find-matches?run={run_id}")
        save = self.first_action(matches, "save")
        status, headers, _ = self.request("POST", "/action", save)
        self.assertEqual(status, 303)
        self.assertEqual(parse_qs(urlparse(headers["Location"]).query)["run"], [run_id])

        matches = self.assert_restored_matches(run_id, "General AI Reviewer")
        hide = self.first_action(matches, "not_interested")
        status, _, _ = self.request("POST", "/action", hide)
        self.assertEqual(status, 303)

        _, _, hidden_tracker = self.request("GET", f"/tracker?run={run_id}&view=hidden")
        show_again = self.first_action(hidden_tracker, "show_again")
        status, headers, _ = self.request("POST", "/action", show_again)
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/find-matches?"))
        self.assertEqual(parse_qs(urlparse(headers["Location"]).query)["run"], [run_id])
        self.assert_restored_matches(run_id, "General AI Reviewer")

    def test_cross_match_run_opportunity_reference_is_rejected(self):
        self.start_server(demo_mode=False)
        first_run = self.create_run(
            {"input_text": "General reviewer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        second_run = self.create_run(
            {"input_text": "Software engineer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        _, _, first_matches = self.request("GET", f"/find-matches?run={first_run}")
        fields = self.first_action(first_matches, "save")
        fields["match_run_id"] = second_run
        status, headers, payload = self.request(
            "POST",
            "/action",
            fields,
            "application/json",
            {app.INLINE_ACTION_HEADER: "1"},
        )
        self.assertEqual(status, 403)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertIn("not available in this match run", json.loads(payload)["error"])
        self.assertEqual(len(self.pipeline_rows("local_user")), 0)

    def test_edit_profile_creates_new_immutable_run_for_same_owner(self):
        self.start_server(demo_mode=False)
        original_text = "General reviewer seeking remote AI work."
        original_run_id = self.create_run(
            {"input_text": original_text, "input_style": "short_paragraph"}
        )

        status, _, edit_page = self.request(
            "GET",
            f"/find-matches?run={original_run_id}&edit=1",
        )
        self.assertEqual(status, 200)
        self.assertIn(original_text, edit_page)
        self.assertIn(f'name="edit_run_id" value="{original_run_id}"', edit_page)
        self.assertIn("Submitting these edits creates a new match run.", edit_page)

        edited_text = "Software engineer with Python experience seeking remote AI coding work."
        edited_run_id = self.create_run(
            {
                "edit_run_id": original_run_id,
                "input_text": edited_text,
                "input_style": "resume_or_linkedin_style",
                "profile": "portuguese_english_reviewer",
            }
        )

        self.assertNotEqual(edited_run_id, original_run_id)
        original_run = self.registry.get(original_run_id)
        edited_run = self.registry.get(edited_run_id)
        self.assertEqual(original_run.owner_profile_id, "local_user")
        self.assertEqual(edited_run.owner_profile_id, original_run.owner_profile_id)
        self.assertEqual(original_run.raw_input, original_text)
        self.assertEqual(edited_run.raw_input, edited_text)
        self.assertEqual(
            edited_run.recommendation_context["matches"]["do_these_first"][0]["display_title"],
            "Python Coding Evaluator",
        )

    def test_action_forms_have_unique_server_keys_and_normalized_versions(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "General reviewer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        _, _, html = self.request("GET", f"/find-matches?run={run_id}")
        parser = FormParser()
        parser.feed(html)
        untracked = [form for form in parser.forms if form["attrs"].get("action") == "/action"]
        keys = [form["fields"]["idempotency_key"] for form in untracked]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(len(key) >= 16 for key in keys))
        self.assertTrue(all(not key.startswith(pipeline_actions.INTERNAL_IDEMPOTENCY_PREFIX) for key in keys))
        self.assertTrue(all("expected_version" not in form["fields"] for form in untracked))

        save = self.first_action(html, "save")
        status, _, payload = self.request(
            "POST", "/action", save, "application/json", {app.INLINE_ACTION_HEADER: "1"}
        )
        self.assertEqual(status, 200)
        result = json.loads(payload)
        self.assertFalse(result["replayed"])
        self.assertEqual(result["state_version"], 2)
        _, _, reloaded = self.request("GET", f"/find-matches?run={run_id}")
        parser = FormParser()
        parser.feed(reloaded)
        tracked = [form for form in parser.forms if form["attrs"].get("action") == "/action"]
        self.assertTrue(tracked)
        self.assertTrue(all(form["fields"]["expected_version"] == "2" for form in tracked))
        self.assertTrue(all(form["fields"]["pipeline_item_id"].startswith("pipeline::") for form in tracked))
        self.assertEqual(
            len({form["fields"]["idempotency_key"] for form in tracked}), len(tracked)
        )

    def test_exact_retry_stale_form_and_idempotency_conflict_http_contract(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "General reviewer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        _, _, html = self.request("GET", f"/find-matches?run={run_id}")
        save = self.first_action(html, "save")
        first = self.request("POST", "/action", save, "application/json", {app.INLINE_ACTION_HEADER: "1"})
        replay = self.request("POST", "/action", save, "application/json", {app.INLINE_ACTION_HEADER: "1"})
        self.assertEqual((first[0], replay[0]), (200, 200))
        self.assertFalse(json.loads(first[2])["replayed"])
        self.assertTrue(json.loads(replay[2])["replayed"])
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_pipeline_transitions").fetchone()[0], 2)

        _, _, html = self.request("GET", f"/find-matches?run={run_id}")
        stale_applied = self.first_action(html, "applied")
        reminder = self.first_action(html, "remind_later")
        self.assertEqual(
            self.request("POST", "/action", reminder, "application/json", {app.INLINE_ACTION_HEADER: "1"})[0],
            200,
        )
        before = self.db_path.read_bytes()
        status, headers, body = self.request(
            "POST", "/action", stale_applied, "application/json", {app.INLINE_ACTION_HEADER: "1"}
        )
        self.assertEqual(status, 409)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertIn("changed since the page was loaded", json.loads(body)["error"])
        self.assertEqual(self.db_path.read_bytes(), before)

        conflicting = dict(save)
        conflicting["action"] = "applied"
        status, _, body = self.request(
            "POST", "/action", conflicting, "application/json", {app.INLINE_ACTION_HEADER: "1"}
        )
        self.assertEqual(status, 409)
        self.assertIn("conflicts with an earlier request", json.loads(body)["error"])
        self.assertNotIn("fingerprint", body.lower())

    def test_browser_workflow_chain_and_applicant_parity(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "Biology researcher seeking remote AI work.", "input_style": "short_paragraph"}
        )
        _, _, html = self.request("GET", f"/find-matches?run={run_id}")
        for action in ("applied", "assessment_started", "assessment_completed", "accepted"):
            fields = self.first_action(html, action)
            status, _, payload = self.request(
                "POST", "/action", fields, "application/json", {app.INLINE_ACTION_HEADER: "1"}
            )
            self.assertEqual(status, 200, payload)
            html = self.request("GET", f"/find-matches?run={run_id}")[2]
        row = self.pipeline_rows("local_user")[0]
        self.assertEqual(row["status"], "accepted")
        with self.connect() as conn:
            self.assertEqual(
                [row[0] for row in conn.execute("SELECT status FROM applicant_status_updates ORDER BY id")],
                ["applied", "assessment_started", "assessment_completed", "accepted"],
            )

    def test_unknown_legacy_hidden_and_reminder_workflows_are_explicit(self):
        self.start_server(demo_mode=False)
        hidden_id = self.seed_legacy_pipeline_item("not_interested", "2026-07-30")
        run = self.registry.create("local_user", "", "short_paragraph")
        _, _, hidden = self.request("GET", f"/tracker?run={run.match_run_id}&view=hidden")
        self.assertIn("Show again as Saved", hidden)
        self.assertIn("previous workflow stage is unknown", hidden)
        show = self.first_action(hidden, "show_again")
        self.assertEqual(show["resolution_mode"], "as_saved")
        ordinary = dict(show)
        ordinary.pop("resolution_mode")
        ordinary["idempotency_key"] = "ordinary-unknown-show-0000001"
        self.assertEqual(
            self.request("POST", "/action", ordinary, "application/json", {app.INLINE_ACTION_HEADER: "1"})[0],
            409,
        )
        status, _, payload = self.request(
            "POST", "/action", show, "application/json", {app.INLINE_ACTION_HEADER: "1"}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(json.loads(payload)["status"], "saved")
        with self.connect() as conn:
            state = pipeline_state.get_current_state(conn, hidden_id, "local_user")
            self.assertEqual(state["workflow_status"], "saved")
            self.assertEqual(state["visibility"], "visible")
            self.assertEqual(state["reminder_at"][:10], "2026-07-30")
            report = pipeline_reconciliation.reconcile_pipeline_state(conn)
            self.assertFalse(report["blocking"], report)

        reminder_id = self.seed_legacy_pipeline_item("remind_later", "2026-08-01")
        with self.connect() as conn:
            report = pipeline_reconciliation.reconcile_pipeline_state(conn)
            self.assertFalse(report["blocking"], report)
        _, _, all_jobs = self.request("GET", f"/tracker?run={run.match_run_id}")
        self.assertIn("Workflow needs confirmation", all_jobs)
        self.assertIn("Reminder set for 2026-08-01", all_jobs)
        _, _, saved_jobs = self.request("GET", f"/tracker?run={run.match_run_id}&view=saved")
        self.assertNotIn("Legacy remind_later", saved_jobs)
        card = self.card_html(all_jobs, "Legacy remind_later")
        applied = self.first_action(card, "applied")
        status, _, payload = self.request(
            "POST", "/action", applied, "application/json", {app.INLINE_ACTION_HEADER: "1"}
        )
        self.assertEqual(status, 200, payload)
        with self.connect() as conn:
            state = pipeline_state.get_current_state(conn, reminder_id, "local_user")
            self.assertEqual(state["workflow_status"], "applied")
            self.assertEqual(state["reminder_at"][:10], "2026-08-01")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM applicant_status_updates").fetchone()[0],
                1,
            )

    def test_missing_migration_and_projection_return_503_without_writes(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "General reviewer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        _, _, html = self.request("GET", f"/find-matches?run={run_id}")
        save = self.first_action(html, "save")
        with self.connect() as conn:
            conn.execute("DELETE FROM wahojobs_schema_migrations")
        before = self.db_path.read_bytes()
        status, _, body = self.request(
            "POST", "/action", save, "application/json", {app.INLINE_ACTION_HEADER: "1"}
        )
        self.assertEqual(status, 503)
        self.assertIn("reconciliation", json.loads(body)["error"])
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_missing_projection_and_visible_unknown_without_reminder_block_reads_and_writes(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "General reviewer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        _, _, html = self.request("GET", f"/find-matches?run={run_id}")
        save = self.first_action(html, "save")
        self.assertEqual(
            self.request("POST", "/action", save, "application/json", {app.INLINE_ACTION_HEADER: "1"})[0],
            200,
        )
        _, _, tracked = self.request("GET", f"/find-matches?run={run_id}")
        applied = self.first_action(tracked, "applied")
        with self.connect() as conn:
            pipeline_id = self.pipeline_rows("local_user")[0]["pipeline_item_id"]
            conn.execute("DELETE FROM user_pipeline_state WHERE pipeline_item_id=?", (pipeline_id,))
        before = self.db_path.read_bytes()
        self.assertEqual(self.request("GET", f"/tracker?run={run_id}")[0], 503)
        status, _, body = self.request(
            "POST", "/action", applied, "application/json", {app.INLINE_ACTION_HEADER: "1"}
        )
        self.assertEqual(status, 503)
        self.assertIn("reconciliation", json.loads(body)["error"])
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_visible_unknown_without_reminder_is_a_blocking_read_invariant(self):
        self.start_server(demo_mode=False)
        self.seed_legacy_pipeline_item("unrecognized_legacy_status")
        run = self.registry.create("local_user", "", "short_paragraph")
        before = self.db_path.read_bytes()
        status, _, body = self.request("GET", f"/tracker?run={run.match_run_id}")
        self.assertEqual(status, 503)
        self.assertIn("needs reconciliation", body)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_fresh_repeated_actions_use_noops_and_invalid_show_again_conflicts(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "General reviewer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        _, _, html = self.request("GET", f"/find-matches?run={run_id}")
        save = self.first_action(html, "save")
        first = json.loads(
            self.request("POST", "/action", save, "application/json", {app.INLINE_ACTION_HEADER: "1"})[2]
        )
        base = {
            "match_run_id": run_id,
            "pipeline_item_id": first["pipeline_item_id"],
            "opportunity_key": save["opportunity_key"],
            "return_to": save["return_to"],
            "section": save["section"],
        }

        def submit(action, version, suffix):
            fields = {
                **base,
                "action": action,
                "expected_version": str(version),
                "idempotency_key": f"fresh-noop-{suffix}-0000000001",
            }
            return self.request(
                "POST", "/action", fields, "application/json", {app.INLINE_ACTION_HEADER: "1"}
            )

        status, _, body = submit("save", first["state_version"], "save")
        self.assertEqual(status, 200)
        saved_repeat = json.loads(body)
        self.assertEqual(saved_repeat["state_version"], 3)
        status, _, body = submit("show_again", 3, "show-visible-saved")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["state_version"], 4)
        status, _, body = submit("applied", 4, "applied")
        self.assertEqual(status, 200)
        applied = json.loads(body)
        status, _, body = submit("applied", applied["state_version"], "applied-repeat")
        self.assertEqual(status, 200)
        applied_repeat = json.loads(body)
        status, _, body = submit("show_again", applied_repeat["state_version"], "invalid-show")
        self.assertEqual(status, 409)
        self.assertIn("unavailable", json.loads(body)["error"].lower())

    def test_concurrent_identical_and_conflicting_browser_requests(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "General reviewer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        fields = self.first_action(html, "save")
        results = []

        def post(payload):
            results.append(
                self.request(
                    "POST", "/action", payload, "application/json", {app.INLINE_ACTION_HEADER: "1"}
                )
            )

        threads = [threading.Thread(target=post, args=(dict(fields),)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(result[0] for result in results), [200, 200])
        self.assertEqual(sorted(json.loads(result[2])["replayed"] for result in results), [False, True])
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_pipeline_items").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_pipeline_transitions").fetchone()[0], 2)

    def test_no_javascript_stale_form_redirects_with_actionable_error(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "General reviewer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        self.request("POST", "/action", self.first_action(html, "save"))
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        stale = self.first_action(html, "applied")
        self.request("POST", "/action", self.first_action(html, "remind_later"))
        status, headers, _ = self.request("POST", "/action", stale)
        self.assertEqual(status, 303)
        parsed = urlparse(headers["Location"])
        self.assertEqual(parse_qs(parsed.query)["run"], [run_id])
        self.assertIn("changed since the page was loaded", parse_qs(parsed.query)["error"][0])

    def test_exact_replay_renders_current_normalized_state_after_later_transitions(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "Biology researcher seeking remote AI work.", "input_style": "short_paragraph"}
        )
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        save = self.first_action(html, "save")
        self.assertEqual(
            self.request("POST", "/action", save, "application/json", {app.INLINE_ACTION_HEADER: "1"})[0],
            200,
        )
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        applied_form = self.first_action(html, "applied")
        applied = json.loads(
            self.request("POST", "/action", applied_form, "application/json", {app.INLINE_ACTION_HEADER: "1"})[2]
        )
        self.assertEqual(applied["state_version"], 3)
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        started = json.loads(
            self.request(
                "POST",
                "/action",
                self.first_action(html, "assessment_started"),
                "application/json",
                {app.INLINE_ACTION_HEADER: "1"},
            )[2]
        )
        self.assertEqual(started["state_version"], 4)

        before = self.db_path.read_bytes()
        status, _, body = self.request(
            "POST", "/action", applied_form, "application/json", {app.INLINE_ACTION_HEADER: "1"}
        )
        self.assertEqual(status, 200)
        replay = json.loads(body)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "assessment_started")
        self.assertEqual(replay["state_version"], 4)
        self.assertIn("Mark assessment complete", replay["controls_html"])
        self.assertNotIn("Mark assessment started", replay["controls_html"])
        self.assertEqual(
            set(re.findall(r'name="expected_version" value="([^"]+)"', replay["controls_html"])),
            {"4"},
        )
        self.assertEqual(self.db_path.read_bytes(), before)

        status, headers, _ = self.request("POST", "/action", applied_form)
        self.assertEqual(status, 303)
        self.assertEqual(self.db_path.read_bytes(), before)
        current = self.request("GET", headers["Location"])[2]
        self.assertIn("Assessment in progress", current)
        self.assertIn('name="expected_version" value="4"', current)

    def test_replays_after_reminder_hide_and_show_use_current_presentation(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "Biology researcher seeking remote AI work.", "input_style": "short_paragraph"}
        )
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        save_form = self.first_action(html, "save")
        self.request("POST", "/action", save_form, "application/json", {app.INLINE_ACTION_HEADER: "1"})
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        self.request("POST", "/action", self.first_action(html, "applied"), "application/json", {app.INLINE_ACTION_HEADER: "1"})
        save_replay = json.loads(
            self.request("POST", "/action", save_form, "application/json", {app.INLINE_ACTION_HEADER: "1"})[2]
        )
        self.assertTrue(save_replay["replayed"])
        self.assertEqual(save_replay["status"], "applied")

        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        reminder_form = self.first_action(html, "remind_later")
        self.request("POST", "/action", reminder_form, "application/json", {app.INLINE_ACTION_HEADER: "1"})
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        self.request("POST", "/action", self.first_action(html, "assessment_started"), "application/json", {app.INLINE_ACTION_HEADER: "1"})
        reminder_replay = json.loads(
            self.request("POST", "/action", reminder_form, "application/json", {app.INLINE_ACTION_HEADER: "1"})[2]
        )
        self.assertTrue(reminder_replay["replayed"])
        self.assertEqual(reminder_replay["status"], "assessment_started")
        self.assertTrue(reminder_replay["reminder_date"])

        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        hide_form = self.first_action(html, "not_interested")
        self.request("POST", "/action", hide_form, "application/json", {app.INLINE_ACTION_HEADER: "1"})
        hidden_replay = json.loads(
            self.request("POST", "/action", reminder_form, "application/json", {app.INLINE_ACTION_HEADER: "1"})[2]
        )
        self.assertTrue(hidden_replay["replayed"])
        self.assertEqual(hidden_replay["status"], "not_interested")
        self.assertTrue(hidden_replay["remove_card"])

        hidden = self.request("GET", f"/tracker?run={run_id}&view=hidden")[2]
        self.request(
            "POST",
            "/action",
            self.first_action(hidden, "show_again"),
            "application/json",
            {app.INLINE_ACTION_HEADER: "1"},
        )
        hide_replay = json.loads(
            self.request("POST", "/action", hide_form, "application/json", {app.INLINE_ACTION_HEADER: "1"})[2]
        )
        self.assertTrue(hide_replay["replayed"])
        self.assertEqual(hide_replay["status"], "assessment_started")
        self.assertFalse(hide_replay["remove_card"])

    def test_action_form_rejects_duplicate_and_noncanonical_values_without_writes(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "General reviewer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        untracked = self.first_action(html, "save")
        self.request("POST", "/action", untracked, "application/json", {app.INLINE_ACTION_HEADER: "1"})
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        tracked = self.first_action(html, "applied")

        def replace(fields, key, values):
            return [(name, value) for name, value in fields.items() if name != key] + [
                (key, value) for value in values
            ]

        cases = {
            "duplicate version same": replace(tracked, "expected_version", ["2", "2"]),
            "duplicate version conflict": replace(tracked, "expected_version", ["2", "99"]),
            "duplicate key same": replace(tracked, "idempotency_key", [tracked["idempotency_key"]] * 2),
            "duplicate key conflict": replace(tracked, "idempotency_key", [tracked["idempotency_key"], "other-key-0000000001"]),
            "whitespace version": replace(tracked, "expected_version", [" 2 "]),
            "whitespace key": replace(tracked, "idempotency_key", [f" {tracked['idempotency_key']} "]),
            "plus": replace(tracked, "expected_version", ["+2"]),
            "negative": replace(tracked, "expected_version", ["-1"]),
            "float": replace(tracked, "expected_version", ["2.0"]),
            "exponent": replace(tracked, "expected_version", ["2e0"]),
            "boolean": replace(tracked, "expected_version", ["True"]),
            "empty": replace(tracked, "expected_version", [""]),
            "duplicate action": replace(tracked, "action", ["applied", "applied"]),
            "duplicate run": replace(tracked, "match_run_id", [run_id, run_id]),
            "duplicate item": replace(tracked, "pipeline_item_id", [tracked["pipeline_item_id"]] * 2),
            "duplicate opportunity": replace(tracked, "opportunity_key", [tracked["opportunity_key"]] * 2),
            "duplicate return context": replace(tracked, "return_to", [tracked["return_to"]] * 2),
            "duplicate section": replace(tracked, "section", [tracked["section"]] * 2),
            "duplicate tracker view": replace(tracked, "tracker_view", ["all", "all"]),
            "duplicate resolution": list(tracked.items()) + [("resolution_mode", "as_saved"), ("resolution_mode", "as_saved")],
            "expected version on untracked": list(untracked.items()) + [("expected_version", "0")],
        }
        for label, pairs in cases.items():
            with self.subTest(label=label):
                before = self.db_path.read_bytes()
                status, headers, body = self.request_pairs("/action", pairs)
                self.assertEqual(status, 400, body)
                self.assertTrue(headers["Content-Type"].startswith("application/json"))
                self.assertEqual(json.loads(body), {"ok": False, "error": "Malformed action request."})
                self.assertEqual(self.db_path.read_bytes(), before)

        before = self.db_path.read_bytes()
        status, _, body = self.request_pairs(
            "/action",
            replace(tracked, "idempotency_key", [f" {tracked['idempotency_key']} "]),
            wants_json=False,
        )
        self.assertEqual(status, 400)
        self.assertIn("Malformed action request", body)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_dashboard_routes_overlay_normalized_pipeline_state_and_forms(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "Software engineer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        self.request(
            "POST",
            "/action",
            self.first_action(html, "applied"),
            "application/json",
            {app.INLINE_ACTION_HEADER: "1"},
        )
        context = app.demo.build_demo_context(
            profile_id="local_user",
            use_product_state=True,
        )
        context["pipeline_report"]["records"][0]["status"] = "saved"
        context["tracked"] = app.demo.build_tracked_index(
            context["pipeline_report"]["records"]
        )

        dashboard_action = None
        with mock.patch.object(app.demo, "build_demo_context", return_value=context):
            for route in ("/dashboard", "/market-dashboard"):
                with self.subTest(route=route):
                    status, _, body = self.request("GET", f"{route}?run={run_id}")
                    self.assertEqual(status, 200, body)
                    card = self.card_html(body, "Python Coding Evaluator")
                    self.assertIn("Applied", card)
                    self.assertNotIn(">Saved<", card)
                    parser = FormParser()
                    parser.feed(card)
                    forms = [form for form in parser.forms if form["attrs"].get("action") == "/action"]
                    self.assertTrue(forms)
                    self.assertTrue(all(form["fields"].get("expected_version") == "2" for form in forms))
                    self.assertEqual(
                        len({form["fields"]["idempotency_key"] for form in forms}),
                        len(forms),
                    )
                    dashboard_action = next(
                        form["fields"]
                        for form in forms
                        if form["fields"]["action"] == "assessment_started"
                    )

        status, _, body = self.request(
            "POST",
            "/action",
            dashboard_action,
            "application/json",
            {app.INLINE_ACTION_HEADER: "1"},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["status"], "assessment_started")
        with self.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT workflow_status FROM user_pipeline_state").fetchone()[0],
                "assessment_started",
            )

    def test_dashboard_routes_tolerate_status_and_reminder_mirror_drift(self):
        self.start_server(demo_mode=False)
        run_id, item_id = self.create_applied_dashboard_item()
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        reminder = json.loads(
            self.request(
                "POST",
                "/action",
                self.first_action(html, "remind_later"),
                "application/json",
                {app.INLINE_ACTION_HEADER: "1"},
            )[2]
        )
        reminder_date = reminder["reminder_date"]
        with self.connect() as conn:
            conn.execute(
                "UPDATE user_pipeline_items SET status='saved', reminder_date='' "
                "WHERE pipeline_item_id=?",
                (item_id,),
            )
        with self.connect() as conn:
            report = pipeline_reconciliation.reconcile_pipeline_state(conn)
        self.assertTrue(report["blocking"])
        self.assertTrue(report["safe_for_normalized_reads"])
        self.assertEqual(
            report["compatibility_mirror_drift_reasons"],
            ["legacy_status_mismatches", "reminder_mirror_mismatches"],
        )

        before = self.db_path.read_bytes()
        mutation_form = None
        for route in ("/dashboard", "/market-dashboard"):
            with self.subTest(route=route):
                status, _, body = self.request("GET", f"{route}?run={run_id}")
                self.assertEqual(status, 200, body)
                card = self.card_html(body, "Python Coding Evaluator")
                self.assertIn("Applied", card)
                self.assertNotIn(">Saved<", card)
                self.assertIn(f"Reminder set for {reminder_date}", card)
                parser = FormParser()
                parser.feed(card)
                forms = [
                    form["fields"]
                    for form in parser.forms
                    if form["attrs"].get("action") == "/action"
                ]
                self.assertTrue(forms)
                self.assertTrue(
                    all(form.get("expected_version") == "3" for form in forms)
                )
                mutation_form = next(
                    form for form in forms if form["action"] == "assessment_started"
                )

        for route in (
            f"/find-matches?run={run_id}",
            f"/tracker?run={run_id}",
        ):
            with self.subTest(normalized_read_route=route):
                status, _, body = self.request("GET", route)
                self.assertEqual(status, 200, body)
                card = self.card_html(body, "Python Coding Evaluator")
                self.assertIn("Applied", card)
                self.assertIn('name="expected_version" value="3"', card)
                if route.startswith("/tracker"):
                    self.assertIn(f"Reminder set for {reminder_date}", card)
        self.assertEqual(self.db_path.read_bytes(), before)

        status, _, body = self.request(
            "POST",
            "/action",
            mutation_form,
            "application/json",
            {app.INLINE_ACTION_HEADER: "1"},
        )
        self.assertEqual(status, 503, body)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_dashboard_routes_tolerate_hidden_status_mirror_drift(self):
        self.start_server(demo_mode=False)
        run_id, item_id = self.create_applied_dashboard_item(
            "General reviewer seeking remote AI work."
        )
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        hidden = json.loads(
            self.request(
                "POST",
                "/action",
                self.first_action(html, "not_interested"),
                "application/json",
                {app.INLINE_ACTION_HEADER: "1"},
            )[2]
        )
        with self.connect() as conn:
            conn.execute(
                "UPDATE user_pipeline_items SET status='applied' WHERE pipeline_item_id=?",
                (item_id,),
            )
        with self.connect() as conn:
            report = pipeline_reconciliation.reconcile_pipeline_state(conn)
        self.assertTrue(report["blocking"])
        self.assertTrue(report["safe_for_normalized_reads"])
        self.assertEqual(
            report["compatibility_mirror_drift_reasons"],
            ["legacy_status_mismatches"],
        )

        before = self.db_path.read_bytes()
        for route in ("/dashboard", "/market-dashboard"):
            with self.subTest(route=route):
                status, _, body = self.request("GET", f"{route}?run={run_id}")
                self.assertEqual(status, 200, body)
                card = self.card_html(body, "General AI Reviewer")
                self.assertIn("Not interested", card)
                self.assertIn("Show again", card)
                self.assertIn(
                    f'name="expected_version" value="{hidden["state_version"]}"', card
                )
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_dashboard_routes_block_projection_owner_mismatch(self):
        def corrupt(conn, _item_id):
            conn.commit()
            conn.execute("PRAGMA foreign_keys = OFF")
            transition_id = conn.execute(
                "SELECT transition_id FROM user_pipeline_transitions "
                "ORDER BY state_version_after DESC LIMIT 1"
            ).fetchone()[0]
            self.replace_transition_fields(
                conn,
                transition_id,
                profile_id="portuguese_english_reviewer",
            )

        self.assert_dashboard_corruption_blocks(corrupt, "owner_mismatches")

    def test_dashboard_routes_block_latest_transition_projection_mismatch(self):
        self.assert_dashboard_corruption_blocks(
            lambda conn, item_id: conn.execute(
                "UPDATE user_pipeline_state SET workflow_status='saved' "
                "WHERE pipeline_item_id=?",
                (item_id,),
            ),
            "latest_transition_state_mismatches",
        )

    def test_dashboard_routes_block_broken_state_version_chain(self):
        def corrupt(conn, _item_id):
            transition_id = conn.execute(
                "SELECT transition_id FROM user_pipeline_transitions "
                "ORDER BY state_version_after DESC LIMIT 1"
            ).fetchone()[0]
            self.replace_transition_fields(
                conn,
                transition_id,
                state_version_before=99,
                state_version_after=100,
            )

        self.assert_dashboard_corruption_blocks(
            corrupt, "non_contiguous_version_chains"
        )

    def test_dashboard_routes_block_missing_required_migration_object(self):
        self.assert_dashboard_corruption_blocks(
            lambda conn, _item_id: conn.execute(
                "DROP INDEX idx_user_pipeline_transitions_occurred"
            ),
            "migration_schema_incomplete",
        )

    def test_dashboard_routes_block_malformed_protected_transition_metadata(self):
        def corrupt(conn, _item_id):
            transition_id = conn.execute(
                "SELECT transition_id FROM user_pipeline_transitions "
                "ORDER BY state_version_after DESC LIMIT 1"
            ).fetchone()[0]
            self.replace_transition_fields(conn, transition_id, metadata_json="{")

        self.assert_dashboard_corruption_blocks(corrupt, "malformed_transition_states")

    def test_dashboard_routes_block_visible_unknown_without_reminder(self):
        self.assert_dashboard_corruption_blocks(
            lambda conn, item_id: conn.execute(
                """
                UPDATE user_pipeline_state
                SET workflow_status=NULL,
                    workflow_status_provenance='unknown_legacy',
                    reminder_at=NULL
                WHERE pipeline_item_id=?
                """,
                (item_id,),
            ),
            "visible_unresolved_workflows",
        )

    def test_dashboard_routes_fail_safely_when_normalized_projection_is_missing(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "General reviewer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        self.request("POST", "/action", self.first_action(html, "applied"), "application/json", {app.INLINE_ACTION_HEADER: "1"})
        with self.connect() as conn:
            conn.execute("DELETE FROM user_pipeline_state")
        before = self.db_path.read_bytes()
        for route in ("/dashboard", "/market-dashboard"):
            status, _, body = self.request("GET", f"{route}?run={run_id}")
            self.assertEqual(status, 503)
            self.assertIn("needs reconciliation", body)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_inline_tracker_actions_return_server_authoritative_filter_fragments(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "General reviewer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        title = "General AI Reviewer"
        self.request("POST", "/action", self.first_action(html, "save"), "application/json", {app.INLINE_ACTION_HEADER: "1"})

        saved_view = self.request("GET", f"/tracker?run={run_id}&view=saved")[2]
        applied = json.loads(
            self.request(
                "POST",
                "/action",
                self.first_action(saved_view, "applied"),
                "application/json",
                {app.INLINE_ACTION_HEADER: "1"},
            )[2]
        )
        self.assertTrue(applied["remove_card"])
        self.assertFalse(applied["remains_in_view"])
        self.assertEqual(applied["current_view_count"], 0)
        self.assertNotIn(title, applied["workspace_html"])
        self.assertIn("No jobs in this view", applied["workspace_html"])

        in_progress = self.request("GET", f"/tracker?run={run_id}&view=in_progress")[2]
        reminder = json.loads(
            self.request(
                "POST",
                "/action",
                self.first_action(in_progress, "remind_later"),
                "application/json",
                {app.INLINE_ACTION_HEADER: "1"},
            )[2]
        )
        self.assertFalse(reminder["remove_card"])
        self.assertTrue(reminder["remains_in_view"])
        self.assertIn(title, reminder["workspace_html"])
        self.assertIn("Reminder set for", reminder["workspace_html"])

        all_view = self.request("GET", f"/tracker?run={run_id}")[2]
        hidden = json.loads(
            self.request(
                "POST",
                "/action",
                self.first_action(all_view, "not_interested"),
                "application/json",
                {app.INLINE_ACTION_HEADER: "1"},
            )[2]
        )
        self.assertTrue(hidden["remove_card"])
        self.assertEqual(hidden["hidden_count"], 1)
        self.assertIn("Show hidden (1)", hidden["workspace_html"])
        self.assertNotIn(title, hidden["workspace_html"])

        hidden_view = self.request("GET", f"/tracker?run={run_id}&view=hidden")[2]
        shown = json.loads(
            self.request(
                "POST",
                "/action",
                self.first_action(hidden_view, "show_again"),
                "application/json",
                {app.INLINE_ACTION_HEADER: "1"},
            )[2]
        )
        self.assertTrue(shown["remove_card"])
        self.assertEqual(shown["hidden_count"], 0)
        self.assertNotIn(title, shown["workspace_html"])
        self.assertIn("No jobs in this view", shown["workspace_html"])

    def test_inline_match_hide_removes_card_from_current_match_run(self):
        self.start_server(demo_mode=False)
        run_id = self.create_run(
            {"input_text": "General reviewer seeking remote AI work.", "input_style": "short_paragraph"}
        )
        html = self.request("GET", f"/find-matches?run={run_id}")[2]
        status, _, body = self.request(
            "POST",
            "/action",
            self.first_action(html, "not_interested"),
            "application/json",
            {app.INLINE_ACTION_HEADER: "1"},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["remove_card"])
        self.assertFalse(payload["remains_in_view"])
        self.assertEqual(payload["current_view_count"], 0)
        self.assertEqual(payload["hidden_count"], 1)

    def test_inline_script_replaces_tracker_fragments_and_removes_match_cards(self):
        script = app.render_inline_action_script()
        self.assertIn('workspace.outerHTML = payload.workspace_html', script)
        self.assertIn('trackerHeader.outerHTML = payload.tracker_header_html', script)
        self.assertIn('if (payload.remove_card)', script)
        self.assertIn('card.remove()', script)
        self.assertIn('payload.current_view_count', script)

    def test_registry_is_bounded_and_evicts_least_recently_used_run(self):
        registry = app.MatchRunRegistry(max_size=2)
        first = registry.create("local_user", "one", "short_paragraph")
        second = registry.create("local_user", "two", "short_paragraph")
        self.assertIsNotNone(registry.get(first.match_run_id))
        third = registry.create("local_user", "three", "short_paragraph")
        self.assertEqual(len(registry), 2)
        self.assertIsNone(registry.get(second.match_run_id))
        self.assertIsNotNone(registry.get(first.match_run_id))
        self.assertIsNotNone(registry.get(third.match_run_id))

    def test_inline_script_uses_explicit_urlencoded_json_contract(self):
        script = app.render_inline_action_script()
        self.assertIn('form.getAttribute("action")', script)
        self.assertNotIn("fetch(form.action", script)
        self.assertIn('"Accept": "application/json"', script)
        self.assertIn('"X-Wahojobs-Inline-Action": "1"', script)
        self.assertIn("application/x-www-form-urlencoded", script)
        self.assertIn("new URLSearchParams(new FormData(form))", script)
        self.assertIn('response.headers.get("Content-Type")', script)
        self.assertLess(
            script.index('response.headers.get("Content-Type")'),
            script.index("await response.json()"),
        )
        self.assertIn("Inline action expected JSON", script)
        self.assertIn("genericFailure", script)
        self.assertIn("js-action-feedback", script)


if __name__ == "__main__":
    unittest.main()
