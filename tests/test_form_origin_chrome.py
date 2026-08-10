"""Opt-in real-Chrome regression for the durable login form origin."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlsplit

from tests.accounts_test_support import INVITATION_KEY
from tests.durable_google_login_browser_test_support import (
    loopback_and_in_memory_provider_only,
    running_https_production_launcher_app,
    temporary_browser_login_state,
)
from wahojobs import accounts
import wahojobs.durable_google_login_browser as browser_module
from wahojobs.durable_google_login_browser import (
    DurableGoogleLoginBrowserIntegration,
    GOOGLE_LOGIN_START_ROUTE,
)
from wahojobs.durable_google_login_runtime import (
    build_durable_google_login_runtime,
)


_RUN_BROWSER_TEST = "WAHOJOBS_RUN_CHROME_ORIGIN_TEST"
_CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
_PARENT_FORM_CONTENT_SECURITY_POLICY = (
    browser_module._ORDINARY_FORM_CONTENT_SECURITY_POLICY
)
_CANDIDATE_FORM_CONTENT_SECURITY_POLICY = (
    browser_module._GOOGLE_LOGIN_FORM_CONTENT_SECURITY_POLICY
)


def _unused_loopback_port():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]
    finally:
        listener.close()


def _header_values(headers, name):
    lowered = name.lower()
    try:
        items = tuple(headers.items())
    except AttributeError:
        items = tuple(headers)
    return tuple(
        value
        for candidate, value in items
        if candidate.lower() == lowered
    )


def _synthetic_database_snapshot(database_path):
    connection = sqlite3.connect(
        f"file:{Path(database_path).as_posix()}?mode=ro",
        uri=True,
        timeout=2.0,
    )
    try:
        invitation = connection.execute(
            "SELECT invitation_status, consumed_at, revoked_at "
            "FROM account_invitations"
        ).fetchone()
        transactions = tuple(
            row[0]
            for row in connection.execute(
                "SELECT lifecycle "
                "FROM google_oidc_authorization_transactions"
            )
        )
        zero_tables = {
            table: connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            for table in (
                "users",
                "auth_identities",
                "product_principals",
                "principal_account_bindings",
                "account_sessions",
                "product_profiles",
            )
        }
        return {
            "invitation": invitation,
            "transactions": transactions,
            "zero_tables": zero_tables,
            "integrity": connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0],
        }
    finally:
        connection.close()


@unittest.skipUnless(
    os.environ.get(_RUN_BROWSER_TEST) == "1",
    f"set {_RUN_BROWSER_TEST}=1 for the real-Chrome origin regression",
)
class FormOriginChromeTests(unittest.TestCase):
    def _run_scenario(self, *, node, parent_policy):
        runtime_port = _unused_loopback_port()
        debug_port = _unused_loopback_port()
        driver = Path(__file__).with_name("form_origin_chrome_driver.mjs")
        observed_requests = []
        observation_lock = threading.Lock()
        state_directory = None
        profile_directory = None
        public_report = None

        with tempfile.TemporaryDirectory(
            prefix=(
                "wahojobs-form-origin-parent-"
                if parent_policy
                else "wahojobs-form-origin-candidate-"
            )
        ) as raw_profile:
            profile_directory = Path(raw_profile).resolve()
            with ExitStack() as stack:
                state = stack.enter_context(
                    temporary_browser_login_state(
                        port=runtime_port,
                        seed_existing_identity=False,
                        seed_existing_profile=False,
                        enable_invited_provisioning=True,
                    )
                )
                state_directory = state.directory
                stack.enter_context(loopback_and_in_memory_provider_only())
                connection = sqlite3.connect(state.database_path)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                try:
                    invitation = accounts.create_invitation(
                        connection,
                        email="form-origin-browser@example.test",
                        lookup_key=INVITATION_KEY,
                        expires_at=state.clock() + timedelta(days=1),
                        created_by="form_origin_browser_test",
                        idempotency_key="form-origin-browser-test-invitation",
                        now=state.clock(),
                    )
                finally:
                    connection.close()

                runtime = build_durable_google_login_runtime(
                    state.configuration_path,
                    _clock=state.clock,
                    _gateway_factory=state.gateway_factory,
                )
                original_handle = DurableGoogleLoginBrowserIntegration.handle

                def observed_handle(
                    integration,
                    method,
                    target,
                    headers,
                    body_stream=None,
                ):
                    observation = None
                    if method == "POST" and target == GOOGLE_LOGIN_START_ROUTE:
                        observation = {
                            "origin": _header_values(headers, "Origin"),
                            "host": _header_values(headers, "Host"),
                            "content_type": _header_values(
                                headers,
                                "Content-Type",
                            ),
                        }
                    response = original_handle(
                        integration,
                        method,
                        target,
                        headers,
                        body_stream,
                    )
                    if observation is not None:
                        observation["response_status"] = response.status
                        locations = _header_values(
                            response.headers,
                            "Location",
                        )
                        observation["location_header_count"] = len(locations)
                        if len(locations) == 1:
                            parsed = urlsplit(locations[0])
                            observation["location_target_is_pinned"] = (
                                parsed.scheme == "https"
                                and parsed.netloc == "accounts.google.com"
                                and parsed.path == "/o/oauth2/v2/auth"
                                and bool(parsed.query)
                                and not parsed.fragment
                            )
                        else:
                            observation["location_target_is_pinned"] = False
                        with observation_lock:
                            observed_requests.append(observation)
                    return response

                close_report = None
                try:
                    with ExitStack() as running_stack:
                        running_stack.enter_context(
                            mock.patch.object(
                                DurableGoogleLoginBrowserIntegration,
                                "handle",
                                new=observed_handle,
                            )
                        )
                        if parent_policy:
                            running_stack.enter_context(
                                mock.patch.object(
                                    browser_module,
                                    "_GOOGLE_LOGIN_FORM_CONTENT_SECURITY_POLICY",
                                    _PARENT_FORM_CONTENT_SECURITY_POLICY,
                                )
                            )
                        running_stack.enter_context(
                            running_https_production_launcher_app(runtime)
                        )
                        environment = dict(os.environ)
                        environment["WAHOJOBS_SYNTHETIC_INVITATION"] = (
                            invitation.invitation_token
                        )
                        completed = subprocess.run(
                            (
                                node,
                                str(driver),
                                str(_CHROME),
                                state.public_origin + "/login",
                                "127.0.0.1",
                                str(runtime_port),
                                str(profile_directory),
                                str(debug_port),
                            ),
                            cwd=Path(__file__).resolve().parents[1],
                            env=environment,
                            capture_output=True,
                            text=True,
                            timeout=60,
                            check=False,
                        )
                    self.assertEqual(
                        completed.returncode,
                        0,
                        msg=(
                            completed.stderr.strip()
                            + " server_observation="
                            + repr(observed_requests)
                        ),
                    )
                    browser = json.loads(completed.stdout.strip())
                    self.assertEqual(browser["loginStatus"], 200)
                    self.assertEqual(browser["loginGetCount"], 1)
                    self.assertEqual(
                        browser["finalLoginUrl"],
                        state.public_origin + "/login",
                    )
                    self.assertEqual(
                        browser["documentOrigin"],
                        state.public_origin,
                    )
                    self.assertEqual(
                        browser["documentReferrerPolicy"],
                        "same-origin",
                    )
                    self.assertEqual(
                        browser["responseReferrerPolicy"],
                        "same-origin",
                    )
                    self.assertTrue(browser["secureContext"])
                    self.assertTrue(browser["narrowSpkiAllowlistUsed"])
                    self.assertFalse(
                        browser["globalCertificateErrorsIgnored"]
                    )
                    self.assertEqual(browser["startPostCount"], 1)
                    self.assertEqual(browser["startStatus"], 303)
                    self.assertEqual(
                        browser["cdpOrigin"],
                        state.public_origin,
                    )
                    expected_policy = (
                        _PARENT_FORM_CONTENT_SECURITY_POLICY
                        if parent_policy
                        else _CANDIDATE_FORM_CONTENT_SECURITY_POLICY
                    )
                    self.assertEqual(
                        browser["documentContentSecurityPolicy"],
                        expected_policy,
                    )
                    if parent_policy:
                        self.assertTrue(browser["cspFormActionViolation"])
                        self.assertGreaterEqual(
                            browser["cspFormActionViolationCount"],
                            1,
                        )
                        self.assertGreaterEqual(
                            browser["cspSecurityLogCount"],
                            1,
                        )
                        self.assertEqual(
                            browser["providerBlockedBeforeNetwork"],
                            0,
                        )
                        self.assertIsNone(browser["startRedirectStatus"])
                        self.assertFalse(browser["providerTargetMatchesPinned"])
                    else:
                        self.assertFalse(browser["cspFormActionViolation"])
                        self.assertEqual(
                            browser["cspFormActionViolationCount"],
                            0,
                        )
                        self.assertEqual(
                            browser["providerBlockedBeforeNetwork"],
                            1,
                        )
                        self.assertEqual(browser["startRedirectStatus"], 303)
                        self.assertTrue(browser["providerTargetMatchesPinned"])
                    with observation_lock:
                        observed = tuple(observed_requests)
                    self.assertEqual(len(observed), 1)
                    self.assertEqual(
                        observed[0]["origin"],
                        (state.public_origin,),
                    )
                    self.assertEqual(
                        observed[0]["host"],
                        (f"localhost:{runtime_port}",),
                    )
                    self.assertEqual(
                        observed[0]["content_type"],
                        ("application/x-www-form-urlencoded",),
                    )
                    self.assertEqual(observed[0]["response_status"], 303)
                    self.assertEqual(observed[0]["location_header_count"], 1)
                    self.assertTrue(observed[0]["location_target_is_pinned"])
                    provider_calls = state.gateway_harness.transport.call_count
                    self.assertEqual(provider_calls, 0)
                finally:
                    close_report = runtime.close(_preserve_primary=True)

                self.assertTrue(close_report.cleanup_complete)
                snapshot = _synthetic_database_snapshot(state.database_path)
                self.assertEqual(snapshot["integrity"], "ok")
                self.assertEqual(snapshot["invitation"], ("pending", None, None))
                self.assertEqual(snapshot["transactions"], ("prepared",))
                self.assertEqual(set(snapshot["zero_tables"].values()), {0})
                self.assertFalse(
                    any(
                        state.database_path.parent.glob(
                            state.database_path.name + "-*"
                        )
                    )
                )
                public_report = {
                    "actual_origin": observed[0]["origin"][0],
                    "csp_form_action_violation": browser[
                        "cspFormActionViolation"
                    ],
                    "document_origin": browser["documentOrigin"],
                    "login_status": browser["loginStatus"],
                    "provider_external_calls": provider_calls,
                    "provider_navigation_intercepted_before_network": (
                        browser["providerBlockedBeforeNetwork"] == 1
                    ),
                    "provider_navigation_target_pinned": browser[
                        "providerTargetMatchesPinned"
                    ],
                    "referrer_policy": browser["documentReferrerPolicy"],
                    "request_reached_login_start": True,
                    "start_status": browser["startStatus"],
                    "synthetic_oidc_lifecycle": snapshot["transactions"][0],
                }

        self.assertIsNotNone(state_directory)
        self.assertIsNotNone(profile_directory)
        self.assertFalse(state_directory.exists())
        self.assertFalse(profile_directory.exists())
        return public_report

    def test_parent_csp_blocks_and_candidate_attempts_pinned_navigation(self):
        if not _CHROME.is_file():
            self.fail("installed_chrome_unavailable")
        node = shutil.which("node")
        if node is None:
            self.fail("node_runtime_unavailable")

        parent_report = self._run_scenario(node=node, parent_policy=True)
        candidate_report = self._run_scenario(
            node=node,
            parent_policy=False,
        )
        print(
            "BROWSER_FORM_REDIRECT_CSP_CONFIRMATION "
            + json.dumps(
                {
                    "candidate": candidate_report,
                    "parent": parent_report,
                },
                sort_keys=True,
            ),
            flush=True,
        )


if __name__ == "__main__":
    unittest.main()
