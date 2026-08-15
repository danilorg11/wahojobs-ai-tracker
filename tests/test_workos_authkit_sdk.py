from __future__ import annotations

from pathlib import Path
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

from wahojobs.workos_authkit import (
    EXCHANGE_MAX_RETRIES,
    EXCHANGE_TIMEOUT_SECONDS,
    WorkOSAuthKitUnavailable,
    WorkOSSDKBoundary,
)


class _FakeUserManagement:
    def __init__(self):
        self.authorization_calls = []
        self.exchange_calls = []
        self.response = None
        self.failure = None

    def get_authorization_url(self, **kwargs):
        self.authorization_calls.append(kwargs)
        return "https://api.workos.com/user_management/authorize"

    def authenticate_with_code(self, **kwargs):
        self.exchange_calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return self.response


class WorkOSSDKBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.user_management = _FakeUserManagement()
        self.client = SimpleNamespace(user_management=self.user_management)
        self.boundary = WorkOSSDKBoundary(self.client)

    def test_hosted_ui_arguments_are_exact_and_magic_auth_projection_is_token_free(self):
        state = secrets.token_urlsafe(32)
        challenge = secrets.token_urlsafe(32)
        redirect_uri = "https://127.0.0.1:9443/auth/workos/callback"
        self.boundary.authorization_url(
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=challenge,
            client_id="client_0123456789abcdef",
        )
        self.assertEqual(
            self.user_management.authorization_calls,
            [
                {
                    "provider": "authkit",
                    "redirect_uri": redirect_uri,
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "max_age": 0,
                    "screen_hint": "sign-in",
                    "client_id": "client_0123456789abcdef",
                }
            ],
        )

        response = SimpleNamespace(
            user=SimpleNamespace(
                id="user_0123456789abcdef",
                email="person@example.test",
                email_verified=True,
            ),
            authentication_method=SimpleNamespace(value="MagicAuth"),
            organization_id=None,
            impersonator=None,
            oauth_tokens=None,
            access_token=object(),
            refresh_token=object(),
            authkit_authorization_code=object(),
        )
        self.user_management.response = response
        code = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(32)
        projected = self.boundary.exchange_code(code=code, code_verifier=verifier)

        self.assertEqual(projected.user_id, "user_0123456789abcdef")
        self.assertEqual(projected.email, "person@example.test")
        self.assertTrue(projected.email_verified)
        self.assertEqual(projected.authentication_method, "MagicAuth")
        self.assertEqual(
            self.user_management.exchange_calls,
            [
                {
                    "code": code,
                    "code_verifier": verifier,
                    "request_options": {
                        "timeout": EXCHANGE_TIMEOUT_SECONDS,
                        "max_retries": EXCHANGE_MAX_RETRIES,
                    },
                }
            ],
        )
        self.assertEqual(EXCHANGE_TIMEOUT_SECONDS, 5.0)
        self.assertEqual(EXCHANGE_MAX_RETRIES, 0)
        self.assertIsNone(response.access_token)
        self.assertIsNone(response.refresh_token)
        self.assertIsNone(response.authkit_authorization_code)
        self.assertIsNone(response.oauth_tokens)
        self.assertEqual(repr(projected), "WorkOSAuthKitAuthentication(<redacted>)")

    def test_sdk_failures_are_detail_free(self):
        canary = secrets.token_urlsafe(32)
        self.user_management.failure = RuntimeError(canary)
        with self.assertRaises(WorkOSAuthKitUnavailable) as caught:
            self.boundary.exchange_code(
                code=secrets.token_urlsafe(32),
                code_verifier=secrets.token_urlsafe(32),
            )
        rendered = repr(caught.exception) + str(caught.exception)
        self.assertNotIn(canary, rendered)
        self.assertEqual(str(caught.exception), "workos_authkit_unavailable")
        self.assertIsNone(caught.exception.__cause__)

    def test_imports_create_no_client_connection_thread_listener_network_or_file(self):
        repository = Path(__file__).resolve().parents[1]
        script = r'''
import os
import socket
import sqlite3
import sys
import threading
import types

events = []

def poison(name):
    def fail(*_args, **_kwargs):
        events.append(name)
        raise AssertionError(name)
    return fail

sqlite3.connect = poison("database_connection")
threading.Thread.start = poison("thread_start")

class PoisonSocket:
    def __init__(self, *_args, **_kwargs):
        events.append("socket_created")
        raise AssertionError("socket_created")

socket.socket = PoisonSocket
provider = types.ModuleType("workos")
provider.WorkOSClient = poison("provider_client")
sys.modules["workos"] = provider
before = tuple(sorted(os.listdir(".")))

import scripts.workos_authkit_provider_migration
import wahojobs.workos_authkit
import wahojobs.workos_authkit_browser
import wahojobs.workos_authkit_schema

after = tuple(sorted(os.listdir(".")))
if events or after != before:
    raise AssertionError((events, before, after))
print("import-isolated")
'''
        with tempfile.TemporaryDirectory(
            prefix="wahojobs-workos-import-test-",
            ignore_cleanup_errors=True,
        ) as directory:
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["PYTHONPATH"] = str(repository) + os.pathsep + environment.get(
                "PYTHONPATH", ""
            )
            completed = subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        self.assertEqual(
            completed.returncode,
            0,
            msg=(completed.stdout + completed.stderr),
        )
        self.assertEqual(completed.stdout.strip(), "import-isolated")


if __name__ == "__main__":
    unittest.main()
