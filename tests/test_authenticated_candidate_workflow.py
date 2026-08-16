import io
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from scripts import local_product_app as local_product
from tests.persistent_profiles_repository_test_support import (
    canonical_fixture,
    install_repository_database,
)
from tests.persistent_profile_canonical_v2_test_support import create_v2_profile
from tests.persistent_profiles_test_support import (
    add_account_principal,
    stable_id,
)
from wahojobs import accounts
from wahojobs.authenticated_profile_matches import (
    AuthenticatedProfileMatchesBrowserIntegration,
    AuthenticatedProfileMatchesService,
    MatchesAuthorityResult,
)
from wahojobs.browser_session_authentication import (
    DurableBrowserSessionAuthenticationGateway,
)
from wahojobs.matching.metadata_overlay import OpportunityMetadataOverlay
from wahojobs.persistent_profile_read_authorization import (
    DurablePersistentProfileReadAuthorizationGateway,
)
import wahojobs.authenticated_profile_matches as matches_module


ORIGIN = "https://app.test"
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


class _DatabaseProviders:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def read(self):
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def write(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = OFF")
        try:
            yield connection
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()


class AuthenticatedCandidateWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "private-beta.sqlite"
        connection = install_repository_database(self.path)
        connection.row_factory = sqlite3.Row
        self.connection = connection
        self.addCleanup(connection.close)
        self.providers = _DatabaseProviders(self.path)
        self.clock = [NOW]
        self.accounts = []
        self.first = self._seed_account("41")
        self.integration = self._integration()
        self.addCleanup(self.integration.close)

    def _seed_account(self, suffix):
        principal_id = add_account_principal(
            self.connection,
            suffix,
            environment="private_beta",
        )
        account_id = self.connection.execute(
            "SELECT user_id FROM principal_account_bindings WHERE principal_id = ?",
            (principal_id,),
        ).fetchone()[0]
        profile_id = stable_id("prf", suffix)
        create_v2_profile(
            self.connection,
            principal_id,
            suffix=suffix,
            environment="private_beta",
            structured_json=json.dumps(
                canonical_fixture(profile_id),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        session = accounts.create_session(
            self.connection,
            user_id=account_id,
            idle_ttl=timedelta(hours=2),
            absolute_ttl=timedelta(hours=8),
            idempotency_key=f"candidate-workflow-session-{suffix}",
            now=NOW,
        )
        state = {
            "account_id": account_id,
            "principal_id": principal_id,
            "profile_id": profile_id,
            "session_id": session.session.session_id,
            "session_token": session.session_token,
            "csrf": session.csrf_secret,
        }
        self.accounts.append(state)
        return state

    def _integration(self):
        service = AuthenticatedProfileMatchesService(
            authentication_gateway=DurableBrowserSessionAuthenticationGateway(
                trusted_environment_namespace="private_beta",
                clock=lambda: self.clock[0],
            ),
            authorization_gateway=DurablePersistentProfileReadAuthorizationGateway(),
            connection_provider=self.providers.read,
            clock=lambda: self.clock[0],
            binding_secret=b"candidate-workflow-test-binding-key",
        )
        return AuthenticatedProfileMatchesBrowserIntegration(
            service,
            connection_provider=self.providers.read,
            write_connection_provider=self.providers.write,
            metadata_overlay=OpportunityMetadataOverlay(
                path=self.path.with_suffix(".overlay.json"),
                records_by_key={},
            ),
            confirmed_profile_artifact_sink=lambda _artifact: None,
            completed_profile_confirmation_authenticator=lambda _request: None,
            public_origin=ORIGIN,
            now=lambda: self.clock[0],
        )

    @staticmethod
    def _authority(state):
        return MatchesAuthorityResult(
            "profile",
            matches_module._AuthorizedMatchesState(
                "profile",
                draft_binding="a" * 64,
                account_id=state["account_id"],
                environment_namespace="private_beta",
                principal_id=state["principal_id"],
                session_id=state["session_id"],
                profile_id=state["profile_id"],
                profile_v2={"schema_version": "canonical_profile_v2"},
            ),
        )

    @staticmethod
    def _match(number=101):
        return {
            "job_id": number,
            "canonical_opportunity_id": number,
            "source": "Configured Inventory",
            "display_title": f"Backend Engineer {number}",
            "title": f"Backend Engineer {number}",
            "url": f"https://jobs.example.test/{number}",
            "affirmative_fit_status": "supported",
            "eligible_for_personalized": True,
        }

    def _run(self, state, match=None):
        match = match or self._match()
        run = self.integration._registry.create(
            owner_profile_id=state["profile_id"],
            raw_input="",
            input_style="short_paragraph",
            recommendation_context={
                "matches": {"top_matches": [match]},
                "_authenticated_inventory_count": 1,
            },
            profile_confirmed=True,
        )
        return run, match

    @staticmethod
    def _headers(state, body=None, *, accept_json=False, include_csrf=True):
        cookie = f"wahojobs_session={state['session_token']}"
        headers = [("Host", "app.test"), ("Cookie", cookie)]
        if body is not None:
            if include_csrf:
                cookie += f"; __Host-wahojobs_session_csrf={state['csrf']}"
            headers = [
                ("Host", "app.test"),
                ("Origin", ORIGIN),
                ("Sec-Fetch-Site", "same-origin"),
                ("Cookie", cookie),
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Content-Length", str(len(body))),
            ]
            if accept_json:
                headers.append(("Accept", "application/json"))
        return tuple(headers)

    def _post(self, state, authority, form, *, accept_json=True, include_csrf=True):
        body = urlencode(form).encode("ascii")
        return self.integration.handle(
            "POST",
            "/action",
            self._headers(
                state,
                body,
                accept_json=accept_json,
                include_csrf=include_csrf,
            ),
            io.BytesIO(body),
        )

    @staticmethod
    def _new_action(run, match, action, key):
        return {
            "action": action,
            "idempotency_key": "candidate-workflow-" + key,
            "match_run_id": run.match_run_id,
            "opportunity_key": local_product.match_opportunity_key(match),
            "pipeline_item_id": "",
            "return_to": "ranked",
            "section": "best_matches",
        }

    @staticmethod
    def _tracked_action(run, record, action, key):
        return {
            "action": action,
            "idempotency_key": "candidate-workflow-" + key,
            "match_run_id": run.match_run_id,
            "pipeline_item_id": record["pipeline_item_id"],
            "expected_version": str(record["state_version"]),
            "return_to": "tracker",
            "section": "tracker",
            "tracker_view": "all",
        }

    def _record(self, state):
        from wahojobs import pipeline_records

        return local_product.normalized_browser_record(
            pipeline_records.list_pipeline_records(
                self.connection,
                state["profile_id"],
                mutation_grade=True,
            )[0]
        )

    def test_actions_create_account_owned_compatibility_and_restore_my_jobs(self):
        run, match = self._run(self.first)
        save = self._new_action(run, match, "save", "save-0001")
        response = self._post(self.first, self.first, save)
        self.assertEqual(response.status, 200, response.body)
        saved_payload = json.loads(response.body)
        self.assertFalse(saved_payload["replayed"])
        self.assertEqual(saved_payload["status"], "saved")
        owner = self.connection.execute(
            "SELECT user_id, is_sample FROM user_profiles WHERE profile_id = ?",
            (self.first["profile_id"],),
        ).fetchone()
        self.assertEqual((owner["user_id"], owner["is_sample"]), (self.first["account_id"], 0))

        replay = self._post(self.first, self.first, save)
        self.assertEqual(replay.status, 200)
        self.assertTrue(json.loads(replay.body)["replayed"])
        native_replay = self._post(
            self.first,
            self.first,
            save,
            accept_json=False,
        )
        self.assertEqual(native_replay.status, 303)
        self.assertEqual(
            dict(native_replay.headers)["Location"],
            f"/find-matches?run={run.match_run_id}",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM user_pipeline_items WHERE profile_id = ?",
                (self.first["profile_id"],),
            ).fetchone()[0],
            1,
        )

        for ordinal, action in enumerate(
            ("remind_later", "applied", "not_interested"),
            start=2,
        ):
            record = self._record(self.first)
            result = self._post(
                self.first,
                self.first,
                self._tracked_action(run, record, action, f"action-{ordinal:04d}"),
            )
            self.assertEqual(result.status, 200, result.body)
            if action == "applied":
                self.assertEqual(
                    json.loads(result.body)["status"],
                    "applied",
                )

        hidden_tracker = self.integration.handle(
            "GET",
            f"/tracker?run={run.match_run_id}",
            self._headers(self.first),
        )
        self.assertIn("Show hidden", hidden_tracker.body.decode("utf-8"))

        hidden_record = self._record(self.first)
        shown = self._post(
            self.first,
            self.first,
            self._tracked_action(run, hidden_record, "show_again", "action-0005"),
        )
        self.assertEqual(shown.status, 200, shown.body)

        tracker = self.integration.handle(
            "GET",
            f"/tracker?run={run.match_run_id}",
            self._headers(self.first),
        )
        page = tracker.body.decode("utf-8")
        self.assertEqual(tracker.status, 200)
        self.assertEqual(dict(tracker.headers)["Referrer-Policy"], "same-origin")
        self.assertIn("My Jobs", page)
        self.assertIn("Backend Engineer 101", page)
        self.assertIn("Applied", page)
        self.assertIn("Reminder set", page)
        self.assertIn("Saved", page)
        self.assertIn("In progress", page)

        with mock.patch.object(
            AuthenticatedProfileMatchesBrowserIntegration,
            "_load_inventory",
            side_effect=AssertionError("current MatchRun must not regenerate matches"),
        ):
            current_matches = self.integration.handle(
                "GET",
                f"/find-matches?run={run.match_run_id}",
                self._headers(self.first),
            )
        current_page = current_matches.body.decode("utf-8")
        self.assertEqual(current_matches.status, 200)
        self.assertEqual(
            dict(current_matches.headers)["Referrer-Policy"],
            "same-origin",
        )
        self.assertIn(run.match_run_id, current_page)
        self.assertIn("Current matches", page)

        profile_target = self.integration.current_matches_target(
            run.match_run_id,
            self._headers(self.first),
        )
        self.assertEqual(profile_target, f"/find-matches?run={run.match_run_id}")
        self.assertIn(
            f"href='/account/profile?run={run.match_run_id}'>My profile</a>",
            current_page,
        )

        second = self._seed_account("43")
        self.assertIsNone(
            self.integration.current_matches_target(
                run.match_run_id,
                self._headers(second),
            )
        )

    def test_public_job_page_reuses_authenticated_pipeline_and_returns_to_job(self):
        from tests.test_public_job_page import JOB_PATH, seed_public_job

        seed_public_job(self.connection)
        logged_out = self.integration.handle(
            "GET",
            JOB_PATH,
            (("Host", "app.test"),),
        )
        logged_out_page = logged_out.body.decode("utf-8")
        self.assertEqual(logged_out.status, 200, logged_out_page)
        self.assertIn("Create a profile or sign in", logged_out_page)
        self.assertNotIn("name=\"action\"", logged_out_page)

        authenticated = self.integration.handle(
            "GET",
            JOB_PATH,
            self._headers(self.first),
        )
        page = authenticated.body.decode("utf-8")
        self.assertEqual(authenticated.status, 200, page)
        self.assertEqual(dict(authenticated.headers)["Cache-Control"], "no-store")
        self.assertIn("Your Wahojobs workflow", page)
        self.assertIn("href='/find-matches'>Matches</a>", page)
        self.assertIn("href='/tracker'>My Jobs</a>", page)
        self.assertIn(">Save</button>", page)
        self.assertIn(">Mark as applied</button>", page)
        self.assertIn(">Not interested</button>", page)
        self.assertNotIn("Create a profile or sign in", page)

        save_form = re.search(
            r'<form[^>]+action-form-save[^>]*>(.*?)</form>',
            page,
            re.DOTALL,
        )
        self.assertIsNotNone(save_form)
        form = dict(
            re.findall(
                r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"',
                save_form.group(1),
            )
        )
        saved = self._post(
            self.first,
            self.first,
            form,
            accept_json=False,
        )
        self.assertEqual(saved.status, 303, saved.body)
        self.assertEqual(dict(saved.headers)["Location"], JOB_PATH)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM user_pipeline_items WHERE profile_id = ?",
                (self.first["profile_id"],),
            ).fetchone()[0],
            1,
        )

        reloaded = self.integration.handle(
            "GET",
            JOB_PATH,
            self._headers(self.first),
        ).body.decode("utf-8")
        self.assertIn("aria-label='Current status: Saved'>Saved</p>", reloaded)
        self.assertIn(">Remind me in 7 days</button>", reloaded)
        self.assertIn(">Mark as applied</button>", reloaded)
        self.assertIn(">Not interested</button>", reloaded)

        tracker = self.integration.handle(
            "GET",
            "/tracker",
            self._headers(self.first),
        ).body.decode("utf-8")
        self.assertIn("Applied AI Engineer — Model Evaluation", tracker)

    def test_rejects_browser_identity_isolates_owners_and_detects_stale_versions(self):
        sealed = self._authority(self.first).authorized_state()
        with self.assertRaisesRegex(AttributeError, "authority_is_immutable"):
            sealed._profile_id = "browser-selected"
        run, match = self._run(self.first)
        self.assertEqual(
            self._post(
                self.first,
                self.first,
                self._new_action(run, match, "save", "save-isolation"),
            ).status,
            200,
        )
        first_record = self._record(self.first)
        second = self._seed_account("42")
        second_run, _ = self._run(second, self._match(202))

        selected_identity = self._tracked_action(
            run,
            first_record,
            "applied",
            "selected-identity",
        )
        selected_identity["owner_profile_id"] = second["profile_id"]
        self.assertEqual(self._post(self.first, self.first, selected_identity).status, 400)

        isolated = self._tracked_action(
            second_run,
            first_record,
            "applied",
            "cross-owner",
        )
        self.assertEqual(self._post(second, second, isolated).status, 403)

        stale = self._tracked_action(run, first_record, "applied", "stale-version")
        stale["expected_version"] = "0"
        self.assertEqual(self._post(self.first, self.first, stale).status, 409)

    def test_csrf_logout_and_expiry_stop_mutation_at_the_boundary(self):
        run, match = self._run(self.first)
        form = self._new_action(run, match, "save", "blocked-save")
        self.assertEqual(
            self._post(
                self.first,
                self.first,
                form,
                include_csrf=False,
            ).status,
            403,
        )

        self.connection.execute(
            "UPDATE account_sessions SET revoked_at = ?, revoke_reason = 'user_logout' "
            "WHERE session_id = ?",
            (NOW.isoformat(timespec="seconds"), self.first["session_id"]),
        )
        self.connection.commit()
        with mock.patch.object(
            AuthenticatedProfileMatchesService,
            "resolve",
            return_value=self._authority(self.first),
        ):
            self.assertEqual(self._post(self.first, self.first, form).status, 401)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM user_pipeline_items").fetchone()[0],
            0,
        )

        expiring = self._seed_account("43")
        expiring_run, expiring_match = self._run(expiring, self._match(303))
        self.clock[0] = NOW + timedelta(hours=3)
        expired_form = self._new_action(
            expiring_run,
            expiring_match,
            "save",
            "expired-save",
        )
        with mock.patch.object(
            AuthenticatedProfileMatchesService,
            "resolve",
            return_value=self._authority(expiring),
        ):
            self.assertEqual(self._post(expiring, expiring, expired_form).status, 401)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM user_pipeline_items WHERE profile_id = ?",
                (expiring["profile_id"],),
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
