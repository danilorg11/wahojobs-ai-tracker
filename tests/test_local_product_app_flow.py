import http.client
import json
import sqlite3
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
from wahojobs.profiles.normalizer import BaselineHeuristicProfileNormalizer


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
        self.connection_patch.stop()
        self.temp_dir.cleanup()

    def _initialize_database(self):
        schema_path = Path(__file__).resolve().parents[1] / "wahojobs" / "db" / "schema.sql"
        conn = self.connect()
        try:
            conn.executescript(schema_path.read_text(encoding="utf-8"))
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
            foreign = app.ensure_pipeline_item(
                conn,
                "portuguese_english_reviewer",
                "Fixture Source",
                "Foreign Reviewer",
                "https://example.test/foreign",
            )
            foreign_id = foreign["id"]

        fields = {
            "match_run_id": software_run_id,
            "pipeline_item_id": foreign_id,
            "action": "applied",
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
        self.assertIn("different profile", json.loads(payload)["error"])

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
        self.assertEqual(json.loads(payload)["status"], "applied")

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
        self.assertEqual(json.loads(payload)["status"], "remind_later")
        row = self.pipeline_rows("local_user")[0]
        expected_reminder = (
            app.datetime.now(app.timezone.utc).date() + timedelta(days=7)
        ).isoformat()
        self.assertEqual(row["reminder_date"], expected_reminder)

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
        self.assertEqual(json.loads(payload)["status"], "not_interested")
        self.assertEqual(self.pipeline_rows("local_user")[0]["status"], "not_interested")
        self.assertEqual(self.registry.get(run_id).owner_profile_id, "local_user")

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
