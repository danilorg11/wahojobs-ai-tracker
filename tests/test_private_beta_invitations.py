from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import scripts.google_oidc_authorization_transactions_migration as migration_006
import scripts.private_beta_invitations as cli
from tests.persistent_profile_canonical_v2_test_support import (
    install_canonical_v2_profiles,
)
from wahojobs.accounts import (
    AccountService,
    AuthenticationUnavailable,
    TrustedIdentityVerifier,
    create_invitation,
    invitation_creation_request_fingerprint,
    revoke_invitation,
)
from wahojobs.database_lifetime_ownership import (
    ROLE_OFFLINE_OPERATOR,
    acquire_database_lifetime_ownership,
    release_database_lifetime_ownership,
)
import wahojobs.private_beta_invitation_operations as operations
from wahojobs.private_beta_invitation_operations import (
    PrivateBetaInvitationOperationError,
    create_private_beta_invitation,
    revoke_private_beta_invitation,
    status_private_beta_invitation,
)


ROOT = Path(__file__).resolve().parents[1]
INVITATION_KEY = b"pb-ops-1-test-invitation-key-material-0001"
NOW = datetime(2026, 8, 4, 15, 0, 0, tzinfo=timezone.utc)
EXPIRY = "2026-09-01T12:00:00Z"
REQUEST_ID = "request-id-0000000000000001"

_CRASH_CHECKPOINTS = (
    "before_token_generation",
    "after_invitation_savepoint",
    "before_stage_create",
    "after_stage_create",
    "mid_stage_write",
    "after_stage_write",
    "after_stage_flush",
    "after_stage_close",
    "after_stage_directory_flush",
    "before_database_commit",
    "after_database_commit",
    "before_publication",
    "after_publication_syscall",
    "after_publication_directory_flush",
    "after_publication_confirmed",
    "after_final_revalidation",
    "before_database_close",
    "after_database_close",
    "before_ownership_release",
    "after_ownership_release_before_result",
)

_CONTROL_EXCEPTIONS = (
    RuntimeError,
    KeyboardInterrupt,
    SystemExit,
    GeneratorExit,
)


class _HostileSuccessStream:
    def __init__(self, failure, exception_type):
        self.failure = failure
        self.exception_type = exception_type
        self.content = ""
        self.write_calls = 0

    def write(self, value):
        self.write_calls += 1
        if self.failure == "zero":
            raise self.exception_type("private")
        if self.failure == "partial":
            self.content += value[: max(1, len(value) // 2)]
            raise self.exception_type("private")
        self.content += value
        return len(value)

    def flush(self):
        if self.failure == "flush":
            raise self.exception_type("private")


def _assert_retry_notice(test_case, notice, *, json_output, cleanup=False):
    expected = {
        "error": "COMMITTED_RETRY_REQUIRED",
        "durable_mutation": "MAY_ALREADY_HAVE_OCCURRED",
        "exact_retry_only": True,
        "recovery": "REPEAT_EXACT_INVOCATION",
        "success_requires": "COMPLETE_FRAME_AND_EXIT_0",
    }
    if cleanup:
        expected["cleanup"] = "INCOMPLETE"
    if json_output:
        test_case.assertEqual(json.loads(notice), expected)
        return
    line = (
        "COMMITTED_RETRY_REQUIRED"
        " durable_mutation=MAY_ALREADY_HAVE_OCCURRED"
        " exact_retry_only=true"
        " recovery=REPEAT_EXACT_INVOCATION"
        " success_requires=COMPLETE_FRAME_AND_EXIT_0"
    )
    if cleanup:
        line += " cleanup=INCOMPLETE"
    test_case.assertEqual(notice, line + "\n")

_CRASH_CHILD = r"""
import os
from datetime import datetime, timezone
from wahojobs.private_beta_invitation_operations import create_private_beta_invitation

checkpoint = os.environ["PB_OPS_TEST_CHECKPOINT"]
def abrupt(name):
    if name == checkpoint:
        os._exit(91)

create_private_beta_invitation(
    configuration_path=os.environ["PB_OPS_TEST_CONFIG"],
    database_path=os.environ["PB_OPS_TEST_DATABASE"],
    invitation_key_path=os.environ["PB_OPS_TEST_KEY"],
    request_id="request-id-0000000000000001",
    expires_at="2026-09-01T12:00:00Z",
    credential_output=os.environ["PB_OPS_TEST_OUTPUT"],
    hidden_email_reader=lambda: (
        "private.beta@example.test",
        "private.beta@example.test",
    ),
    _clock=lambda: datetime(2026, 8, 4, 15, 0, 0, tzinfo=timezone.utc),
    _checkpoint=abrupt,
)
raise SystemExit(90)
"""

_CONCURRENT_CREATE_CHILD = r"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from wahojobs.private_beta_invitation_operations import (
    PrivateBetaInvitationOperationError,
    create_private_beta_invitation,
)

barrier = os.environ.get("PB_OPS_TEST_BARRIER")
def checkpoint(name):
    if barrier and name == "before_token_generation":
        marker = Path(barrier + "." + os.environ["PB_OPS_TEST_PARTICIPANT"])
        marker.write_text("ready", encoding="ascii")
        release = Path(barrier + ".release")
        while not release.exists():
            pass

try:
    result = create_private_beta_invitation(
        configuration_path=os.environ["PB_OPS_TEST_CONFIG"],
        database_path=os.environ["PB_OPS_TEST_DATABASE"],
        invitation_key_path=os.environ["PB_OPS_TEST_KEY"],
        request_id=os.environ["PB_OPS_TEST_REQUEST"],
        expires_at="2026-09-01T12:00:00Z",
        credential_output=os.environ["PB_OPS_TEST_OUTPUT"],
        hidden_email_reader=lambda: (
            os.environ["PB_OPS_TEST_EMAIL"],
            os.environ["PB_OPS_TEST_EMAIL"],
        ),
        _clock=lambda: datetime(2026, 8, 4, 15, 0, 0, tzinfo=timezone.utc),
        _checkpoint=checkpoint,
    )
except PrivateBetaInvitationOperationError as error:
    print(json.dumps({"error": error.code, "exit": error.exit_code}, sort_keys=True))
else:
    print(json.dumps({"outcome": result.outcome, "reference": result.invitation_reference}, sort_keys=True))
"""

_ARCHIVE_AUTHORITY_CHILD = r"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

def denied(*_args, **_kwargs):
    raise AssertionError("subprocess_activated")

subprocess.Popen = denied
subprocess.run = denied
subprocess.call = denied
subprocess.check_call = denied
subprocess.check_output = denied

from wahojobs.private_beta_invitation_operations import (
    create_private_beta_invitation,
    revoke_private_beta_invitation,
    status_private_beta_invitation,
)

root = Path.cwd().resolve(strict=True)
for name, module in tuple(sys.modules.items()):
    if not (
        name in {"scripts", "tests", "wahojobs"}
        or name.startswith(("scripts.", "tests.", "wahojobs."))
    ):
        continue
    source = getattr(module, "__file__", None)
    if source is None:
        continue
    Path(source).resolve(strict=True).relative_to(root)

barriers = []
def checkpoint(name):
    barriers.append(name)

common = {
    "configuration_path": os.environ["PB_OPS_TEST_CONFIG"],
    "database_path": os.environ["PB_OPS_TEST_DATABASE"],
    "invitation_key_path": os.environ["PB_OPS_TEST_KEY"],
    "_clock": lambda: datetime(2026, 8, 4, 15, 0, 0, tzinfo=timezone.utc),
    "_checkpoint": checkpoint,
}
created = create_private_beta_invitation(
    **common,
    request_id="archive-request-id-00000000001",
    expires_at="2026-09-01T12:00:00Z",
    credential_output=os.environ["PB_OPS_TEST_OUTPUT"],
    hidden_email_reader=lambda: (
        "archive.private@example.test",
        "archive.private@example.test",
    ),
)
found = status_private_beta_invitation(
    **common,
    invitation_reference=created.invitation_reference,
)
revoked = revoke_private_beta_invitation(
    **common,
    invitation_reference=created.invitation_reference,
)
replayed = revoke_private_beta_invitation(
    **common,
    invitation_reference=created.invitation_reference,
)
required = {
    "before_database_commit",
    "before_publication",
    "before_ownership_release",
    "status_after_row_read",
}
if not required.issubset(barriers):
    raise AssertionError("archive_barriers_not_reached")
print(json.dumps({
    "created": created.outcome,
    "found": found.status,
    "revoked": revoked.status,
    "replayed": replayed.outcome,
    "barriers": sorted(set(barriers)),
}, sort_keys=True))
"""

_STATUS_REVOKE_CHILD = r"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from wahojobs.private_beta_invitation_operations import (
    PrivateBetaInvitationOperationError,
    revoke_private_beta_invitation,
    status_private_beta_invitation,
)

mode = os.environ["PB_OPS_TEST_MODE"]
barrier = os.environ.get("PB_OPS_TEST_BARRIER")
def checkpoint(name):
    if mode == "status" and barrier and name == "status_after_row_read":
        Path(barrier + ".ready").write_text("ready", encoding="ascii")
        release = Path(barrier + ".release")
        while not release.exists():
            pass

common = {
    "configuration_path": os.environ["PB_OPS_TEST_CONFIG"],
    "database_path": os.environ["PB_OPS_TEST_DATABASE"],
    "invitation_key_path": os.environ["PB_OPS_TEST_KEY"],
    "invitation_reference": os.environ["PB_OPS_TEST_REFERENCE"],
    "_clock": lambda: datetime(2026, 8, 4, 15, 0, 0, tzinfo=timezone.utc),
    "_checkpoint": checkpoint,
}
try:
    result = (
        status_private_beta_invitation(**common)
        if mode == "status"
        else revoke_private_beta_invitation(**common)
    )
except PrivateBetaInvitationOperationError as error:
    print(json.dumps({"error": error.code, "exit": error.exit_code}, sort_keys=True))
else:
    print(json.dumps({"outcome": result.outcome, "status": result.status}, sort_keys=True))
"""


@contextmanager
def private_beta_state(*, environment="private_beta", mutate_configuration=None):
    with tempfile.TemporaryDirectory(prefix="wahojobs-pb-ops-1-") as raw:
        directory = Path(raw).resolve(strict=True)
        if directory == ROOT or ROOT in directory.parents:
            raise AssertionError("temporary_state_inside_repository")
        database_path = directory / "private-beta.sqlite"
        connection = install_canonical_v2_profiles(database_path)
        try:
            migration_006.apply_google_oidc_authorization_transactions_migration(
                connection,
                requested_path=database_path,
                expected_identity=migration_006.database_file_identity(database_path),
            )
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.commit()
        finally:
            connection.close()

        client_secret = directory / "google-client-secret.bin"
        invitation_key = directory / "invitation.key"
        lookup_key = directory / "oidc-lookup.key"
        protection_key = directory / "oidc-protection.key"
        _write_secret(client_secret, b"test-client-secret-material")
        _write_secret(invitation_key, INVITATION_KEY)
        _write_secret(lookup_key, b"L" * 32)
        _write_secret(protection_key, b"P" * 32)
        document = {
            "version": 1,
            "environment": environment,
            "database_path": str(database_path),
            "bind_host": "127.0.0.1",
            "bind_port": 8443,
            "public_origin": "https://localhost:8443",
            "google_redirect_uri": "https://localhost:8443/auth/google/callback",
            "google_client_id": "pb-ops-test-client-id",
            "google_client_secret_file": str(client_secret),
            "account_invitation_lookup_key_file": str(invitation_key),
            "oidc_lookup_keys": [{"version": 1, "file": str(lookup_key)}],
            "oidc_lookup_active_version": 1,
            "oidc_protection_keys": [
                {"version": 1, "file": str(protection_key)}
            ],
            "oidc_protection_active_version": 1,
            "session_idle_ttl_seconds": 3600,
            "session_absolute_ttl_seconds": 604800,
            "allowed_post_login_paths": ["/account/profile"],
        }
        if mutate_configuration is not None:
            mutate_configuration(document)
        configuration_path = directory / "private-beta.json"
        configuration_path.write_text(
            json.dumps(
                document,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        if os.name == "posix":
            configuration_path.chmod(0o600)
        output_directory = directory / "credentials"
        output_directory.mkdir(mode=0o700)
        operations._harden_private_output_directory_for_testing(output_directory)
        state = {
            "directory": directory,
            "database": database_path.resolve(strict=True),
            "configuration": configuration_path.resolve(strict=True),
            "key": invitation_key.resolve(strict=True),
            "output_directory": output_directory.resolve(strict=True),
            "output": output_directory / "invitation.json",
        }
        yield state


def _write_secret(path: Path, payload: bytes):
    path.write_bytes(payload)
    if os.name == "posix":
        path.chmod(0o600)


def _create(state, **overrides):
    arguments = {
        "configuration_path": state["configuration"],
        "database_path": state["database"],
        "invitation_key_path": state["key"],
        "request_id": REQUEST_ID,
        "expires_at": EXPIRY,
        "credential_output": state["output"],
        "hidden_email_reader": lambda: (
            "Private.Beta@Example.test",
            "private.beta@example.test",
        ),
        "_clock": lambda: NOW,
    }
    arguments.update(overrides)
    return create_private_beta_invitation(**arguments)


def _status(state, reference, **overrides):
    arguments = {
        "configuration_path": state["configuration"],
        "database_path": state["database"],
        "invitation_key_path": state["key"],
        "invitation_reference": reference,
        "_clock": lambda: NOW,
    }
    arguments.update(overrides)
    return status_private_beta_invitation(**arguments)


def _revoke(state, reference, **overrides):
    arguments = {
        "configuration_path": state["configuration"],
        "database_path": state["database"],
        "invitation_key_path": state["key"],
        "invitation_reference": reference,
        "_clock": lambda: NOW,
    }
    arguments.update(overrides)
    return revoke_private_beta_invitation(**arguments)


def _invitation_rows(database):
    connection = sqlite3.connect(database)
    try:
        return tuple(
            connection.execute(
                "SELECT invitation_id, invitation_status, revoked_at, "
                "idempotency_key, request_fingerprint "
                "FROM account_invitations ORDER BY invitation_id"
            )
        )
    finally:
        connection.close()


def _private_file(path: Path, payload: bytes):
    if os.name == "nt":
        handle = operations._windows_create_private_file(path)
        try:
            operations._windows_write_all(handle, payload)
            operations._windows_flush_file(handle)
        finally:
            operations._windows_close_handle(handle)
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _start_concurrent_create(state, *, participant, request, email, output, barrier=None):
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(ROOT),
            "PB_OPS_TEST_CONFIG": str(state["configuration"]),
            "PB_OPS_TEST_DATABASE": str(state["database"]),
            "PB_OPS_TEST_KEY": str(state["key"]),
            "PB_OPS_TEST_REQUEST": request,
            "PB_OPS_TEST_EMAIL": email,
            "PB_OPS_TEST_OUTPUT": str(output),
            "PB_OPS_TEST_PARTICIPANT": participant,
        }
    )
    if barrier is not None:
        environment["PB_OPS_TEST_BARRIER"] = str(barrier)
    process = subprocess.Popen(
        [sys.executable, "-B", "-"],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    process.stdin.write(_CONCURRENT_CREATE_CHILD)
    process.stdin.close()
    process.stdin = None
    return process


def _start_status_revoke(state, reference, *, mode, barrier=None):
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(ROOT),
            "PB_OPS_TEST_CONFIG": str(state["configuration"]),
            "PB_OPS_TEST_DATABASE": str(state["database"]),
            "PB_OPS_TEST_KEY": str(state["key"]),
            "PB_OPS_TEST_REFERENCE": reference,
            "PB_OPS_TEST_MODE": mode,
        }
    )
    if barrier is not None:
        environment["PB_OPS_TEST_BARRIER"] = str(barrier)
    process = subprocess.Popen(
        [sys.executable, "-B", "-"],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    process.stdin.write(_STATUS_REVOKE_CHILD)
    process.stdin.close()
    process.stdin = None
    return process


class PrivateBetaInvitationParserTests(unittest.TestCase):
    def test_exact_create_grammar(self):
        command = cli._parse(
            [
                "--json",
                "create",
                "--config",
                "C:\\config.json",
                "--database",
                "C:\\database.sqlite",
                "--invitation-key-file",
                "C:\\invitation.key",
                "--request-id",
                REQUEST_ID,
                "--expires-at",
                EXPIRY,
                "--credential-output",
                "C:\\invitation.json",
            ]
        )
        self.assertEqual(command.name, "create")
        self.assertTrue(command.json_output)

    def test_defaults_duplicates_unknowns_and_extra_commands_are_absent(self):
        invalid = (
            ["create"],
            ["list"],
            ["consume"],
            ["--json", "--json", "status"],
            ["status", "--database", "x"],
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with self.assertRaises(cli._SyntaxFailure):
                    cli._parse(list(arguments))

    def test_unsafe_syntax_value_is_not_reflected(self):
        canary = "SECRET-CANARY-DO-NOT-REFLECT"
        with mock.patch.object(sys, "stderr", new_callable=lambda: __import__("io").StringIO()) as stderr:
            code = cli.main([canary])
        self.assertEqual(code, 2)
        self.assertEqual(stderr.getvalue(), "INVALID_INPUT\n")
        self.assertNotIn(canary, stderr.getvalue())


class AccountInvitationDomainTests(unittest.TestCase):
    def test_creation_uses_authoritative_fingerprint_helper(self):
        with private_beta_state() as state:
            connection = sqlite3.connect(state["database"])
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                metadata = {
                    "configuration_binding_sha256": "a" * 64,
                    "operator_protocol": "pb_ops_1_create_v1",
                    "output_binding_sha256": "b" * 64,
                }
                expected = invitation_creation_request_fingerprint(
                    email="person@example.test",
                    lookup_key=INVITATION_KEY,
                    expires_at=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
                    created_by="private_beta_offline_operator",
                    source_metadata=metadata,
                )
                created = create_invitation(
                    connection,
                    email="person@example.test",
                    lookup_key=INVITATION_KEY,
                    expires_at=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
                    created_by="private_beta_offline_operator",
                    idempotency_key="fingerprint-helper-test",
                    source_metadata=metadata,
                    now=NOW,
                )
                stored = connection.execute(
                    "SELECT request_fingerprint FROM account_invitations "
                    "WHERE invitation_id = ?",
                    (created.invitation.invitation_id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(stored, expected)

    def test_revoke_only_mutates_unexpired_pending_row(self):
        with private_beta_state() as state:
            connection = sqlite3.connect(state["database"])
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                created = create_invitation(
                    connection,
                    email="expired@example.test",
                    lookup_key=INVITATION_KEY,
                    expires_at=NOW + timedelta(seconds=1),
                    created_by="test_operator",
                    idempotency_key="expired-revoke-test",
                    now=NOW,
                )
                with self.assertRaises(AuthenticationUnavailable):
                    revoke_invitation(
                        connection,
                        invitation_id=created.invitation.invitation_id,
                        now=NOW + timedelta(seconds=1),
                    )
                row = connection.execute(
                    "SELECT invitation_status, revoked_at FROM account_invitations "
                    "WHERE invitation_id = ?",
                    (created.invitation.invitation_id,),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(tuple(row), ("pending", None))


class PrivateBetaInvitationOperationTests(unittest.TestCase):
    def test_create_status_replay_and_revoke(self):
        with private_beta_state() as state:
            created = _create(state)
            self.assertEqual(created.operation, "create")
            self.assertEqual(created.outcome, "created")
            self.assertEqual(created.email_hint, "p***@example.test")
            self.assertEqual(created.status, "pending")
            payload = json.loads(state["output"].read_text(encoding="ascii"))
            self.assertEqual(payload["format"], operations.ENVELOPE_FORMAT)
            self.assertEqual(payload["invitation_reference"], created.invitation_reference)
            self.assertNotIn("private.beta@example.test", state["output"].name)

            replayed = _create(state)
            self.assertEqual(replayed.outcome, "replayed")
            self.assertEqual(replayed.invitation_reference, created.invitation_reference)
            self.assertEqual(len(_invitation_rows(state["database"])), 1)

            status = _status(state, created.invitation_reference)
            self.assertEqual(status.status, "pending")
            self.assertIsNotNone(status.created_at)
            revoked = _revoke(state, created.invitation_reference)
            self.assertEqual(revoked.status, "revoked")
            replayed_revoke = _revoke(state, created.invitation_reference)
            self.assertEqual(replayed_revoke.outcome, "replayed")
            self.assertEqual(replayed_revoke.status, "revoked")
            row = _invitation_rows(state["database"])[0]
            revoked_at = row[2]
            self.assertEqual(
                _revoke(state, created.invitation_reference).outcome,
                "replayed",
            )
            self.assertEqual(_invitation_rows(state["database"])[0][2], revoked_at)

        revoke_boundaries = (
            "before_database_commit",
            "after_database_commit",
            "before_ownership_release",
            "after_ownership_release_before_result",
        )
        for checkpoint_name in revoke_boundaries:
            for exception_type in _CONTROL_EXCEPTIONS:
                with self.subTest(
                    revoke_boundary=checkpoint_name,
                    exception=exception_type.__name__,
                ), private_beta_state() as state:
                    created = _create(state)

                    def interrupt(name):
                        if name == checkpoint_name:
                            raise exception_type("private")

                    with self.assertRaises(
                        PrivateBetaInvitationOperationError
                    ) as caught:
                        _revoke(
                            state,
                            created.invitation_reference,
                            _checkpoint=interrupt,
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "COMMITTED_RETRY_REQUIRED",
                    )
                    self.assertEqual(caught.exception.exit_code, 8)
                    self.assertNotIn(
                        "private",
                        str(caught.exception) + repr(caught.exception),
                    )
                    first = _invitation_rows(state["database"])[0]
                    self.assertEqual(
                        first[1],
                        (
                            "pending"
                            if checkpoint_name == "before_database_commit"
                            else "revoked"
                        ),
                    )
                    retried = _revoke(state, created.invitation_reference)
                    self.assertIn(retried.outcome, {"revoked", "replayed"})
                    after_retry = _invitation_rows(state["database"])[0]
                    self.assertEqual(after_retry[1], "revoked")
                    self.assertEqual(
                        _revoke(state, created.invitation_reference).outcome,
                        "replayed",
                    )
                    self.assertEqual(
                        _invitation_rows(state["database"])[0],
                        after_retry,
                    )

    def test_status_effective_expiry_does_not_write(self):
        with private_beta_state() as state:
            created = _create(state)
            before = state["database"].read_bytes()
            status = _status(
                state,
                created.invitation_reference,
                _clock=lambda: datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            )
            self.assertEqual(status.status, "expired")
            self.assertEqual(state["database"].read_bytes(), before)
            self.assertEqual(_invitation_rows(state["database"])[0][1], "pending")

    def test_expired_revoke_is_nonmutating_and_reports_effective_state(self):
        with private_beta_state() as state:
            created = _create(state)
            before = _invitation_rows(state["database"])[0]
            with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                _revoke(
                    state,
                    created.invitation_reference,
                    _clock=lambda: datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
                )
            self.assertEqual(caught.exception.code, "INVITATION_NOT_PENDING")
            self.assertEqual(caught.exception.status, "expired")
            self.assertEqual(_invitation_rows(state["database"])[0], before)

    def test_status_and_revoke_never_read_key_bytes_or_hmac(self):
        with private_beta_state() as state:
            created = _create(state)
            with mock.patch.object(
                operations,
                "_read_invitation_key",
                side_effect=AssertionError("key_bytes_read"),
            ), mock.patch.object(
                operations,
                "invitation_secret_hmac",
                side_effect=AssertionError("hmac_computed"),
            ):
                self.assertEqual(_status(state, created.invitation_reference).status, "pending")
                self.assertEqual(_revoke(state, created.invitation_reference).status, "revoked")

    def test_status_reports_consumed_without_identity_or_user_join(self):
        with private_beta_state() as state:
            created = _create(state)
            envelope = json.loads(state["output"].read_text(encoding="ascii"))
            verifier = TrustedIdentityVerifier()
            identity = verifier.from_validated_google_claims(
                provider_subject="pb-ops-consumed-subject",
                verified_email="private.beta@example.test",
                email_verified=True,
                authenticated_at=NOW,
                metadata_version="google_oidc_v1",
            )
            connection = sqlite3.connect(state["database"])
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                AccountService(verifier).create_invited_user(
                    connection,
                    identity=identity,
                    invitation_token=envelope["invitation_credential"],
                    invitation_lookup_key=INVITATION_KEY,
                    idempotency_key="pb-ops-consumption-test",
                    now=NOW,
                )
            finally:
                connection.close()
            with mock.patch.object(
                operations,
                "_read_invitation_key",
                side_effect=AssertionError("key_bytes_read"),
            ):
                status = _status(state, created.invitation_reference)
            self.assertEqual(status.status, "consumed")

    def test_request_conflict_is_nonmutating(self):
        with private_beta_state() as state:
            created = _create(state)
            with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                _create(
                    state,
                    hidden_email_reader=lambda: (
                        "different@example.test",
                        "different@example.test",
                    ),
                )
            self.assertEqual(caught.exception.code, "REQUEST_ID_CONFLICT")
            self.assertEqual(len(_invitation_rows(state["database"])), 1)
            self.assertEqual(_invitation_rows(state["database"])[0][0], created.invitation_reference)

    def test_different_request_cannot_reuse_one_output_path(self):
        with private_beta_state() as state:
            first = _create(state)
            original = state["output"].read_bytes()
            with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                _create(
                    state,
                    request_id="second-request-id-000000001",
                    hidden_email_reader=lambda: (
                        "second@example.test",
                        "second@example.test",
                    ),
                )
            self.assertEqual(caught.exception.exit_code, 6)
            self.assertEqual(state["output"].read_bytes(), original)
            self.assertEqual(len(_invitation_rows(state["database"])), 1)
            self.assertEqual(_invitation_rows(state["database"])[0][0], first.invitation_reference)

    def test_deleted_committed_output_is_unrecoverable_not_reissued(self):
        with private_beta_state() as state:
            created = _create(state)
            state["output"].unlink()
            with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                _create(state)
            self.assertEqual(caught.exception.code, "CREDENTIAL_RECOVERY_UNAVAILABLE")
            self.assertEqual(len(_invitation_rows(state["database"])), 1)
            self.assertFalse(state["output"].exists())
            self.assertEqual(_invitation_rows(state["database"])[0][0], created.invitation_reference)

    def test_private_beta_only_and_explicit_cross_confirmation(self):
        with private_beta_state(environment="test") as state:
            with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                _status(state, "inv_" + "0" * 32)
            self.assertEqual(caught.exception.code, "CONFIGURATION_INVALID")
        with private_beta_state() as state, private_beta_state() as other:
            with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                status_private_beta_invitation(
                    configuration_path=state["configuration"],
                    database_path=other["database"],
                    invitation_key_path=state["key"],
                    invitation_reference="inv_" + "0" * 32,
                )
            self.assertEqual(caught.exception.code, "TARGET_VALIDATION_FAILED")

    def test_invalid_nonsecret_inputs_fail_before_configuration(self):
        cases = (
            {"request_id": "short"},
            {"expires_at": "2026-09-01T12:00:00+00:00"},
            {"expires_at": "2026-02-30T12:00:00Z"},
            {"credential_output": Path("relative.json")},
        )
        with private_beta_state() as state:
            for changes in cases:
                with self.subTest(changes=changes):
                    with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                        _create(state, **changes)
                    self.assertEqual(caught.exception.exit_code, 2)
            with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                _status(state, "INV_" + "0" * 32)
            self.assertEqual(caught.exception.code, "INVITATION_REFERENCE_INVALID")

    def test_hidden_double_entry_occurs_before_ownership(self):
        with private_beta_state() as state:
            coordination = Path(str(state["database"]) + ".wahojobs-lifetime.lock")
            with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                _create(
                    state,
                    hidden_email_reader=lambda: (
                        "one@example.test",
                        "two@example.test",
                    ),
                )
            self.assertEqual(caught.exception.code, "EMAIL_MISMATCH")
            self.assertFalse(coordination.exists())
            self.assertEqual(_invitation_rows(state["database"]), ())

    def test_real_ownership_contention_is_retryable(self):
        with private_beta_state() as state:
            owner = acquire_database_lifetime_ownership(
                state["database"],
                role=ROLE_OFFLINE_OPERATOR,
            )
            try:
                with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                    _status(state, "inv_" + "0" * 32)
                self.assertEqual(caught.exception.code, "OWNERSHIP_BUSY")
                self.assertEqual(caught.exception.exit_code, 4)
            finally:
                release_database_lifetime_ownership(
                    owner,
                    role=ROLE_OFFLINE_OPERATOR,
                    database_path=state["database"],
                )

    def test_sidecar_schema_and_key_fail_closed(self):
        with private_beta_state() as state:
            sidecar = Path(str(state["database"]) + "-wal")
            sidecar.write_bytes(b"unsafe")
            try:
                with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                    _status(state, "inv_" + "0" * 32)
                self.assertEqual(caught.exception.code, "DATABASE_ATTESTATION_FAILED")
            finally:
                sidecar.unlink()
        with private_beta_state() as state:
            state["key"].write_bytes(b"short")
            with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                _create(state)
            self.assertIn(caught.exception.code, {"TARGET_VALIDATION_FAILED", "KEY_SOURCE_INVALID"})

    def test_wrong_migration_and_wal_database_fail_closed(self):
        registered_markers = (
            *migration_006.PREREQUISITE_MIGRATION_VERSIONS,
            migration_006.MIGRATION_VERSION,
        )
        self.assertEqual(
            registered_markers,
            (
                "001_pipeline_state",
                "002_accounts_sessions",
                "003_product_principals",
                "004_persistent_product_profiles",
                "005_persistent_profile_canonical_v2",
                "006_google_oidc_authorization_transactions",
            ),
        )
        for marker in registered_markers:
            with self.subTest(marker=marker), private_beta_state() as state:
                connection = sqlite3.connect(state["database"])
                try:
                    connection.execute(
                        "DELETE FROM wahojobs_schema_migrations WHERE version = ?",
                        (marker,),
                    )
                    connection.commit()
                finally:
                    connection.close()
                with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                    _status(state, "inv_" + "0" * 32)
                self.assertEqual(caught.exception.code, "DATABASE_ATTESTATION_FAILED")
        with private_beta_state() as state:
            connection = sqlite3.connect(state["database"])
            try:
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode = WAL").fetchone()[0],
                    "wal",
                )
            finally:
                connection.close()
            with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                _status(state, "inv_" + "0" * 32)
            self.assertEqual(caught.exception.exit_code, 3)

    def test_unknown_reference_is_redacted(self):
        with private_beta_state() as state:
            with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                _status(state, "inv_" + "0" * 32)
            self.assertEqual(caught.exception.code, "INVITATION_UNKNOWN")
            self.assertEqual(caught.exception.exit_code, 5)
            self.assertNotIn(str(state["database"]), repr(caught.exception))

    def test_database_and_key_identity_replacement_fail_before_sqlite_mutation(self):
        with private_beta_state() as state:
            replacement = state["directory"] / "replacement.sqlite"
            shutil.copy2(state["database"], replacement)

            def replace_database():
                os.replace(replacement, state["database"])
                return ("person@example.test", "person@example.test")

            with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                _create(state, hidden_email_reader=replace_database)
            self.assertIn(caught.exception.code, {"TARGET_VALIDATION_FAILED", "OWNERSHIP_LOST"})
            self.assertEqual(_invitation_rows(state["database"]), ())

        with private_beta_state() as state:
            replacement = state["directory"] / "replacement.key"
            _write_secret(replacement, b"replacement-invitation-key-material-0001")

            def replace_key():
                os.replace(replacement, state["key"])
                return ("person@example.test", "person@example.test")

            with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                _create(state, hidden_email_reader=replace_key)
            self.assertEqual(caught.exception.code, "TARGET_VALIDATION_FAILED")
            self.assertEqual(_invitation_rows(state["database"]), ())

    def test_existing_destination_and_stage_are_never_overwritten(self):
        for target_kind in ("final", "stage"):
            with self.subTest(target_kind=target_kind), private_beta_state() as state:
                if target_kind == "final":
                    target = state["output"]
                else:
                    digest = __import__("hashlib").sha256(
                        str(state["output"]).encode("utf-8")
                    ).hexdigest()
                    target = state["output_directory"] / f".pb-ops-1-{digest}.pending"
                original = b'{"unrelated":true}\n'
                _private_file(target, original)
                with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                    _create(state)
                self.assertEqual(caught.exception.exit_code, 6)
                self.assertEqual(target.read_bytes(), original)
                self.assertEqual(_invitation_rows(state["database"]), ())

    def test_output_parent_and_file_metadata_are_native_private(self):
        with private_beta_state() as state:
            created = _create(state)
            self.assertTrue(created.invitation_reference.startswith("inv_"))
            metadata = os.lstat(state["output"])
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual(metadata.st_nlink, 1)
            if os.name == "nt":
                operations._windows_validate_private_dacl(state["output"])
                operations._windows_require_supported_local_volume(state["output"])
            else:
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
                self.assertEqual(metadata.st_uid, os.geteuid())

        with private_beta_state() as state:
            unsafe = state["directory"] / "unsafe-output"
            unsafe.mkdir()
            if os.name == "posix":
                unsafe.chmod(0o755)
            with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                _create(state, credential_output=unsafe / "invitation.json")
            self.assertEqual(caught.exception.exit_code, 6)

    def test_same_output_across_different_databases_commits_only_one_invitation(self):
        with private_beta_state() as left, private_beta_state() as right:
            shared_output = left["output"]
            barrier = left["directory"] / "concurrent-output-barrier"
            processes = (
                _start_concurrent_create(
                    left,
                    participant="left",
                    request="request-id-left-000000000001",
                    email="left@example.test",
                    output=shared_output,
                    barrier=barrier,
                ),
                _start_concurrent_create(
                    right,
                    participant="right",
                    request="request-id-right-00000000001",
                    email="right@example.test",
                    output=shared_output,
                    barrier=barrier,
                ),
            )
            deadline = time.monotonic() + 20
            markers = (Path(str(barrier) + ".left"), Path(str(barrier) + ".right"))
            while not all(path.exists() for path in markers):
                if time.monotonic() >= deadline:
                    for process in processes:
                        process.kill()
                    self.fail("concurrent_workers_did_not_reach_barrier")
                time.sleep(0.01)
            Path(str(barrier) + ".release").write_text("release", encoding="ascii")
            outcomes = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(stderr, "")
                self.assertEqual(process.returncode, 0)
                outcomes.append(json.loads(stdout))
            successes = [item for item in outcomes if "outcome" in item]
            failures = [item for item in outcomes if "error" in item]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["exit"], 6)
            self.assertEqual(
                len(_invitation_rows(left["database"]))
                + len(_invitation_rows(right["database"])),
                1,
            )
            self.assertTrue(shared_output.is_file())
            self.assertEqual(
                tuple(left["output_directory"].glob("*.pending")),
                (),
            )

    def test_different_databases_and_outputs_operate_independently(self):
        with private_beta_state() as left, private_beta_state() as right:
            left_result = _create(left, request_id="independent-left-000000001")
            right_result = _create(right, request_id="independent-right-00000001")
            self.assertNotEqual(left_result.invitation_reference, right_result.invitation_reference)
            self.assertEqual(len(_invitation_rows(left["database"])), 1)
            self.assertEqual(len(_invitation_rows(right["database"])), 1)

        with private_beta_state() as state:
            created = _create(state)
            barrier = state["directory"] / "status-revoke"
            status_process = _start_status_revoke(
                state,
                created.invitation_reference,
                mode="status",
                barrier=barrier,
            )
            deadline = time.monotonic() + 20
            ready = Path(str(barrier) + ".ready")
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "status_barrier_not_reached")

            blocked_revoke = _start_status_revoke(
                state,
                created.invitation_reference,
                mode="revoke",
            )
            blocked_stdout, blocked_stderr = blocked_revoke.communicate(
                timeout=30
            )
            self.assertEqual(blocked_revoke.returncode, 0, blocked_stderr)
            self.assertEqual(blocked_stderr, "")
            self.assertEqual(
                json.loads(blocked_stdout),
                {"error": "OWNERSHIP_BUSY", "exit": 4},
            )
            self.assertEqual(
                _invitation_rows(state["database"])[0][1],
                "pending",
            )

            Path(str(barrier) + ".release").write_text(
                "release",
                encoding="ascii",
            )
            status_stdout, status_stderr = status_process.communicate(
                timeout=30
            )
            self.assertEqual(status_process.returncode, 0, status_stderr)
            self.assertEqual(status_stderr, "")
            self.assertEqual(
                json.loads(status_stdout),
                {"outcome": "found", "status": "pending"},
            )

            successful_revoke = _start_status_revoke(
                state,
                created.invitation_reference,
                mode="revoke",
            )
            revoke_stdout, revoke_stderr = successful_revoke.communicate(
                timeout=30
            )
            self.assertEqual(successful_revoke.returncode, 0, revoke_stderr)
            self.assertEqual(revoke_stderr, "")
            self.assertEqual(
                json.loads(revoke_stdout),
                {"outcome": "revoked", "status": "revoked"},
            )
            rows = _invitation_rows(state["database"])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1], "revoked")
            self.assertIsNotNone(rows[0][2])
            self.assertEqual(
                _status(state, created.invitation_reference).status,
                "revoked",
            )
            owner = acquire_database_lifetime_ownership(
                state["database"],
                role=ROLE_OFFLINE_OPERATOR,
            )
            try:
                self.assertIsNotNone(owner)
            finally:
                release_database_lifetime_ownership(
                    owner,
                    role=ROLE_OFFLINE_OPERATOR,
                    database_path=state["database"],
                )

    def test_stage_write_and_flush_failures_roll_back_without_residue(self):
        fault_names = (
            ("_windows_write_all", "_write_all"),
            ("_windows_flush_file", "fsync"),
        )
        for windows_name, posix_name in fault_names:
            with self.subTest(fault=windows_name), private_beta_state() as state:
                target_name = windows_name if os.name == "nt" else posix_name
                if os.name == "posix" and posix_name == "fsync":
                    patcher = mock.patch.object(
                        operations.os,
                        "fsync",
                        side_effect=OSError("injected_flush_failure"),
                    )
                else:
                    patcher = mock.patch.object(
                        operations,
                        target_name,
                        side_effect=OSError("injected_stage_failure"),
                    )
                with patcher, self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                    _create(state)
                self.assertIn(caught.exception.exit_code, {6, 7})
                self.assertEqual(_invitation_rows(state["database"]), ())
                self.assertFalse(state["output"].exists())
                self.assertEqual(
                    tuple(state["output_directory"].glob("*.pending")),
                    (),
                )
                self.assertEqual(operations._sqlite_sidecars(state["database"]), ())

    def test_publication_failure_preserves_authenticated_recovery_stage(self):
        for exception_type in _CONTROL_EXCEPTIONS:
            with self.subTest(
                publication_syscall=exception_type.__name__
            ), private_beta_state() as state:
                native_name = (
                    "_windows_move_no_replace_write_through"
                    if os.name == "nt"
                    else "_posix_rename_no_replace"
                )
                with mock.patch.object(
                    operations,
                    native_name,
                    side_effect=exception_type("private"),
                ), self.assertRaises(
                    PrivateBetaInvitationOperationError
                ) as caught:
                    _create(state)
                self.assertEqual(
                    caught.exception.code,
                    "COMMITTED_RETRY_REQUIRED",
                )
                self.assertEqual(caught.exception.exit_code, 8)
                self.assertEqual(len(_invitation_rows(state["database"])), 1)
                self.assertFalse(state["output"].exists())
                self.assertEqual(
                    len(tuple(state["output_directory"].glob("*.pending"))),
                    1,
                )
                recovered = _create(state)
                self.assertEqual(recovered.outcome, "recovered")
                self.assertTrue(state["output"].is_file())
                self.assertEqual(
                    tuple(state["output_directory"].glob("*.pending")),
                    (),
                )

        boundary_points = (
            "before_database_commit",
            "after_database_commit",
            "before_publication",
            "after_publication_syscall",
            "after_publication_confirmed",
            "before_ownership_release",
            "after_ownership_release_before_result",
        )
        for checkpoint_name in boundary_points:
            for exception_type in _CONTROL_EXCEPTIONS:
                with self.subTest(
                    checkpoint=checkpoint_name,
                    exception=exception_type.__name__,
                ), private_beta_state() as state:

                    def interrupt(name):
                        if name == checkpoint_name:
                            raise exception_type("private")

                    with self.assertRaises(
                        PrivateBetaInvitationOperationError
                    ) as caught:
                        _create(state, _checkpoint=interrupt)
                    self.assertEqual(
                        caught.exception.code,
                        "COMMITTED_RETRY_REQUIRED",
                    )
                    self.assertEqual(caught.exception.exit_code, 8)
                    rendered = str(caught.exception) + repr(caught.exception)
                    self.assertNotIn("private", rendered)
                    self.assertEqual(
                        len(_invitation_rows(state["database"])),
                        0 if checkpoint_name == "before_database_commit" else 1,
                    )
                    retried = _create(state)
                    self.assertIn(
                        retried.outcome,
                        {"created", "recovered", "replayed"},
                    )
                    self.assertEqual(len(_invitation_rows(state["database"])), 1)
                    self.assertTrue(state["output"].is_file())
                    self.assertEqual(
                        tuple(state["output_directory"].glob("*.pending")),
                        (),
                    )

        with private_beta_state() as state:
            native_name = (
                "_windows_move_no_replace_write_through"
                if os.name == "nt"
                else "_posix_rename_no_replace"
            )
            with mock.patch.object(
                operations,
                native_name,
                side_effect=OSError("injected_publication_failure"),
            ), self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                _create(state)
            self.assertEqual(caught.exception.code, "COMMITTED_RETRY_REQUIRED")
            self.assertEqual(caught.exception.exit_code, 8)
            self.assertEqual(len(_invitation_rows(state["database"])), 1)
            self.assertFalse(state["output"].exists())
            self.assertEqual(
                len(tuple(state["output_directory"].glob("*.pending"))),
                1,
            )
            recovered = _create(state)
            self.assertEqual(recovered.outcome, "recovered")
            self.assertTrue(state["output"].is_file())
            self.assertEqual(
                tuple(state["output_directory"].glob("*.pending")),
                (),
            )

        recovery_checkpoints = (
            "during_double_link_recovery_validation",
            "before_double_link_stage_unlink",
            "after_double_link_unlink_before_directory_flush",
            "after_double_link_directory_flush_before_confirmation",
            "after_double_link_recovery_confirmed",
        )
        if os.name == "posix":
            fallback_checkpoints = (
                "after_posix_link_before_unlink",
                "after_posix_unlink_before_directory_flush",
            )
            for checkpoint_name in fallback_checkpoints:
                for exception_type in _CONTROL_EXCEPTIONS:
                    with self.subTest(
                        fallback_checkpoint=checkpoint_name,
                        exception=exception_type.__name__,
                    ), private_beta_state() as state:

                        def interrupt(name):
                            if name == checkpoint_name:
                                raise exception_type("private")

                        class NoRenameAt2:
                            pass

                        with mock.patch.object(
                            operations.ctypes,
                            "CDLL",
                            return_value=NoRenameAt2(),
                        ), self.assertRaises(
                            PrivateBetaInvitationOperationError
                        ) as caught:
                            _create(state, _checkpoint=interrupt)
                        self.assertEqual(
                            caught.exception.code,
                            "COMMITTED_RETRY_REQUIRED",
                        )
                        stage = state["output_directory"] / (
                            ".pb-ops-1-"
                            + hashlib.sha256(
                                str(state["output"]).encode("utf-8")
                            ).hexdigest()
                            + ".pending"
                        )
                        if checkpoint_name == "after_posix_link_before_unlink":
                            self.assertTrue(stage.is_file())
                            self.assertTrue(state["output"].is_file())
                            self.assertEqual(os.lstat(stage).st_nlink, 2)
                            self.assertEqual(
                                os.lstat(stage).st_ino,
                                os.lstat(state["output"]).st_ino,
                            )
                        else:
                            self.assertFalse(stage.exists())
                            self.assertEqual(
                                os.lstat(state["output"]).st_nlink,
                                1,
                            )
                        retried = _create(state)
                        self.assertIn(
                            retried.outcome,
                            {"recovered", "replayed"},
                        )
                        self.assertFalse(stage.exists())
                        self.assertEqual(
                            os.lstat(state["output"]).st_nlink,
                            1,
                        )

            for checkpoint_name in recovery_checkpoints:
                with self.subTest(
                    double_link_checkpoint=checkpoint_name
                ), private_beta_state() as state:
                    _create(state)
                    stage = next(
                        state["output_directory"].glob("*.pending"),
                        None,
                    )
                    self.assertIsNone(stage)
                    stage = state["output_directory"] / (
                        ".pb-ops-1-"
                        + hashlib.sha256(
                            str(state["output"]).encode("utf-8")
                        ).hexdigest()
                        + ".pending"
                    )
                    os.link(state["output"], stage)
                    self.assertEqual(os.lstat(stage).st_nlink, 2)

                    def interrupt(name):
                        if name == checkpoint_name:
                            raise RuntimeError("private")

                    with self.assertRaises(
                        PrivateBetaInvitationOperationError
                    ) as caught:
                        _create(state, _checkpoint=interrupt)
                    self.assertEqual(
                        caught.exception.code,
                        "COMMITTED_RETRY_REQUIRED",
                    )
                    retried = _create(state)
                    self.assertIn(retried.outcome, {"recovered", "replayed"})
                    self.assertFalse(stage.exists())
                    self.assertEqual(os.lstat(state["output"]).st_nlink, 1)

            with private_beta_state() as state:
                _create(state)
                stage = state["output_directory"] / (
                    ".pb-ops-1-"
                    + hashlib.sha256(
                        str(state["output"]).encode("utf-8")
                    ).hexdigest()
                    + ".pending"
                )
                third = state["output_directory"] / "third-link"
                os.link(state["output"], stage)
                os.link(state["output"], third)
                with self.assertRaises(
                    PrivateBetaInvitationOperationError
                ) as caught:
                    _create(state)
                self.assertEqual(
                    caught.exception.code,
                    "CREDENTIAL_DESTINATION_UNAVAILABLE",
                )
                self.assertTrue(stage.exists())
                self.assertTrue(state["output"].exists())
                self.assertTrue(third.exists())
                self.assertEqual(os.lstat(stage).st_nlink, 3)
        else:
            with private_beta_state() as state:
                _create(state)
                stage = state["output_directory"] / (
                    ".pb-ops-1-"
                    + hashlib.sha256(
                        str(state["output"]).encode("utf-8")
                    ).hexdigest()
                    + ".pending"
                )
                os.link(state["output"], stage)
                before = (
                    os.lstat(stage).st_ino,
                    os.lstat(state["output"]).st_ino,
                    os.lstat(stage).st_nlink,
                )
                with self.assertRaises(
                    PrivateBetaInvitationOperationError
                ) as caught:
                    _create(state)
                self.assertEqual(
                    caught.exception.code,
                    "CREDENTIAL_DESTINATION_UNAVAILABLE",
                )
                self.assertTrue(stage.exists())
                self.assertTrue(state["output"].exists())
                self.assertEqual(
                    before,
                    (
                        os.lstat(stage).st_ino,
                        os.lstat(state["output"]).st_ino,
                        os.lstat(stage).st_nlink,
                    ),
                )

    def test_database_close_failure_prevents_success_but_retry_replays(self):
        with private_beta_state() as state:
            def retain_live_session(session, *, checkpoint=None):
                return operations._CloseReport(False, True)

            with mock.patch.object(
                operations._DatabaseSession,
                "close",
                retain_live_session,
            ), self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                _create(state)
            self.assertEqual(caught.exception.code, "COMMITTED_RETRY_REQUIRED")
            self.assertEqual(caught.exception.exit_code, 8)
            self.assertTrue(caught.exception.cleanup_incomplete)
            self.assertEqual(len(_invitation_rows(state["database"])), 1)
            self.assertTrue(state["output"].is_file())
            with operations._RETAINED_CLEANUP_LOCK:
                self.assertEqual(len(operations._RETAINED_CLEANUPS), 1)
            self.assertEqual(_create(state).outcome, "replayed")
            with operations._RETAINED_CLEANUP_LOCK:
                self.assertEqual(operations._RETAINED_CLEANUPS, [])

        class RetryableHandle:
            def __init__(self, *, terminal_on_error=False):
                self.closed = False
                self.calls = 0
                self.terminal_on_error = terminal_on_error

            def close(self):
                self.calls += 1
                if self.calls == 1:
                    if self.terminal_on_error:
                        self.closed = True
                    raise RuntimeError("private")
                self.closed = True

        for terminal_on_error in (False, True):
            with self.subTest(terminal_on_error=terminal_on_error):
                handle = RetryableHandle(
                    terminal_on_error=terminal_on_error
                )
                pinned = operations._PinnedConfiguration(
                    path=Path("configuration"),
                    identity=operations._FileIdentity(0, 0, 0, 0, 0, 0, 1),
                    handle=handle,
                    environment="private_beta",
                    database_path=Path("database"),
                    key_path=Path("key"),
                )
                first = pinned.close()
                self.assertTrue(first.exception_observed)
                if terminal_on_error:
                    self.assertTrue(first.terminal)
                    self.assertIsNone(pinned.handle)
                    self.assertEqual(handle.calls, 1)
                else:
                    self.assertFalse(first.terminal)
                    self.assertIs(pinned.handle, handle)
                    second = pinned.close()
                    self.assertTrue(second.terminal)
                    self.assertIsNone(pinned.handle)
                    self.assertEqual(handle.calls, 2)

        read_descriptor, write_descriptor = os.pipe()
        try:
            output = operations._OutputTarget(
                final_path=Path("final"),
                stage_path=Path("stage"),
                parent_path=Path("parent"),
                parent_identity=operations._FileIdentity(0, 0, 0, 0, 0, 0, 1),
                output_binding="0" * 64,
                directory_descriptor=read_descriptor,
            )
            original_close = operations.os.close
            calls = 0

            def fail_once(descriptor):
                nonlocal calls
                if descriptor == read_descriptor and calls == 0:
                    calls += 1
                    raise RuntimeError("private")
                return original_close(descriptor)

            with mock.patch.object(operations.os, "close", fail_once):
                first = output.close()
                self.assertFalse(first.terminal)
                self.assertEqual(output.directory_descriptor, read_descriptor)
                second = output.close()
                self.assertTrue(second.terminal)
                self.assertIsNone(output.directory_descriptor)
        finally:
            os.close(write_descriptor)

        with private_beta_state() as state:
            def retain_live_output(output, *, checkpoint=None):
                return operations._CloseReport(False, True)

            with mock.patch.object(
                operations._OutputTarget,
                "close",
                retain_live_output,
            ), self.assertRaises(
                PrivateBetaInvitationOperationError
            ) as caught:
                _create(
                    state,
                    hidden_email_reader=lambda: ("bad", "bad"),
                )
            self.assertEqual(caught.exception.code, "CLEANUP_INCOMPLETE")
            self.assertEqual(_invitation_rows(state["database"]), ())
            with operations._RETAINED_CLEANUP_LOCK:
                self.assertEqual(len(operations._RETAINED_CLEANUPS), 1)
            self.assertEqual(_create(state).outcome, "created")
            with operations._RETAINED_CLEANUP_LOCK:
                self.assertEqual(operations._RETAINED_CLEANUPS, [])

        for exception_type in _CONTROL_EXCEPTIONS:
            with self.subTest(
                preownership_cleanup=exception_type.__name__
            ), private_beta_state() as state:
                closed = []
                original_configuration_close = (
                    operations._PinnedConfiguration.close
                )

                def observed_configuration_close(configuration, *, checkpoint=None):
                    report = original_configuration_close(
                        configuration,
                        checkpoint=checkpoint,
                    )
                    closed.append(report.terminal)
                    return report

                def interrupt(name):
                    if name == "before_output_close":
                        raise exception_type("private")

                with mock.patch.object(
                    operations._PinnedConfiguration,
                    "close",
                    observed_configuration_close,
                ), self.assertRaises(
                    PrivateBetaInvitationOperationError
                ) as caught:
                    _create(
                        state,
                        hidden_email_reader=lambda: ("bad", "bad"),
                        _checkpoint=interrupt,
                    )
                self.assertEqual(caught.exception.code, "CLEANUP_INCOMPLETE")
                self.assertTrue(closed)
                self.assertTrue(all(closed))
                self.assertEqual(_invitation_rows(state["database"]), ())

        with private_beta_state() as state:
            alternate_key = state["directory"] / "alternate.key"
            _write_secret(alternate_key, b"A" * 32)

            def retain_live_configuration(configuration, *, checkpoint=None):
                return operations._CloseReport(False, True)

            with mock.patch.object(
                operations._PinnedConfiguration,
                "close",
                retain_live_configuration,
            ), self.assertRaises(
                PrivateBetaInvitationOperationError
            ) as caught:
                create_private_beta_invitation(
                    configuration_path=state["configuration"],
                    database_path=state["database"],
                    invitation_key_path=alternate_key.resolve(strict=True),
                    request_id=REQUEST_ID,
                    expires_at=EXPIRY,
                    credential_output=state["output"],
                    hidden_email_reader=lambda: (
                        "private.beta@example.test",
                        "private.beta@example.test",
                    ),
                    _clock=lambda: NOW,
                )
            self.assertEqual(caught.exception.code, "CLEANUP_INCOMPLETE")
            with operations._RETAINED_CLEANUP_LOCK:
                self.assertEqual(len(operations._RETAINED_CLEANUPS), 1)
            self.assertEqual(_create(state).outcome, "created")
            with operations._RETAINED_CLEANUP_LOCK:
                self.assertEqual(operations._RETAINED_CLEANUPS, [])

        with private_beta_state() as state:
            created = _create(state)

            def retain_live_configuration(configuration, *, checkpoint=None):
                return operations._CloseReport(False, True)

            with mock.patch.object(
                operations._PinnedConfiguration,
                "close",
                retain_live_configuration,
            ), self.assertRaises(
                PrivateBetaInvitationOperationError
            ) as caught:
                _status(
                    state,
                    created.invitation_reference,
                )
            self.assertEqual(caught.exception.code, "CLEANUP_INCOMPLETE")
            self.assertEqual(
                _status(state, created.invitation_reference).status,
                "pending",
            )

        with private_beta_state() as state:
            created = _create(state)

            def retain_live_configuration(configuration, *, checkpoint=None):
                return operations._CloseReport(False, True)

            with mock.patch.object(
                operations._PinnedConfiguration,
                "close",
                retain_live_configuration,
            ), self.assertRaises(
                PrivateBetaInvitationOperationError
            ) as caught:
                _revoke(
                    state,
                    created.invitation_reference,
                )
            self.assertEqual(
                caught.exception.code,
                "COMMITTED_RETRY_REQUIRED",
            )
            self.assertTrue(caught.exception.cleanup_incomplete)
            self.assertEqual(
                _revoke(state, created.invitation_reference).outcome,
                "replayed",
            )

        def retained_cleanups():
            with operations._RETAINED_CLEANUP_LOCK:
                return list(operations._RETAINED_CLEANUPS)

        for raw_path in ("pinned_file", "database"):
            for exception_type in _CONTROL_EXCEPTIONS:
                with self.subTest(
                    raw_path=raw_path,
                    close_state="live",
                    exception=exception_type.__name__,
                ), private_beta_state() as state:
                    original_open = operations.os.open
                    original_fdopen = operations.os.fdopen
                    original_close = operations.os.close
                    original_session = operations._DatabaseSession
                    original_release = operations.release_database_lifetime_ownership
                    target_path = (
                        state["configuration"]
                        if raw_path == "pinned_file"
                        else state["database"]
                    )
                    captured = {}
                    failure = {"active": True}
                    close_calls = []
                    events = []
                    sentinel_read, sentinel_write = os.pipe()
                    sentinel_identity = operations._identity(
                        os.fstat(sentinel_read)
                    )

                    def observed_open(path, flags, *args, **kwargs):
                        descriptor = original_open(path, flags, *args, **kwargs)
                        if Path(path) == target_path and (
                            raw_path == "pinned_file"
                            or "descriptor" in captured
                        ):
                            if "descriptor" not in captured:
                                captured["descriptor"] = descriptor
                                captured["identity"] = operations._identity(
                                    os.fstat(descriptor)
                                )
                            else:
                                events.append("new_work")
                        return descriptor

                    def interrupted_fdopen(descriptor, *args, **kwargs):
                        if (
                            raw_path == "pinned_file"
                            and failure["active"]
                            and descriptor == captured.get("descriptor")
                        ):
                            raise exception_type("private")
                        return original_fdopen(descriptor, *args, **kwargs)

                    def interrupted_session(*args, **kwargs):
                        if raw_path == "database" and failure["active"]:
                            captured["descriptor"] = kwargs["descriptor"]
                            captured["identity"] = kwargs["descriptor_identity"]
                            raise exception_type("private")
                        return original_session(*args, **kwargs)

                    def interrupted_close(descriptor):
                        if descriptor == captured.get("descriptor"):
                            if failure["active"]:
                                close_calls.append("failed")
                                raise exception_type("private")
                            if "drained" not in events:
                                events.append("drained")
                                original_close(descriptor)
                                self.assertEqual(
                                    operations._raw_descriptor_state(
                                        descriptor,
                                        captured["identity"],
                                    )[0],
                                    "terminal",
                                )
                                return None
                        return original_close(descriptor)

                    def observed_release(*args, **kwargs):
                        if args and args[0] is captured.get("ownership"):
                            events.append("ownership_released")
                        return original_release(*args, **kwargs)

                    try:
                        with mock.patch.object(
                            operations.os,
                            "open",
                            observed_open,
                        ), mock.patch.object(
                            operations.os,
                            "fdopen",
                            interrupted_fdopen,
                        ), mock.patch.object(
                            operations.os,
                            "close",
                            interrupted_close,
                        ), mock.patch.object(
                            operations,
                            "_DatabaseSession",
                            side_effect=interrupted_session,
                        ), mock.patch.object(
                            operations,
                            "release_database_lifetime_ownership",
                            observed_release,
                        ):
                            with self.assertRaises(
                                PrivateBetaInvitationOperationError
                            ) as caught:
                                _create(state)
                            self.assertEqual(
                                (caught.exception.code, caught.exception.exit_code),
                                ("CLEANUP_INCOMPLETE", 7),
                            )
                            descriptor = captured["descriptor"]
                            self.assertEqual(
                                operations._raw_descriptor_state(
                                    descriptor,
                                    captured["identity"],
                                )[0],
                                "live",
                            )
                            entries = retained_cleanups()
                            self.assertEqual(len(entries), 1)
                            retained = entries[0]
                            self.assertIsNotNone(retained.raw)
                            self.assertEqual(retained.raw.descriptor, descriptor)
                            self.assertEqual(len(close_calls), 2)
                            if raw_path == "database":
                                self.assertIsNotNone(retained.ownership)
                                self.assertTrue(retained.state.raw_ownership_retained)
                                captured["ownership"] = retained.ownership
                                self.assertNotIn("ownership_released", events)
                            else:
                                self.assertIsNone(retained.ownership)

                            if exception_type is RuntimeError:
                                with self.assertRaises(
                                    PrivateBetaInvitationOperationError
                                ) as repeated:
                                    _create(state)
                                self.assertEqual(
                                    (repeated.exception.code, repeated.exception.exit_code),
                                    ("CLEANUP_INCOMPLETE", 7),
                                )
                                self.assertEqual(len(close_calls), 4)
                                repeated_entries = retained_cleanups()
                                self.assertEqual(len(repeated_entries), 1)
                                self.assertIs(repeated_entries[0].raw, retained.raw)
                                self.assertEqual(
                                    operations._raw_descriptor_state(
                                        descriptor,
                                        captured["identity"],
                                    )[0],
                                    "live",
                                )

                            failure["active"] = False
                            self.assertEqual(_create(state).outcome, "created")
                            self.assertEqual(retained_cleanups(), [])
                            self.assertEqual(events[0], "drained")
                            if raw_path == "database":
                                self.assertEqual(
                                    events[:3],
                                    ["drained", "ownership_released", "new_work"],
                                )
                            else:
                                self.assertEqual(events[:2], ["drained", "new_work"])
                            self.assertEqual(
                                operations._raw_descriptor_state(
                                    sentinel_read,
                                    sentinel_identity,
                                )[0],
                                "live",
                            )
                    finally:
                        failure["active"] = False
                        try:
                            operations._drain_retained_cleanup_or_raise()
                        except PrivateBetaInvitationOperationError:
                            operations._drain_retained_cleanup_or_raise()
                        original_close(sentinel_read)
                        original_close(sentinel_write)

        for raw_path in ("pinned_file", "database"):
            for exception_type in _CONTROL_EXCEPTIONS:
                with self.subTest(
                    raw_path=raw_path,
                    close_state="terminal_then_reused",
                    exception=exception_type.__name__,
                ), private_beta_state() as state:
                    original_open = operations.os.open
                    original_fdopen = operations.os.fdopen
                    original_close = operations.os.close
                    original_session = operations._DatabaseSession
                    target_path = (
                        state["configuration"]
                        if raw_path == "pinned_file"
                        else state["database"]
                    )
                    captured = {}
                    failure = {"active": True}
                    replacement = {"descriptor": None}
                    target_close_calls = 0

                    def observed_open(path, flags, *args, **kwargs):
                        descriptor = original_open(path, flags, *args, **kwargs)
                        if (
                            raw_path == "pinned_file"
                            and Path(path) == target_path
                            and "descriptor" not in captured
                        ):
                            captured["descriptor"] = descriptor
                            captured["identity"] = operations._identity(
                                os.fstat(descriptor)
                            )
                        return descriptor

                    def interrupted_fdopen(descriptor, *args, **kwargs):
                        if (
                            raw_path == "pinned_file"
                            and failure["active"]
                            and descriptor == captured.get("descriptor")
                        ):
                            raise exception_type("private")
                        return original_fdopen(descriptor, *args, **kwargs)

                    def interrupted_session(*args, **kwargs):
                        if raw_path == "database" and failure["active"]:
                            captured["descriptor"] = kwargs["descriptor"]
                            captured["identity"] = kwargs["descriptor_identity"]
                            raise exception_type("private")
                        return original_session(*args, **kwargs)

                    def close_then_raise(descriptor):
                        nonlocal target_close_calls
                        if (
                            descriptor == captured.get("descriptor")
                            and replacement["descriptor"] is None
                        ):
                            target_close_calls += 1
                            original_close(descriptor)
                            replacement_descriptor = original_open(
                                state["key"],
                                os.O_RDONLY | getattr(os, "O_BINARY", 0),
                            )
                            replacement["descriptor"] = replacement_descriptor
                            replacement["identity"] = operations._identity(
                                os.fstat(replacement_descriptor)
                            )
                            self.assertEqual(replacement_descriptor, descriptor)
                            raise exception_type("private")
                        return original_close(descriptor)

                    try:
                        with mock.patch.object(
                            operations.os,
                            "open",
                            observed_open,
                        ), mock.patch.object(
                            operations.os,
                            "fdopen",
                            interrupted_fdopen,
                        ), mock.patch.object(
                            operations.os,
                            "close",
                            close_then_raise,
                        ), mock.patch.object(
                            operations,
                            "_DatabaseSession",
                            side_effect=interrupted_session,
                        ):
                            with self.assertRaises(
                                PrivateBetaInvitationOperationError
                            ) as caught:
                                _create(state)
                            self.assertEqual(
                                (caught.exception.code, caught.exception.exit_code),
                                ("CLEANUP_INCOMPLETE", 7),
                            )
                            failure["active"] = False
                            self.assertEqual(
                                operations._raw_descriptor_state(
                                    captured["descriptor"],
                                    captured["identity"],
                                )[0],
                                "reused",
                            )
                            self.assertEqual(retained_cleanups(), [])
                            self.assertEqual(target_close_calls, 1)
                            self.assertEqual(_create(state).outcome, "created")
                            self.assertEqual(target_close_calls, 1)
                            self.assertEqual(
                                operations._raw_descriptor_state(
                                    replacement["descriptor"],
                                    replacement["identity"],
                                )[0],
                                "live",
                            )
                    finally:
                        failure["active"] = False
                        try:
                            operations._drain_retained_cleanup_or_raise()
                        except PrivateBetaInvitationOperationError:
                            operations._drain_retained_cleanup_or_raise()
                        if replacement["descriptor"] is not None:
                            original_close(replacement["descriptor"])

        handoff_boundaries = {
            "pinned_file": (
                ("before_pinned_file_delivery", "source"),
                (
                    "after_pinned_file_delivery_before_acknowledgement",
                    "source",
                ),
                ("before_pinned_configuration_adoption", "source"),
                (
                    "before_pinned_configuration_acknowledgement_transition",
                    "source",
                ),
                (
                    "after_pinned_configuration_acknowledgement_transition",
                    "destination",
                ),
                ("after_pinned_configuration_adoption", "destination"),
            ),
            "database": (
                ("before_database_session_delivery", "source"),
                (
                    "after_database_session_delivery_before_acknowledgement",
                    "source",
                ),
                ("before_database_session_adoption", "source"),
                (
                    "before_database_session_acknowledgement_transition",
                    "source",
                ),
                (
                    "after_database_session_acknowledgement_transition",
                    "destination",
                ),
                ("after_database_session_adoption", "destination"),
            ),
        }

        for raw_path, boundaries in handoff_boundaries.items():
            for exception_type in _CONTROL_EXCEPTIONS:
                for boundary, expected_owner in boundaries:
                    with self.subTest(
                        handoff=raw_path,
                        boundary=boundary,
                        exception=exception_type.__name__,
                        cleanup="live",
                    ), private_beta_state() as state:
                        captured = {}
                        failure = {"active": True}
                        later = {"active": False}
                        close_attempts = []
                        events = []
                        sentinel_read, sentinel_write = os.pipe()
                        sentinel_identity = operations._identity(
                            os.fstat(sentinel_read)
                        )
                        original_adopt_handle = (
                            operations._RawResourceCleanup.adopt_file_handle
                        )
                        original_stage_configuration = (
                            operations._RawResourceCleanup.stage_configuration_handle
                        )
                        original_stage_session = (
                            operations._RawResourceCleanup.stage_database_session
                        )
                        original_raw_close = operations._RawResourceCleanup.close
                        original_configuration_close = (
                            operations._PinnedConfiguration.close
                        )
                        original_session_close = operations._DatabaseSession.close
                        original_project_root = operations._trusted_project_root
                        original_release = (
                            operations.release_database_lifetime_ownership
                        )
                        original_os_close = operations.os.close

                        def observed_adopt_handle(authority, handle):
                            result = original_adopt_handle(authority, handle)
                            if raw_path == "pinned_file" and "authority" not in captured:
                                descriptor = handle.fileno()
                                captured.update(
                                    authority=authority,
                                    descriptor=descriptor,
                                    identity=operations._identity(
                                        os.fstat(descriptor)
                                    ),
                                )
                            return result

                        def observed_stage_configuration(authority, configuration):
                            result = original_stage_configuration(
                                authority,
                                configuration,
                            )
                            if raw_path == "pinned_file":
                                captured["destination"] = configuration
                            return result

                        def observed_stage_session(authority, session):
                            result = original_stage_session(authority, session)
                            if raw_path == "database" and "authority" not in captured:
                                captured.update(
                                    authority=authority,
                                    destination=session,
                                    descriptor=session.descriptor,
                                    identity=session.descriptor_identity,
                                )
                            return result

                        def observed_raw_close(authority, *, checkpoint=None):
                            if authority is captured.get("authority"):
                                if failure["active"]:
                                    close_attempts.append("source")
                                    if later["active"]:
                                        events.append("cleanup_attempt")
                                    return operations._CloseReport(False, True)
                                if later["active"]:
                                    events.append("cleanup_terminal")
                            return original_raw_close(
                                authority,
                                checkpoint=checkpoint,
                            )

                        def observed_configuration_close(
                            configuration,
                            *,
                            checkpoint=None,
                        ):
                            if configuration is captured.get("destination"):
                                if failure["active"]:
                                    close_attempts.append("destination")
                                    if later["active"]:
                                        events.append("cleanup_attempt")
                                    return operations._CloseReport(False, True)
                                if later["active"]:
                                    events.append("cleanup_terminal")
                            return original_configuration_close(
                                configuration,
                                checkpoint=checkpoint,
                            )

                        def observed_session_close(session, *, checkpoint=None):
                            if (
                                session is captured.get("destination")
                                and captured[
                                    "authority"
                                ].handoff_acknowledged_for(session)
                            ):
                                if failure["active"]:
                                    close_attempts.append("destination")
                                    if later["active"]:
                                        events.append("cleanup_attempt")
                                    return operations._CloseReport(False, True)
                                if later["active"]:
                                    events.append("cleanup_terminal")
                            return original_session_close(
                                session,
                                checkpoint=checkpoint,
                            )

                        def observed_project_root():
                            if later["active"]:
                                events.append("new_work")
                            return original_project_root()

                        def observed_release(*args, **kwargs):
                            if later["active"]:
                                events.append("ownership_released")
                            return original_release(*args, **kwargs)

                        def interrupt(name):
                            if name == boundary:
                                raise exception_type("private")

                        try:
                            with mock.patch.object(
                                operations._RawResourceCleanup,
                                "adopt_file_handle",
                                observed_adopt_handle,
                            ), mock.patch.object(
                                operations._RawResourceCleanup,
                                "stage_configuration_handle",
                                observed_stage_configuration,
                            ), mock.patch.object(
                                operations._RawResourceCleanup,
                                "stage_database_session",
                                observed_stage_session,
                            ), mock.patch.object(
                                operations._RawResourceCleanup,
                                "close",
                                observed_raw_close,
                            ), mock.patch.object(
                                operations._PinnedConfiguration,
                                "close",
                                observed_configuration_close,
                            ), mock.patch.object(
                                operations._DatabaseSession,
                                "close",
                                observed_session_close,
                            ), mock.patch.object(
                                operations,
                                "_trusted_project_root",
                                observed_project_root,
                            ), mock.patch.object(
                                operations,
                                "release_database_lifetime_ownership",
                                observed_release,
                            ):
                                with self.assertRaises(
                                    PrivateBetaInvitationOperationError
                                ) as caught:
                                    _create(state, _checkpoint=interrupt)
                                self.assertEqual(
                                    (
                                        caught.exception.code,
                                        caught.exception.exit_code,
                                    ),
                                    ("CLEANUP_INCOMPLETE", 7),
                                )
                                self.assertEqual(
                                    operations._raw_descriptor_state(
                                        captured["descriptor"],
                                        captured["identity"],
                                    )[0],
                                    "live",
                                )
                                entries = retained_cleanups()
                                self.assertEqual(len(entries), 1)
                                retained = entries[0]
                                destination = captured.get("destination")
                                acknowledged = (
                                    destination is not None
                                    and captured[
                                        "authority"
                                    ].handoff_acknowledged_for(destination)
                                )
                                self.assertEqual(
                                    acknowledged,
                                    expected_owner == "destination",
                                )
                                if expected_owner == "source":
                                    self.assertIs(
                                        retained.raw,
                                        captured["authority"],
                                    )
                                    self.assertIsNone(retained.session)
                                    self.assertIsNone(retained.configuration)
                                elif raw_path == "pinned_file":
                                    self.assertIsNone(retained.raw)
                                    self.assertIs(
                                        retained.configuration,
                                        destination,
                                    )
                                else:
                                    self.assertIsNone(retained.raw)
                                    self.assertIs(retained.session, destination)
                                self.assertEqual(close_attempts, [expected_owner] * 2)
                                if raw_path == "database":
                                    self.assertIsNotNone(retained.ownership)
                                    self.assertNotIn(
                                        "ownership_released",
                                        events,
                                    )

                                later["active"] = True
                                with self.assertRaises(
                                    PrivateBetaInvitationOperationError
                                ) as repeated:
                                    _create(state)
                                self.assertEqual(
                                    (
                                        repeated.exception.code,
                                        repeated.exception.exit_code,
                                    ),
                                    ("CLEANUP_INCOMPLETE", 7),
                                )
                                self.assertEqual(
                                    close_attempts,
                                    [expected_owner] * 4,
                                )
                                self.assertEqual(
                                    events,
                                    ["cleanup_attempt", "cleanup_attempt"],
                                )
                                repeated_entries = retained_cleanups()
                                self.assertEqual(len(repeated_entries), 1)
                                self.assertIs(repeated_entries[0], retained)
                                self.assertEqual(
                                    operations._raw_descriptor_state(
                                        captured["descriptor"],
                                        captured["identity"],
                                    )[0],
                                    "live",
                                )
                                events.clear()
                                failure["active"] = False
                                self.assertEqual(_create(state).outcome, "created")
                                self.assertEqual(retained_cleanups(), [])
                                self.assertEqual(events[0], "cleanup_terminal")
                                if raw_path == "database":
                                    self.assertEqual(
                                        events[:3],
                                        [
                                            "cleanup_terminal",
                                            "ownership_released",
                                            "new_work",
                                        ],
                                    )
                                else:
                                    self.assertEqual(
                                        events[:2],
                                        ["cleanup_terminal", "new_work"],
                                    )
                                self.assertEqual(
                                    operations._raw_descriptor_state(
                                        captured["descriptor"],
                                        captured["identity"],
                                    )[0],
                                    "terminal",
                                )
                                self.assertEqual(
                                    operations._raw_descriptor_state(
                                        sentinel_read,
                                        sentinel_identity,
                                    )[0],
                                    "live",
                                )
                        finally:
                            failure["active"] = False
                            try:
                                operations._drain_retained_cleanup_or_raise()
                            except PrivateBetaInvitationOperationError:
                                operations._drain_retained_cleanup_or_raise()
                            original_os_close(sentinel_read)
                            original_os_close(sentinel_write)

        for raw_path in ("pinned_file", "database"):
            for exception_type, boundary, expected_owner in (
                (exception_type, boundary, expected_owner)
                for exception_type in _CONTROL_EXCEPTIONS
                for boundary, expected_owner in handoff_boundaries[raw_path]
            ):
                with self.subTest(
                    handoff=raw_path,
                    boundary=boundary,
                    exception=exception_type.__name__,
                    cleanup="terminal_then_reused",
                ), private_beta_state() as state:
                    captured = {}
                    replacement = {"descriptor": None}
                    target_close_calls = 0
                    original_adopt_handle = (
                        operations._RawResourceCleanup.adopt_file_handle
                    )
                    original_stage_configuration = (
                        operations._RawResourceCleanup.stage_configuration_handle
                    )
                    original_stage_session = (
                        operations._RawResourceCleanup.stage_database_session
                    )
                    original_configuration_close = (
                        operations._PinnedConfiguration.close
                    )
                    original_raw_close = operations._RawResourceCleanup.close
                    original_os_close = operations.os.close
                    original_os_open = operations.os.open

                    def observed_adopt_handle(authority, handle):
                        result = original_adopt_handle(authority, handle)
                        if raw_path == "pinned_file" and "authority" not in captured:
                            descriptor = handle.fileno()
                            captured.update(
                                authority=authority,
                                descriptor=descriptor,
                                identity=operations._identity(
                                    os.fstat(descriptor)
                                ),
                            )
                        return result

                    def observed_stage_configuration(authority, configuration):
                        result = original_stage_configuration(
                            authority,
                            configuration,
                        )
                        if raw_path == "pinned_file":
                            captured["destination"] = configuration
                        return result

                    def observed_stage_session(authority, session):
                        result = original_stage_session(authority, session)
                        if raw_path == "database" and "authority" not in captured:
                            captured.update(
                                authority=authority,
                                destination=session,
                                descriptor=session.descriptor,
                                identity=session.descriptor_identity,
                            )
                        return result

                    def replacement_descriptor():
                        descriptor = original_os_open(
                            state["key"],
                            os.O_RDONLY | getattr(os, "O_BINARY", 0),
                        )
                        replacement["descriptor"] = descriptor
                        replacement["identity"] = operations._identity(
                            os.fstat(descriptor)
                        )
                        self.assertEqual(descriptor, captured["descriptor"])

                    def raw_close_then_raise(authority, *, checkpoint=None):
                        nonlocal target_close_calls
                        report = original_raw_close(
                            authority,
                            checkpoint=checkpoint,
                        )
                        if (
                            authority is captured.get("authority")
                            and expected_owner == "source"
                            and replacement["descriptor"] is None
                        ):
                            self.assertTrue(report.terminal)
                            target_close_calls += 1
                            replacement_descriptor()
                            raise exception_type("private")
                        return report

                    def configuration_close_then_raise(
                        configuration,
                        *,
                        checkpoint=None,
                    ):
                        nonlocal target_close_calls
                        report = original_configuration_close(
                            configuration,
                            checkpoint=checkpoint,
                        )
                        if (
                            configuration is captured.get("destination")
                            and expected_owner == "destination"
                            and replacement["descriptor"] is None
                        ):
                            self.assertTrue(report.terminal)
                            target_close_calls += 1
                            replacement_descriptor()
                            raise exception_type("private")
                        return report

                    def descriptor_close_then_raise(descriptor):
                        nonlocal target_close_calls
                        if (
                            raw_path == "database"
                            and expected_owner == "destination"
                            and descriptor == captured.get("descriptor")
                            and replacement["descriptor"] is None
                        ):
                            target_close_calls += 1
                            original_os_close(descriptor)
                            replacement_descriptor()
                            raise exception_type("private")
                        return original_os_close(descriptor)

                    def interrupt(name):
                        if name == boundary:
                            raise exception_type("private")

                    try:
                        with mock.patch.object(
                            operations._RawResourceCleanup,
                            "adopt_file_handle",
                            observed_adopt_handle,
                        ), mock.patch.object(
                            operations._RawResourceCleanup,
                            "stage_configuration_handle",
                            observed_stage_configuration,
                        ), mock.patch.object(
                            operations._RawResourceCleanup,
                            "stage_database_session",
                            observed_stage_session,
                        ), mock.patch.object(
                            operations._RawResourceCleanup,
                            "close",
                            raw_close_then_raise,
                        ), mock.patch.object(
                            operations._PinnedConfiguration,
                            "close",
                            configuration_close_then_raise,
                        ), mock.patch.object(
                            operations.os,
                            "close",
                            descriptor_close_then_raise,
                        ):
                            with self.assertRaises(
                                PrivateBetaInvitationOperationError
                            ) as caught:
                                _create(state, _checkpoint=interrupt)
                            self.assertEqual(
                                (
                                    caught.exception.code,
                                    caught.exception.exit_code,
                                ),
                                ("CLEANUP_INCOMPLETE", 7),
                            )
                            destination = captured.get("destination")
                            self.assertEqual(
                                destination is not None
                                and captured[
                                    "authority"
                                ].handoff_acknowledged_for(destination),
                                expected_owner == "destination",
                            )
                            self.assertEqual(
                                operations._raw_descriptor_state(
                                    captured["descriptor"],
                                    captured["identity"],
                                )[0],
                                "reused",
                            )
                            self.assertEqual(retained_cleanups(), [])
                            self.assertEqual(target_close_calls, 1)
                            self.assertEqual(_create(state).outcome, "created")
                            self.assertEqual(target_close_calls, 1)
                            self.assertEqual(
                                operations._raw_descriptor_state(
                                    replacement["descriptor"],
                                    replacement["identity"],
                                )[0],
                                "live",
                            )
                    finally:
                        try:
                            operations._drain_retained_cleanup_or_raise()
                        except PrivateBetaInvitationOperationError:
                            operations._drain_retained_cleanup_or_raise()
                        if replacement["descriptor"] is not None:
                            original_os_close(replacement["descriptor"])

    def test_result_and_errors_never_expose_secret_or_target_canaries(self):
        declared_pairs = operations._PUBLIC_ERROR_PAIRS
        public_codes = {code for code, _exit_code in declared_pairs}
        public_exit_codes = {exit_code for _code, exit_code in declared_pairs}
        self.assertEqual(len(declared_pairs), 23)
        self.assertEqual(public_exit_codes, set(range(2, 9)))
        for code in public_codes:
            for exit_code in public_exit_codes:
                with self.subTest(
                    error_pair=(code, exit_code),
                    declared=(code, exit_code) in declared_pairs,
                ):
                    error = PrivateBetaInvitationOperationError(
                        code,
                        exit_code,
                        status="pending",
                        cleanup_incomplete=True,
                    )
                    if (code, exit_code) in declared_pairs:
                        self.assertEqual((error.code, error.exit_code), (code, exit_code))
                        self.assertEqual(error.status, "pending")
                    else:
                        self.assertEqual(
                            (error.code, error.exit_code),
                            ("INTERNAL_FAILURE", 7),
                        )
                        self.assertIsNone(error.status)
                        self.assertFalse(error.cleanup_incomplete)
        self.assertEqual(
            {
                pair
                for pair in declared_pairs
                if pair[0] == "COMMITTED_RETRY_REQUIRED" or pair[1] == 8
            },
            {("COMMITTED_RETRY_REQUIRED", 8)},
        )
        for code, exit_code in (
            (None, 7),
            ("INTERNAL_FAILURE", True),
            (b"INTERNAL_FAILURE", 7),
            ("INTERNAL_FAILURE", 7.0),
        ):
            with self.subTest(invalid_types=(code, exit_code)):
                error = PrivateBetaInvitationOperationError(
                    code,
                    exit_code,
                    status="pending",
                    cleanup_incomplete=True,
                )
                self.assertEqual(
                    (error.code, error.exit_code, error.status),
                    ("INTERNAL_FAILURE", 7, None),
                )
                self.assertFalse(error.cleanup_incomplete)

        with private_beta_state() as state:
            email = "canary.secret.email@example.test"
            request = "canary-request-id-00000000001"
            created = _create(
                state,
                request_id=request,
                hidden_email_reader=lambda: (email, email),
            )
            envelope = json.loads(state["output"].read_text(encoding="ascii"))
            credential = envelope["invitation_credential"]
            rendered = repr(created) + json.dumps(created.approved_fields())
            for canary in (
                email,
                request,
                credential,
                str(state["configuration"]),
                str(state["database"]),
                str(state["key"]),
                str(state["output"]),
            ):
                self.assertNotIn(canary, rendered)
            with self.assertRaises(PrivateBetaInvitationOperationError) as caught:
                _create(state, request_id="different-request-id-0000001")
            error_rendered = str(caught.exception) + repr(caught.exception)
            self.assertNotIn(str(state["database"]), error_rendered)

            for json_output in (False, True):
                record = cli._success_record(
                    created.approved_fields(),
                    json_output=json_output,
                )
                self.assertLessEqual(len(record.encode("utf-8")), 4096)
                self.assertTrue(record.endswith("\n"))
                if json_output:
                    frame = json.loads(record)
                    self.assertEqual(frame["frame"], "pb_ops_1_success_v1")
                    payload = json.dumps(
                        frame["payload"],
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("ascii")
                    self.assertEqual(frame["payload_bytes"], len(payload))
                    self.assertEqual(
                        frame["payload_sha256"],
                        hashlib.sha256(payload).hexdigest(),
                    )
                else:
                    header, payload = record[:-1].split(" payload=", 1)
                    parts = header.split()
                    self.assertEqual(parts[0], "PB_OPS_1_SUCCESS_V1")
                    self.assertEqual(
                        int(parts[1].removeprefix("bytes=")),
                        len(payload.encode("utf-8")),
                    )
                    self.assertEqual(
                        parts[2].removeprefix("sha256="),
                        hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                    )

            command = [
                "create",
                "--config",
                str(state["configuration"]),
                "--database",
                str(state["database"]),
                "--invitation-key-file",
                str(state["key"]),
                "--request-id",
                request,
                "--expires-at",
                EXPIRY,
                "--credential-output",
                str(state["output"]),
            ]
            for json_output in (False, True):
                arguments = (["--json"] if json_output else []) + command
                for exception_type in _CONTROL_EXCEPTIONS:
                    with self.subTest(
                        mode="json" if json_output else "human",
                        boundary="render",
                        exception=exception_type.__name__,
                    ):
                        stdout = io.StringIO()
                        stderr = io.StringIO()
                        with mock.patch.object(
                            cli,
                            "_execute",
                            return_value=created,
                        ), mock.patch.object(
                            cli,
                            "_success_record",
                            side_effect=exception_type("private"),
                        ), mock.patch.object(
                            cli.sys,
                            "stdout",
                            stdout,
                        ), mock.patch.object(
                            cli.sys,
                            "stderr",
                            stderr,
                        ):
                            exit_code = cli.main(arguments)
                        self.assertEqual(exit_code, 8)
                        self.assertEqual(stdout.getvalue(), "")
                        notice = stderr.getvalue()
                        _assert_retry_notice(
                            self,
                            notice,
                            json_output=json_output,
                        )
                        for canary in (
                            email,
                            request,
                            credential,
                            str(state["database"]),
                            str(state["output"]),
                        ):
                            self.assertNotIn(canary, notice)

                    for boundary in ("zero", "partial", "flush"):
                        with self.subTest(
                            mode="json" if json_output else "human",
                            boundary=boundary,
                            exception=exception_type.__name__,
                        ):
                            stdout = _HostileSuccessStream(
                                boundary,
                                exception_type,
                            )
                            stderr = io.StringIO()
                            with mock.patch.object(
                                cli,
                                "_execute",
                                return_value=created,
                            ), mock.patch.object(
                                cli.sys,
                                "stdout",
                                stdout,
                            ), mock.patch.object(
                                cli.sys,
                                "stderr",
                                stderr,
                            ):
                                exit_code = cli.main(arguments)
                            self.assertEqual(exit_code, 8)
                            self.assertEqual(stdout.write_calls, 1)
                            notice = stderr.getvalue()
                            _assert_retry_notice(
                                self,
                                notice,
                                json_output=json_output,
                            )
                            if boundary == "zero":
                                self.assertEqual(stdout.content, "")
                            elif boundary == "partial":
                                self.assertTrue(stdout.content)
                                self.assertFalse(stdout.content.endswith("\n"))
                                if json_output:
                                    with self.assertRaises(json.JSONDecodeError):
                                        json.loads(stdout.content)
                            else:
                                self.assertTrue(stdout.content.endswith("\n"))
                            for canary in (
                                email,
                                request,
                                credential,
                                str(state["database"]),
                                str(state["output"]),
                            ):
                                self.assertNotIn(canary, notice)

    def test_same_request_concurrency_converges_on_one_row_and_credential(self):
        with private_beta_state() as state:
            processes = tuple(
                _start_concurrent_create(
                    state,
                    participant=str(index),
                    request=REQUEST_ID,
                    email="private.beta@example.test",
                    output=state["output"],
                )
                for index in range(2)
            )
            outcomes = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(stderr, "")
                self.assertEqual(process.returncode, 0)
                outcomes.append(json.loads(stdout))
            self.assertEqual(len(_invitation_rows(state["database"])), 1)
            self.assertTrue(state["output"].is_file())
            self.assertEqual(
                sum(item.get("outcome") == "created" for item in outcomes),
                1,
            )
            retryable = [item for item in outcomes if item.get("exit") == 4]
            replayed = [item for item in outcomes if item.get("outcome") == "replayed"]
            self.assertEqual(len(retryable) + len(replayed), 1)
            self.assertEqual(_create(state).outcome, "replayed")

    def test_no_network_runtime_or_provider_activation(self):
        git_variables = {
            "PATH": "hostile-path-must-not-be-used",
            "GIT_DIR": "hostile-git-dir",
            "GIT_WORK_TREE": "hostile-git-work-tree",
            "GIT_COMMON_DIR": "hostile-git-common-dir",
            "GIT_INDEX_FILE": "hostile-git-index",
            "GIT_OBJECT_DIRECTORY": "hostile-git-objects",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": "hostile-git-alternates",
            "GIT_CONFIG": "hostile-git-config",
            "GIT_CONFIG_GLOBAL": "hostile-git-global",
            "GIT_CONFIG_SYSTEM": "hostile-git-system",
            "GIT_CEILING_DIRECTORIES": "hostile-git-ceiling",
        }
        with private_beta_state() as state:
            with mock.patch.object(
                socket,
                "socket",
                side_effect=AssertionError("network_activated"),
            ), mock.patch.object(
                subprocess,
                "Popen",
                side_effect=AssertionError("subprocess_activated"),
            ), mock.patch.object(
                subprocess,
                "run",
                side_effect=AssertionError("subprocess_activated"),
            ), mock.patch.dict(
                os.environ,
                git_variables,
                clear=False,
            ):
                created = _create(state)
                self.assertEqual(created.status, "pending")
                self.assertEqual(
                    _status(state, created.invitation_reference).status,
                    "pending",
                )
                self.assertEqual(
                    _revoke(state, created.invitation_reference).status,
                    "revoked",
                )

            with tempfile.TemporaryDirectory(
                prefix="wahojobs-pb-ops-1-archive-"
            ) as raw:
                archive = Path(raw).resolve(strict=True) / "archive"
                archive.mkdir()
                shutil.copytree(ROOT / "wahojobs", archive / "wahojobs")
                shutil.copytree(ROOT / "scripts", archive / "scripts")
                self.assertFalse((archive / ".git").exists())
                environment = os.environ.copy()
                environment.update(git_variables)
                environment.update(
                    {
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONNOUSERSITE": "1",
                        "PYTHONPATH": str(archive),
                        "PB_OPS_TEST_CONFIG": str(state["configuration"]),
                        "PB_OPS_TEST_DATABASE": str(state["database"]),
                        "PB_OPS_TEST_KEY": str(state["key"]),
                        "PB_OPS_TEST_OUTPUT": str(
                            state["output_directory"] / "archive.json"
                        ),
                    }
                )
                completed = subprocess.run(
                    [sys.executable, "-B", "-"],
                    cwd=archive,
                    env=environment,
                    input=_ARCHIVE_AUTHORITY_CHILD,
                    stdin=None,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertEqual(result["created"], "created")
                self.assertEqual(result["found"], "pending")
                self.assertEqual(result["revoked"], "revoked")
                self.assertEqual(result["replayed"], "replayed")
                self.assertIn("status_after_row_read", result["barriers"])


class PrivateBetaInvitationSubprocessTests(unittest.TestCase):
    def test_redirected_create_has_no_stdin_fallback_and_no_mutation(self):
        with private_beta_state() as state:
            command = [
                sys.executable,
                "-B",
                str(ROOT / "scripts" / "private_beta_invitations.py"),
                "create",
                "--config",
                str(state["configuration"]),
                "--database",
                str(state["database"]),
                "--invitation-key-file",
                str(state["key"]),
                "--request-id",
                REQUEST_ID,
                "--expires-at",
                EXPIRY,
                "--credential-output",
                str(state["output"]),
            ]
            completed = subprocess.run(
                command,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                creationflags=(
                    subprocess.DETACHED_PROCESS if os.name == "nt" else 0
                ),
                start_new_session=(os.name == "posix"),
            )
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(completed.stderr, "CONSOLE_UNAVAILABLE\n")
            self.assertEqual(_invitation_rows(state["database"]), ())
            self.assertFalse(
                Path(str(state["database"]) + ".wahojobs-lifetime.lock").exists()
            )

    def test_status_json_subprocess_is_whitelisted(self):
        with private_beta_state() as state:
            created = _create(state)
            command = [
                sys.executable,
                "-B",
                str(ROOT / "scripts" / "private_beta_invitations.py"),
                "--json",
                "status",
                "--config",
                str(state["configuration"]),
                "--database",
                str(state["database"]),
                "--invitation-key-file",
                str(state["key"]),
                "--invitation-id",
                created.invitation_reference,
            ]
            completed = subprocess.run(
                command,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            fields = json.loads(completed.stdout)
            self.assertEqual(
                set(fields),
                {
                    "frame",
                    "payload",
                    "payload_bytes",
                    "payload_sha256",
                },
            )
            self.assertEqual(fields["frame"], "pb_ops_1_success_v1")
            payload = fields["payload"]
            self.assertEqual(
                set(payload),
                {
                    "operation",
                    "outcome",
                    "invitation_reference",
                    "email_hint",
                    "created_at",
                    "expires_at",
                    "status",
                },
            )
            canonical = json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            self.assertEqual(fields["payload_bytes"], len(canonical))
            self.assertEqual(
                fields["payload_sha256"],
                hashlib.sha256(canonical).hexdigest(),
            )
            self.assertEqual(completed.stderr, "")

    def test_abrupt_death_schedule_converges_on_exact_retry(self):
        committed_checkpoints = set(_CRASH_CHECKPOINTS[10:])
        for checkpoint in _CRASH_CHECKPOINTS:
            with self.subTest(checkpoint=checkpoint), private_beta_state() as state:
                environment = os.environ.copy()
                environment.update(
                    {
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONPATH": str(ROOT),
                        "PB_OPS_TEST_CHECKPOINT": checkpoint,
                        "PB_OPS_TEST_CONFIG": str(state["configuration"]),
                        "PB_OPS_TEST_DATABASE": str(state["database"]),
                        "PB_OPS_TEST_KEY": str(state["key"]),
                        "PB_OPS_TEST_OUTPUT": str(state["output"]),
                    }
                )
                completed = subprocess.run(
                    [sys.executable, "-B", "-"],
                    cwd=ROOT,
                    env=environment,
                    input=_CRASH_CHILD,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(completed.returncode, 91, completed.stderr)
                self.assertEqual(completed.stdout, "")
                self.assertEqual(completed.stderr, "")
                rows_after_death = _invitation_rows(state["database"])
                self.assertEqual(
                    len(rows_after_death),
                    1 if checkpoint in committed_checkpoints else 0,
                )
                retried = _create(state)
                self.assertIn(retried.outcome, {"created", "recovered", "replayed"})
                self.assertEqual(len(_invitation_rows(state["database"])), 1)
                self.assertTrue(state["output"].is_file())
                self.assertEqual(
                    tuple(state["output_directory"].glob("*.pending")),
                    (),
                )
                self.assertEqual(operations._sqlite_sidecars(state["database"]), ())


if __name__ == "__main__":
    unittest.main()
