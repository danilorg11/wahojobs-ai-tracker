import ast
import contextlib
import copy
from datetime import datetime, timedelta
import gc
import gzip
import hashlib
from importlib import metadata
import inspect
import json
from pathlib import Path
import pickle
import re
import socket
import sqlite3
import tempfile
import threading
import types
import unittest
from unittest import mock
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
import weakref
import zlib

from requests.models import PreparedRequest, Response
from requests.exceptions import ConnectTimeout, ConnectionError as RequestsConnectionError

from tests.google_oidc_gateway_test_support import (
    CLIENT_ID,
    CLIENT_SECRET,
    DEFAULT_SUBJECT,
    NOW,
    PRIMARY_SIGNING_FIXTURE,
    REDIRECT_URI,
    ROTATED_SIGNING_FIXTURE,
    assert_rejects_copy_pickle,
    authorization_parameters,
    close_secret_vault,
    completion_policy,
    durable_counts,
    gateway_database,
    jwks_document,
    make_fake_gateway,
    make_real_gateway,
    request_secret_vault,
    seed_existing_google_identity,
    signed_id_token,
    sockets_blocked,
    valid_id_token_claims,
    vault_entry_count,
)
import wahojobs.google_oidc_gateway as gateway_module
from wahojobs.google_oidc_gateway import (
    GoogleOidcAuthorizationTransaction,
    GoogleOidcGateway,
    GoogleOidcGatewayFailure,
    PreparedGoogleOidcAuthorization,
    TrustedGoogleOidcConfiguration,
)


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_PATH = ROOT / "wahojobs" / "google_oidc_gateway.py"
APPROVED_INPUT_HASH = (
    "ad98ae0e20742519e9866531bb24f99a79cec9d100780107e0bae98d640e9c99"
)
APPROVED_LOCK_HASH = (
    "1fadebfc987a09fe5af62ae68f71b170ff91a9500d68e17f8917d1b920511ae9"
)
APPROVED_LOCK_CANONICAL_LF_HASH = (
    "482e87807668f764ec4e95311d8f205b7fc93bd3477fb293a0be62ab9e0e6f05"
)
APPROVED_INPUT_BYTES = 83
APPROVED_LOCK_CANONICAL_LF_BYTES = 23679
APPROVED_LOCK_BYTES = 23998
APPROVED_DIRECT = {
    "authlib": "1.7.2",
    "cryptography": "50.0.0",
    "joserfc": "1.7.4",
    "requests": "2.34.2",
    "workos": "10.2.0",
}
APPROVED_CLOSURE = {
    "anyio": "4.14.2",
    "authlib": "1.7.2",
    "certifi": "2026.7.22",
    "cffi": "2.1.0",
    "charset-normalizer": "3.4.9",
    "cryptography": "50.0.0",
    "h11": "0.16.0",
    "httpcore": "1.0.9",
    "httpx": "0.28.1",
    "idna": "3.18",
    "joserfc": "1.7.4",
    "pycparser": "3.0",
    "pyjwt": "2.13.0",
    "requests": "2.34.2",
    "typing-extensions": "4.16.0",
    "urllib3": "2.7.0",
    "workos": "10.2.0",
}


def _validated_reviewed_git_text(
    path,
    *,
    expected_canonical_sha256,
    canonical_byte_length,
    reviewed_line_ending,
    expected_reviewed_sha256,
    reviewed_byte_length,
):
    source = path.read_bytes()
    if source.startswith(b"\xef\xbb\xbf"):
        raise ValueError("reviewed_text_bom")
    if not source.endswith(b"\n"):
        raise ValueError("reviewed_text_final_newline")
    if b"\r" in source:
        newline_residue = source.replace(b"\r\n", b"")
        if b"\r" in newline_residue or b"\n" in newline_residue:
            raise ValueError("reviewed_text_line_endings")
        canonical = source.replace(b"\r\n", b"\n")
    else:
        canonical = source
    if len(canonical) != canonical_byte_length:
        raise ValueError("reviewed_text_length")
    if hashlib.sha256(canonical).hexdigest() != expected_canonical_sha256:
        raise ValueError("reviewed_text_hash")
    if reviewed_line_ending == b"\n":
        reviewed = canonical
    elif reviewed_line_ending == b"\r\n":
        reviewed = canonical.replace(b"\n", b"\r\n")
    else:
        raise ValueError("reviewed_text_policy")
    if len(reviewed) != reviewed_byte_length:
        raise ValueError("reviewed_artifact_length")
    if hashlib.sha256(reviewed).hexdigest() != expected_reviewed_sha256:
        raise ValueError("reviewed_artifact_hash")
    return canonical


def _lock_entries(source):
    starts = list(
        re.finditer(
            r"(?m)^([a-z0-9][a-z0-9._-]*)==([^\s\\]+)\s+\\\s*$",
            source,
        )
    )
    entries = {}
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(source)
        entries[match.group(1)] = (match.group(2), source[match.start() : end])
    return entries


def _replace_authorization_parameter(prepared, name, value):
    parts = urlsplit(prepared.authorization_url)
    parameters = parse_qs(
        parts.query,
        keep_blank_values=True,
        strict_parsing=True,
        max_num_fields=16,
    )
    parameters[name] = [value]
    query = urlencode(
        [(key, item) for key, values in parameters.items() for item in values]
    )
    return types.SimpleNamespace(
        authorization_url=urlunsplit(
            (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
        )
    )


def _ordinary_reachable_objects(root):
    pending = [root]
    reached = []
    seen = set()
    scalar = (str, bytes, bytearray, int, float, bool, type(None))
    while pending:
        value = pending.pop()
        marker = id(value)
        if marker in seen:
            continue
        seen.add(marker)
        reached.append(value)
        if isinstance(value, scalar):
            continue
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
            continue
        if isinstance(value, (tuple, list, set, frozenset)):
            pending.extend(value)
            continue
        if isinstance(
            value,
            (
                type,
                types.ModuleType,
                types.CodeType,
                types.FrameType,
                types.TracebackType,
                weakref.ReferenceType,
            ),
        ):
            continue
        try:
            namespace = vars(value)
        except TypeError:
            namespace = None
        if namespace:
            pending.extend(namespace.values())
        for cls in type(value).__mro__:
            slots = cls.__dict__.get("__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for slot in slots:
                if slot in {"__dict__", "__weakref__"}:
                    continue
                try:
                    pending.append(object.__getattribute__(value, slot))
                except (AttributeError, TypeError):
                    pass
    return tuple(reached)


def _snapshot_authority_rows(connection):
    snapshot = {}
    for table in (
        "users",
        "auth_identities",
        "persistent_profiles",
        "profile_ownership_bindings",
    ):
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if exists is not None:
            snapshot[table] = tuple(
                tuple(row)
                for row in connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY 1'
                ).fetchall()
            )
    return snapshot


class _SocketsBlockedTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        blocker = sockets_blocked()
        blocker.__enter__()
        self.addCleanup(blocker.__exit__, None, None, None)

    def keep_harness(self, harness):
        self.addCleanup(harness.close)
        return harness

    def keep_vault(self, vault=None):
        vault = vault or request_secret_vault()
        self.addCleanup(close_secret_vault, vault)
        return vault

    def complete_fake(
        self,
        harness,
        database,
        *,
        prepared=None,
        policy=None,
        vault=None,
        callback_url=None,
    ):
        prepared = prepared or harness.gateway.prepare_authorization()
        if callback_url is None:
            callback_url = harness.transport.callback_for(prepared)
        vault = vault or self.keep_vault()
        return harness.gateway.complete_authorization(
            database.connection,
            prepared.transaction,
            callback_url,
            policy or completion_policy(),
            vault,
        )

    def complete_real_failure(self, harness, prepared, callback_url):
        return harness.gateway.complete_authorization(
            None,
            prepared.transaction,
            callback_url,
            None,
            None,
        )


class DependencyAndLockContractTests(unittest.TestCase):
    def test_direct_input_is_exact_and_both_artifacts_keep_reviewed_hashes(self):
        requirements_in = ROOT / "requirements.in"
        requirements_lock = ROOT / "requirements.lock"
        canonical_input = _validated_reviewed_git_text(
            requirements_in,
            expected_canonical_sha256=APPROVED_INPUT_HASH,
            canonical_byte_length=APPROVED_INPUT_BYTES,
            reviewed_line_ending=b"\n",
            expected_reviewed_sha256=APPROVED_INPUT_HASH,
            reviewed_byte_length=APPROVED_INPUT_BYTES,
        )
        _validated_reviewed_git_text(
            requirements_lock,
            expected_canonical_sha256=APPROVED_LOCK_CANONICAL_LF_HASH,
            canonical_byte_length=APPROVED_LOCK_CANONICAL_LF_BYTES,
            reviewed_line_ending=b"\r\n",
            expected_reviewed_sha256=APPROVED_LOCK_HASH,
            reviewed_byte_length=APPROVED_LOCK_BYTES,
        )
        self.assertEqual(
            canonical_input.split(b"\n"),
            [
                b"Authlib==1.7.2",
                b"cryptography==50.0.0",
                b"joserfc==1.7.4",
                b"requests==2.34.2",
                b"workos==10.2.0",
                b"",
            ],
        )

    def test_reviewed_git_text_accepts_only_lf_or_consistent_crlf(self):
        canonical = b"alpha \t\n\nbeta\n"
        expected_hash = hashlib.sha256(canonical).hexdigest()
        invalid = {
            "changed_content": b"alpha \t\n\nzeta\n",
            "missing_final_newline": canonical[:-1],
            "lone_cr": b"alpha \t\rbeta\n",
            "mixed_endings": b"alpha \t\r\n\nbeta\r\n",
            "bom": b"\xef\xbb\xbf" + canonical,
            "unexpected_byte": b"alpha \t\n\nbet\xff\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reviewed.txt"
            for label, source in (
                ("lf", canonical),
                ("crlf", canonical.replace(b"\n", b"\r\n")),
            ):
                with self.subTest(accepted=label):
                    path.write_bytes(source)
                    self.assertEqual(
                        _validated_reviewed_git_text(
                            path,
                            expected_canonical_sha256=expected_hash,
                            canonical_byte_length=len(canonical),
                            reviewed_line_ending=b"\n",
                            expected_reviewed_sha256=expected_hash,
                            reviewed_byte_length=len(canonical),
                        ),
                        canonical,
                    )
            for label, source in invalid.items():
                with self.subTest(rejected=label):
                    path.write_bytes(source)
                    with self.assertRaises(ValueError):
                        _validated_reviewed_git_text(
                            path,
                            expected_canonical_sha256=expected_hash,
                            canonical_byte_length=len(canonical),
                            reviewed_line_ending=b"\n",
                            expected_reviewed_sha256=expected_hash,
                            reviewed_byte_length=len(canonical),
                        )

    def test_lock_is_exact_hash_only_approved_closure(self):
        source = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        entries = _lock_entries(source)
        self.assertEqual(
            {name: version for name, (version, _block) in entries.items()},
            APPROVED_CLOSURE,
        )
        for name, (version, block) in entries.items():
            with self.subTest(package=name):
                self.assertRegex(version, r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
                self.assertNotIn(">=", block)
                self.assertRegex(block, r"--hash=sha256:[0-9a-f]{64}")
        self.assertNotRegex(
            source,
            r"(?im)^\s*(?:--index-url|--extra-index-url|--trusted-host|"
            r"-e\s|--editable|https?://|file:|git\+|--find-links)",
        )

    def test_installed_direct_versions_are_exact(self):
        self.assertEqual(
            {name: metadata.version(name) for name in APPROVED_DIRECT},
            APPROVED_DIRECT,
        )

    def test_gateway_imports_only_reviewed_protocol_dependencies(self):
        source = GATEWAY_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        related_roots = {
            name.split(".", 1)[0]
            for name in imported
            if name.split(".", 1)[0]
            in {
                "authlib",
                "joserfc",
                "requests",
                "oauthlib",
                "requests_oauthlib",
                "jwt",
                "jose",
                "jwcrypto",
                "httpx",
                "cryptography",
            }
        }
        self.assertEqual(related_roots, {"authlib", "joserfc", "requests"})
        self.assertNotIn("authlib.jose", imported)
        self.assertNotIn("authlib.jose", source)

    def test_gateway_delegates_jose_and_contains_no_manual_jwt_verifier(self):
        source = GATEWAY_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots = {
            (node.module or "").split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        imported_roots.update(
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertNotIn("base64", imported_roots)
        self.assertNotIn("cryptography", imported_roots)
        for forbidden in (
            "urlsafe_b64decode",
            "b64decode",
            ".split(\".\")",
            ".rsplit(\".\")",
            "verify_signature",
            "load_pem_",
            "RSAAlgorithm",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertIn("dependencies.jwt.decode(", source)
        self.assertIn("dependencies.CodeIDToken(", source)
        self.assertIn("key_set_type.import_key_set(document)", source)
        self.assertIn('algorithms=["RS256"]', source)

    def test_no_alternative_dependency_manifest_was_added(self):
        for name in (
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "Pipfile",
            "Pipfile.lock",
            "poetry.lock",
            "pdm.lock",
            "requirements.txt",
        ):
            with self.subTest(name=name):
                self.assertFalse((ROOT / name).exists())


class TrustedConfigurationTests(_SocketsBlockedTestCase):
    def test_authorized_configuration_fixes_the_reviewed_policy(self):
        harness = self.keep_harness(make_real_gateway())
        prepared = harness.gateway.prepare_authorization()
        self.assertIs(type(harness.configuration), TrustedGoogleOidcConfiguration)
        self.assertIs(type(harness.gateway), GoogleOidcGateway)
        parameters = authorization_parameters(prepared)
        endpoint = urlsplit(prepared.authorization_url)
        self.assertEqual(
            (endpoint.scheme, endpoint.netloc, endpoint.path),
            (
                "https",
                "accounts.google.com",
                "/o/oauth2/v2/auth",
            ),
        )
        self.assertEqual(parameters["client_id"], CLIENT_ID)
        self.assertEqual(parameters["redirect_uri"], REDIRECT_URI)
        self.assertEqual(parameters["response_type"], "code")
        self.assertEqual(parameters["scope"], "openid email")
        self.assertEqual(parameters["code_challenge_method"], "S256")
        configuration_record = object.__getattribute__(
            harness.configuration,
            "_TrustedGoogleOidcConfiguration__record",
        )
        self.assertEqual(parameters["max_age"], "86400")
        self.assertEqual(
            int(parameters["max_age"]),
            configuration_record.maximum_authentication_age_seconds,
        )
        self.assertEqual(
            json.loads(parameters["claims"]),
            {"id_token": {"auth_time": {"essential": True}}},
        )
        self.assertNotIn("prompt", parameters)

        pairs = parse_qsl(
            endpoint.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=16,
        )
        for case, replacement in (("missing", None), ("wrong", "86399")):
            with self.subTest(max_age=case):
                changed_pairs = [
                    (name, replacement if name == "max_age" else value)
                    for name, value in pairs
                    if name != "max_age" or replacement is not None
                ]
                changed_url = urlunsplit(
                    (
                        endpoint.scheme,
                        endpoint.netloc,
                        endpoint.path,
                        urlencode(changed_pairs),
                        endpoint.fragment,
                    )
                )
                with self.assertRaises(gateway_module._Unavailable):
                    gateway_module._validate_prepared_authorization_url(
                        changed_url,
                        configuration_record,
                        parameters["state"],
                        parameters["nonce"],
                    )

    def test_direct_construction_subclass_duck_and_lookalike_are_rejected(self):
        for constructor, arguments in (
            (TrustedGoogleOidcConfiguration, ()),
            (GoogleOidcGateway, ()),
            (GoogleOidcGateway, (object(),)),
        ):
            with self.subTest(constructor=constructor.__name__, arguments=arguments):
                with self.assertRaises(TypeError):
                    constructor(*arguments)

        with self.assertRaises(TypeError):
            class ConfigurationSubclass(TrustedGoogleOidcConfiguration):
                pass

        class ConfigurationDuck:
            provider = "google"
            environment = "test"

            def __repr__(self):
                return "TrustedGoogleOidcConfiguration(<redacted>)"

        with self.assertRaises(TypeError):
            GoogleOidcGateway(ConfigurationDuck())
        reconstructed = object.__new__(TrustedGoogleOidcConfiguration)
        with self.assertRaises(TypeError):
            GoogleOidcGateway(reconstructed)

    def test_configuration_is_immutable_noncopyable_and_nonserializable(self):
        harness = self.keep_harness(make_fake_gateway())
        configuration = harness.configuration
        assert_rejects_copy_pickle(configuration)
        with self.assertRaises(AttributeError):
            configuration.provider = "google"
        with self.assertRaises(TypeError):
            json.dumps(configuration)
        self.assertEqual(
            repr(configuration),
            "TrustedGoogleOidcConfiguration(<redacted>)",
        )
        self.assertEqual(str(configuration), repr(configuration))
        original_record = object.__getattribute__(configuration, "_record")
        with self.assertRaises(AttributeError):
            object.__setattr__(configuration, "_record", object())
        object.__setattr__(
            configuration,
            "_TrustedGoogleOidcConfiguration__record",
            object(),
        )
        rejected = harness.gateway.prepare_authorization()
        self.assertEqual(rejected.status, "unavailable")
        object.__setattr__(
            configuration,
            "_TrustedGoogleOidcConfiguration__record",
            original_record,
        )

    def test_callback_extensions_cannot_override_trusted_configuration_or_transport(self):
        from authlib.integrations.requests_client import OAuth2Session

        original_fetch = OAuth2Session.fetch_token
        for name, value in (
            ("client_id", "attacker-client"),
            ("redirect_uri", "https://attacker.invalid/callback"),
            ("provider", "attacker"),
            ("environment", "production"),
            ("header", "X-Override"),
            ("cookie", "override"),
            ("profile", "someone-else"),
        ):
            with self.subTest(name=name):
                harness = self.keep_harness(make_real_gateway())
                prepared = harness.gateway.prepare_authorization()
                callback = harness.transport.callback_for(
                    prepared,
                    code="configuration-extension-code",
                    extra_pairs=((name, value),),
                )
                with mock.patch.object(
                    OAuth2Session,
                    "fetch_token",
                    autospec=True,
                    side_effect=original_fetch,
                ) as fetch_token:
                    result = self.complete_real_failure(
                        harness,
                        prepared,
                        callback,
                    )
                self.assertEqual(result.status, "unavailable")
                fetch_token.assert_called_once()
                authorization_response = fetch_token.call_args.kwargs[
                    "authorization_response"
                ]
                self.assertEqual(
                    {
                        field
                        for field, _item in parse_qsl(
                            urlsplit(authorization_response).query,
                            keep_blank_values=True,
                            strict_parsing=True,
                        )
                    },
                    {"code", "iss", "state"},
                )
                self.assertNotIn(name, authorization_response)
                self.assertNotIn(value, authorization_response)
                token = harness.transport.observations[0]
                self.assertTrue(token.exact_client)
                self.assertTrue(token.exact_redirect)
                self.assertTrue(token.exact_pkce)

    def test_deployment_constructor_has_no_test_authority_inputs(self):
        parameters = inspect.signature(GoogleOidcGateway).parameters
        self.assertEqual(
            set(parameters),
            {
                "client_id",
                "client_secret",
                "redirect_uri",
                "environment_namespace",
            },
        )
        for forbidden in (
            "provider",
            "provider_adapter",
            "transport",
            "claims",
            "projection",
            "key_set",
            "decoder",
            "clock",
            "monotonic_clock",
        ):
            self.assertNotIn(forbidden, parameters)
        with self.assertRaises(TypeError):
            GoogleOidcGateway(
                client_id=CLIENT_ID,
                client_secret=bytearray(CLIENT_SECRET),
                redirect_uri=REDIRECT_URI,
                environment_namespace="production",
            )
        self.assertNotIn("issue_configuration", gateway_module.__all__)
        self.assertNotIn("make_gateway", gateway_module.__all__)

    def test_secret_is_redacted_and_not_ordinarily_reachable(self):
        secret = bytearray(CLIENT_SECRET)
        secret_text = bytes(secret).decode("ascii")
        harness = self.keep_harness(make_fake_gateway(client_secret=secret))
        prepared = harness.gateway.prepare_authorization()
        self.assertEqual(secret, bytearray())
        for value in (
            harness.configuration,
            harness.gateway,
            prepared,
            prepared.transaction,
        ):
            with self.subTest(value=type(value).__name__):
                self.assertNotIn(secret_text, repr(value))
                self.assertNotIn(secret_text, str(value))
        for value in (prepared, prepared.transaction):
            reached = _ordinary_reachable_objects(value)
            self.assertFalse(
                any(
                    type(item) is bytearray
                    and bytes(item) == CLIENT_SECRET
                    for item in reached
                )
            )

        configuration_record = object.__getattribute__(
            harness.configuration,
            "_record",
        )
        credential = configuration_record.credential
        credential_record = object.__getattribute__(credential, "_record")
        live_reached = _ordinary_reachable_objects(
            (harness.configuration, harness.gateway)
        )
        secret_buffers = [
            item
            for item in live_reached
            if type(item) is bytearray and bytes(item) == CLIENT_SECRET
        ]
        self.assertEqual(len(secret_buffers), 1)
        self.assertIs(secret_buffers[0], credential_record.secret_buffer)
        self.assertFalse(
            any(
                (type(item) is str and item == secret_text)
                or (type(item) is bytes and item == CLIENT_SECRET)
                for item in live_reached
            )
        )
        self.assertEqual(
            repr(credential),
            "_GoogleClientCredential(<redacted>)",
        )
        harness.close()
        self.assertEqual(credential_record.secret_buffer, bytearray())
        self.assertEqual(credential_record.digest, b"")


class AuthorizationTransactionTests(_SocketsBlockedTestCase):
    def test_preparation_binds_redirect_state_nonce_and_pkce_s256(self):
        harness = self.keep_harness(make_real_gateway())
        prepared = harness.gateway.prepare_authorization()
        parameters = authorization_parameters(prepared)
        self.assertIs(type(prepared), PreparedGoogleOidcAuthorization)
        self.assertIs(
            type(prepared.transaction),
            GoogleOidcAuthorizationTransaction,
        )
        self.assertEqual(prepared.transaction.status, "fresh")
        self.assertEqual(
            prepared.transaction.expires_at - prepared.transaction.created_at,
            timedelta(minutes=10),
        )
        self.assertEqual(parameters["redirect_uri"], REDIRECT_URI)
        self.assertEqual(parameters["code_challenge_method"], "S256")
        self.assertRegex(parameters["state"], r"^[A-Za-z0-9_-]{32,}$")
        self.assertRegex(parameters["nonce"], r"^[A-Za-z0-9_-]{32,}$")
        self.assertRegex(parameters["code_challenge"], r"^[A-Za-z0-9_-]{43}$")
        self.assertNotEqual(parameters["state"], parameters["nonce"])
        self.assertNotIn("code_verifier", parameters)

    def test_fresh_in_progress_consumed_and_concurrent_claim_has_one_winner(self):
        harness = self.keep_harness(make_fake_gateway(block=True))
        prepared = harness.gateway.prepare_authorization()
        transaction = prepared.transaction
        callback = harness.transport.callback_for(prepared)
        first_results = []

        def complete_first():
            first_results.append(
                harness.gateway.complete_authorization(
                    None,
                    transaction,
                    callback,
                    None,
                    None,
                )
            )

        thread = threading.Thread(target=complete_first)
        thread.start()
        try:
            self.assertTrue(harness.fake_provider.entered.wait(timeout=2))
            self.assertEqual(transaction.status, "in_progress")
            second = harness.gateway.complete_authorization(
                None,
                transaction,
                callback,
                None,
                None,
            )
            self.assertEqual(
                second.status,
                "invalid_or_expired_transaction",
            )
        finally:
            harness.fake_provider.release.set()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(first_results), 1)
        self.assertEqual(first_results[0].status, "unavailable")
        self.assertEqual(transaction.status, "consumed")
        self.assertEqual(harness.fake_provider.call_count, 1)

    def test_expiry_accepts_last_second_and_rejects_exact_boundary(self):
        with gateway_database(suffix="transaction-last-second") as database:
            harness = self.keep_harness(
                make_fake_gateway(subject=database.subject)
            )
            prepared = harness.gateway.prepare_authorization()
            harness.clock.advance(599)
            result = self.complete_fake(
                harness,
                database,
                prepared=prepared,
            )
            self.assertEqual(result.status, "issued")

        boundary = self.keep_harness(make_fake_gateway())
        prepared = boundary.gateway.prepare_authorization()
        callback = boundary.transport.callback_for(prepared)
        boundary.clock.advance(600)
        result = boundary.gateway.complete_authorization(
            None,
            prepared.transaction,
            callback,
            None,
            None,
        )
        self.assertEqual(result.status, "invalid_or_expired_transaction")
        self.assertEqual(boundary.fake_provider.call_count, 0)
        self.assertEqual(prepared.transaction.status, "consumed")

    def test_clock_rollback_and_provider_delay_expire_as_invalid_transaction(self):
        rollback = self.keep_harness(make_fake_gateway())
        rollback_prepared = rollback.gateway.prepare_authorization()
        rollback_callback = rollback.transport.callback_for(
            rollback_prepared
        )
        rollback.clock.advance_wall(-1)
        rollback_result = rollback.gateway.complete_authorization(
            None,
            rollback_prepared.transaction,
            rollback_callback,
            None,
            None,
        )
        self.assertEqual(
            rollback_result.status,
            "invalid_or_expired_transaction",
        )
        self.assertEqual(rollback.fake_provider.call_count, 0)
        self.assertEqual(rollback_prepared.transaction.status, "consumed")

        delayed = self.keep_harness(make_fake_gateway(block=True))
        delayed_prepared = delayed.gateway.prepare_authorization()
        delayed_callback = delayed.transport.callback_for(delayed_prepared)
        outcomes = []

        def complete_after_provider():
            outcomes.append(
                delayed.gateway.complete_authorization(
                    None,
                    delayed_prepared.transaction,
                    delayed_callback,
                    None,
                    None,
                )
            )

        thread = threading.Thread(target=complete_after_provider)
        thread.start()
        try:
            self.assertTrue(delayed.fake_provider.entered.wait(timeout=2))
            delayed.clock.advance(600)
        finally:
            delayed.fake_provider.release.set()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(
            outcomes[0].status,
            "invalid_or_expired_transaction",
        )
        self.assertEqual(delayed.fake_provider.call_count, 1)
        self.assertEqual(delayed_prepared.transaction.status, "consumed")

    def test_terminal_failure_is_one_use_and_fresh_attempt_is_required(self):
        harness = self.keep_harness(
            make_fake_gateway(
                outcomes=("authentication_denied", "success"),
            )
        )
        first = harness.gateway.prepare_authorization()
        first_callback = harness.transport.callback_for(first)
        denied = harness.gateway.complete_authorization(
            None,
            first.transaction,
            first_callback,
            None,
            None,
        )
        self.assertEqual(denied.status, "authentication_denied")
        replay = harness.gateway.complete_authorization(
            None,
            first.transaction,
            first_callback,
            None,
            None,
        )
        self.assertEqual(replay.status, "invalid_or_expired_transaction")

        with gateway_database() as database:
            second = harness.gateway.prepare_authorization()
            issued = self.complete_fake(
                harness,
                database,
                prepared=second,
            )
            self.assertEqual(issued.status, "issued")
        self.assertEqual(harness.fake_provider.call_count, 2)

    def test_foreign_gateway_rejects_without_claiming_owner_transaction(self):
        owner = self.keep_harness(make_fake_gateway())
        foreign = self.keep_harness(make_fake_gateway())
        prepared = owner.gateway.prepare_authorization()
        callback = owner.transport.callback_for(prepared)
        result = foreign.gateway.complete_authorization(
            None,
            prepared.transaction,
            callback,
            None,
            None,
        )
        self.assertEqual(result.status, "invalid_or_expired_transaction")
        self.assertEqual(prepared.transaction.status, "fresh")
        owner_result = owner.gateway.complete_authorization(
            None,
            prepared.transaction,
            callback,
            None,
            None,
        )
        self.assertEqual(owner_result.status, "unavailable")
        self.assertEqual(prepared.transaction.status, "consumed")

    def test_copy_reconstruction_replacement_and_tampering_are_rejected(self):
        owner = self.keep_harness(make_fake_gateway())
        foreign = self.keep_harness(make_fake_gateway())
        prepared = owner.gateway.prepare_authorization()
        transaction = prepared.transaction
        callback = owner.transport.callback_for(prepared)
        assert_rejects_copy_pickle(transaction)

        reconstructed = object.__new__(GoogleOidcAuthorizationTransaction)
        for replacement in (
            reconstructed,
            prepared,
            types.SimpleNamespace(
                status="fresh",
                created_at=transaction.created_at,
                expires_at=transaction.expires_at,
            ),
        ):
            with self.subTest(replacement=type(replacement).__name__):
                result = owner.gateway.complete_authorization(
                    None,
                    replacement,
                    callback,
                    None,
                    None,
                )
                self.assertEqual(
                    result.status,
                    "invalid_or_expired_transaction",
                )

        object.__setattr__(
            transaction,
            "_gateway_reference",
            weakref.ref(foreign.gateway),
        )
        tampered = owner.gateway.complete_authorization(
            None,
            transaction,
            callback,
            None,
            None,
        )
        self.assertEqual(tampered.status, "invalid_or_expired_transaction")
        self.assertEqual(transaction.status, "consumed")
        self.assertEqual(owner.fake_provider.call_count, 0)

    def test_every_terminal_path_clears_prepared_url(self):
        harness = self.keep_harness(
            make_fake_gateway(outcomes=("provider_unavailable",))
        )
        prepared = harness.gateway.prepare_authorization()
        callback = harness.transport.callback_for(prepared)
        authorization_url = prepared.authorization_url
        self.assertIn("state=", authorization_url)
        result = harness.gateway.complete_authorization(
            None,
            prepared.transaction,
            callback,
            None,
            None,
        )
        self.assertEqual(result.status, "provider_unavailable")
        self.assertEqual(prepared.transaction.status, "consumed")
        with self.assertRaises(TypeError):
            _ = prepared.authorization_url
        self.assertNotIn(authorization_url, repr(prepared))


class RealProtocolAdapterTests(_SocketsBlockedTestCase):
    def claim_failure(
        self,
        *,
        claims_overrides=None,
        missing_claims=(),
        signing_fixture=None,
        algorithm="RS256",
        header_overrides=None,
        expected="authentication_denied",
    ):
        harness = self.keep_harness(make_real_gateway())
        prepared = harness.gateway.prepare_authorization()
        callback = harness.transport.callback_for(
            prepared,
            claims_overrides=claims_overrides,
            missing_claims=missing_claims,
            signing_fixture=signing_fixture,
            algorithm=algorithm,
            header_overrides=header_overrides,
        )
        result = self.complete_real_failure(harness, prepared, callback)
        self.assertEqual(result.status, expected)
        return harness, result

    def test_valid_exchange_verification_and_bounded_transport(self):
        with gateway_database() as database:
            harness = self.keep_harness(make_real_gateway())
            prepared = harness.gateway.prepare_authorization()
            callback = harness.transport.callback_for(prepared)
            vault = self.keep_vault()
            result = harness.gateway.complete_authorization(
                database.connection,
                prepared.transaction,
                callback,
                completion_policy(),
                vault,
            )
            self.assertEqual(result.status, "issued")
            self.assertEqual(vault_entry_count(vault), 1)
            observations = harness.transport.observations
            self.assertEqual([item.role for item in observations], ["token", "jwks"])
            token, jwks = observations
            self.assertEqual(token.method, "POST")
            self.assertEqual(
                token.url,
                "https://oauth2.googleapis.com/token",
            )
            self.assertEqual(token.timeout, (3, 5))
            self.assertTrue(token.verify_enabled)
            self.assertTrue(token.streamed)
            self.assertEqual(token.accept_encoding, "gzip, deflate")
            self.assertTrue(token.exact_client)
            self.assertTrue(token.exact_redirect)
            self.assertTrue(token.exact_pkce)
            self.assertEqual(jwks.method, "GET")
            self.assertEqual(
                jwks.url,
                "https://www.googleapis.com/oauth2/v3/certs",
            )
            self.assertEqual(jwks.timeout, (3, 5))
            self.assertTrue(jwks.verify_enabled)
            self.assertTrue(jwks.streamed)
            self.assertEqual(jwks.accept_encoding, "gzip, deflate")
            self.assertEqual(prepared.transaction.status, "consumed")

    def test_state_mismatch_is_invalid_transaction_without_exchange(self):
        harness = self.keep_harness(make_real_gateway())
        prepared = harness.gateway.prepare_authorization()
        callback = harness.transport.callback_for(
            prepared,
            state="different-state",
        )
        result = self.complete_real_failure(harness, prepared, callback)
        self.assertEqual(result.status, "invalid_or_expired_transaction")
        self.assertEqual(harness.transport.token_request_count, 0)

    def test_changed_redirect_uri_is_denied_before_exchange(self):
        harness = self.keep_harness(make_real_gateway())
        prepared = harness.gateway.prepare_authorization()
        callback = harness.transport.callback_for(
            prepared,
            base_uri="https://other.test.invalid/callback",
        )
        result = self.complete_real_failure(harness, prepared, callback)
        self.assertEqual(result.status, "authentication_denied")
        self.assertEqual(harness.transport.token_request_count, 0)

    def test_wrong_pkce_verifier_is_denied_by_exchange(self):
        harness = self.keep_harness(make_real_gateway())
        prepared = harness.gateway.prepare_authorization()
        wrong_challenge = _replace_authorization_parameter(
            prepared,
            "code_challenge",
            "A" * 43,
        )
        callback = harness.transport.callback_for(
            wrong_challenge,
            code="wrong-pkce-code",
        )
        result = self.complete_real_failure(harness, prepared, callback)
        self.assertEqual(result.status, "authentication_denied")
        self.assertEqual(harness.transport.token_request_count, 1)
        self.assertFalse(harness.transport.observations[0].exact_pkce)
        self.assertEqual(harness.transport.jwks_request_count, 0)

    def test_callback_provider_error_is_generic_denial(self):
        harness = self.keep_harness(make_real_gateway())
        prepared = harness.gateway.prepare_authorization()
        callback = harness.transport.callback_for(
            prepared,
            error="access_denied",
        )
        result = self.complete_real_failure(harness, prepared, callback)
        self.assertEqual(result.status, "authentication_denied")
        self.assertEqual(harness.transport.token_request_count, 0)

    def test_malformed_duplicate_and_oversized_callback_parameters(self):
        harness = self.keep_harness(make_real_gateway())
        malformed = harness.gateway.prepare_authorization()
        duplicate = harness.gateway.prepare_authorization()
        oversized = harness.gateway.prepare_authorization()
        callbacks = (
            (
                malformed,
                REDIRECT_URI + "?code",
            ),
            (
                duplicate,
                harness.transport.callback_for(
                    duplicate,
                    code="duplicate-state-code",
                    extra_pairs=(("state", "duplicate"),),
                ),
            ),
            (
                oversized,
                REDIRECT_URI + "?code=" + ("x" * 8_200) + "&state=x",
            ),
        )
        for prepared, callback in callbacks:
            with self.subTest(callback_size=len(callback)):
                result = self.complete_real_failure(
                    harness,
                    prepared,
                    callback,
                )
                self.assertEqual(result.status, "authentication_denied")
        self.assertEqual(harness.transport.token_request_count, 0)

    def test_wrong_signature_and_unapproved_algorithm_are_denied(self):
        with self.subTest(case="wrong_signature"):
            harness, _result = self.claim_failure(
                signing_fixture=ROTATED_SIGNING_FIXTURE,
                header_overrides={"kid": PRIMARY_SIGNING_FIXTURE.kid},
            )
            self.assertEqual(harness.transport.jwks_request_count, 1)
        with self.subTest(case="unapproved_algorithm"):
            harness, _result = self.claim_failure(algorithm="HS256")
            self.assertEqual(harness.transport.jwks_request_count, 1)

    def test_issuer_audience_and_authorized_party_matrix(self):
        cases = (
            ("issuer", {"iss": "https://attacker.invalid"}, ()),
            ("audience", {"aud": "different-client"}, ()),
            (
                "multiple_audience",
                {"aud": [CLIENT_ID, "different-client"]},
                (),
            ),
            ("wrong_azp", {"azp": "different-client"}, ()),
            (
                "missing_required_azp",
                {"aud": [CLIENT_ID, "different-client"]},
                ("azp",),
            ),
        )
        for name, overrides, missing in cases:
            with self.subTest(name=name):
                self.claim_failure(
                    claims_overrides=overrides,
                    missing_claims=missing,
                )

    def test_required_claim_numeric_types_and_legacy_issuer(self):
        for claim in ("iss", "sub", "aud", "exp", "iat", "nonce"):
            with self.subTest(missing=claim):
                self.claim_failure(missing_claims=(claim,))

        numeric_cases = (
            ("exp_float", {"exp": float(int(NOW.timestamp()) + 300)}),
            ("iat_bool", {"iat": True}),
            ("nbf_string", {"nbf": str(int(NOW.timestamp()))}),
        )
        for name, overrides in numeric_cases:
            with self.subTest(name=name):
                self.claim_failure(claims_overrides=overrides)

        with gateway_database() as database:
            harness = self.keep_harness(make_real_gateway())
            prepared = harness.gateway.prepare_authorization()
            callback = harness.transport.callback_for(
                prepared,
                claims_overrides={"iss": "accounts.google.com"},
            )
            vault = self.keep_vault()
            result = harness.gateway.complete_authorization(
                database.connection,
                prepared.transaction,
                callback,
                completion_policy(),
                vault,
            )
            self.assertEqual(result.status, "issued")

    def test_wrong_nonce_expiry_and_future_time_matrix(self):
        timestamp = lambda value: int(value.timestamp())
        cases = (
            ("nonce", {"nonce": "different-nonce"}),
            (
                "expired",
                {"exp": timestamp(NOW - timedelta(seconds=61))},
            ),
            (
                "future_iat",
                {"iat": timestamp(NOW + timedelta(seconds=61))},
            ),
            (
                "future_nbf",
                {"nbf": timestamp(NOW + timedelta(seconds=61))},
            ),
        )
        for name, overrides in cases:
            with self.subTest(name=name):
                self.claim_failure(claims_overrides=overrides)

    def test_authentication_time_matrix(self):
        timestamp = lambda value: int(value.timestamp())
        cases = (
            ("missing", {}, ("auth_time",)),
            (
                "stale",
                {
                    "auth_time": timestamp(
                        NOW
                        - timedelta(
                            seconds=(
                                gateway_module._MAX_AUTHENTICATION_AGE_SECONDS
                                + gateway_module._CLOCK_SKEW_SECONDS
                                + 1
                            )
                        )
                    )
                },
                (),
            ),
            (
                "future",
                {"auth_time": timestamp(NOW + timedelta(seconds=61))},
                (),
            ),
            ("malformed", {"auth_time": "not-a-number"}, ()),
            (
                "contradictory",
                {
                    "auth_time": timestamp(NOW),
                    "iat": timestamp(NOW - timedelta(seconds=61)),
                },
                (),
            ),
        )
        for name, overrides, missing in cases:
            with self.subTest(name=name):
                self.claim_failure(
                    claims_overrides=overrides,
                    missing_claims=missing,
                )

    def test_unknown_key_forces_one_refresh_and_can_succeed(self):
        with gateway_database() as database:
            harness = self.keep_harness(make_real_gateway())
            harness.transport.queue_jwks_response(
                document=jwks_document(PRIMARY_SIGNING_FIXTURE)
            )
            harness.transport.queue_jwks_response(
                document=jwks_document(ROTATED_SIGNING_FIXTURE)
            )
            prepared = harness.gateway.prepare_authorization()
            callback = harness.transport.callback_for(
                prepared,
                signing_fixture=ROTATED_SIGNING_FIXTURE,
            )
            vault = self.keep_vault()
            result = harness.gateway.complete_authorization(
                database.connection,
                prepared.transaction,
                callback,
                completion_policy(),
                vault,
            )
            self.assertEqual(result.status, "issued")
            self.assertEqual(harness.transport.jwks_request_count, 2)

    def test_second_unknown_key_failure_is_denied(self):
        harness = self.keep_harness(make_real_gateway())
        harness.transport.queue_jwks_response(
            document=jwks_document(PRIMARY_SIGNING_FIXTURE)
        )
        harness.transport.queue_jwks_response(
            document=jwks_document(PRIMARY_SIGNING_FIXTURE)
        )
        prepared = harness.gateway.prepare_authorization()
        callback = harness.transport.callback_for(
            prepared,
            signing_fixture=ROTATED_SIGNING_FIXTURE,
        )
        result = self.complete_real_failure(harness, prepared, callback)
        self.assertEqual(result.status, "authentication_denied")
        self.assertEqual(harness.transport.jwks_request_count, 2)

    def test_malformed_oversized_empty_and_wrong_shape_jwks_fail_closed(self):
        plans = (
            ("malformed", {"body": b"{"}),
            (
                "oversized",
                {
                    "document": jwks_document(PRIMARY_SIGNING_FIXTURE),
                    "declared_length": 256 * 1024 + 1,
                },
            ),
            (
                "streamed_oversized",
                {"body": b"x" * (256 * 1024 + 1)},
            ),
            ("empty", {"document": {"keys": []}}),
            ("wrong_shape", {"document": {"keys": "not-a-list"}}),
        )
        for name, plan in plans:
            with self.subTest(name=name):
                harness = self.keep_harness(make_real_gateway())
                harness.transport.queue_jwks_response(**plan)
                prepared = harness.gateway.prepare_authorization()
                callback = harness.transport.callback_for(prepared)
                result = self.complete_real_failure(
                    harness,
                    prepared,
                    callback,
                )
                self.assertEqual(result.status, "provider_unavailable")
                self.assertEqual(harness.transport.jwks_request_count, 1)

    def test_token_and_jwks_transport_errors_are_provider_unavailable(self):
        cases = (
            ("token", ConnectTimeout("fixture-timeout")),
            ("jwks", RequestsConnectionError("fixture-connection")),
        )
        for role, exception in cases:
            with self.subTest(role=role):
                harness = self.keep_harness(make_real_gateway())
                if role == "token":
                    harness.transport.queue_token_response(exception=exception)
                else:
                    harness.transport.queue_jwks_response(exception=exception)
                prepared = harness.gateway.prepare_authorization()
                callback = harness.transport.callback_for(prepared)
                result = self.complete_real_failure(
                    harness,
                    prepared,
                    callback,
                )
                self.assertEqual(result.status, "provider_unavailable")
                self.assertEqual(harness.transport.token_request_count, 1)
                self.assertEqual(
                    harness.transport.jwks_request_count,
                    int(role == "jwks"),
                )

    def test_token_wrong_origin_redirect_and_history_fail_closed(self):
        plans = (
            {
                "document": {"id_token": "unused"},
                "url": "https://attacker.invalid/token",
            },
            {
                "document": {"id_token": "unused"},
                "status": 302,
                "location": "https://attacker.invalid/token",
            },
            {
                "document": {"id_token": "unused"},
                "history": True,
            },
        )
        for plan in plans:
            with self.subTest(plan=tuple(sorted(plan))):
                harness = self.keep_harness(make_real_gateway())
                harness.transport.queue_token_response(**plan)
                prepared = harness.gateway.prepare_authorization()
                callback = harness.transport.callback_for(prepared)
                result = self.complete_real_failure(
                    harness,
                    prepared,
                    callback,
                )
                self.assertEqual(result.status, "provider_unavailable")
                self.assertEqual(harness.transport.token_request_count, 1)
                self.assertEqual(harness.transport.jwks_request_count, 0)

    def test_oversized_malformed_and_wrong_type_token_response_fail_closed(self):
        plans = (
            (
                "oversized",
                {
                    "body": b"{}",
                    "declared_length": 64 * 1024 + 1,
                },
            ),
            (
                "streamed_oversized",
                {"body": b"x" * (64 * 1024 + 1)},
            ),
            ("malformed", {"body": b"{"}),
            (
                "wrong_content_type",
                {
                    "document": {"id_token": "unused"},
                    "content_type": "text/plain",
                },
            ),
        )
        for name, plan in plans:
            with self.subTest(name=name):
                harness = self.keep_harness(make_real_gateway())
                harness.transport.queue_token_response(**plan)
                prepared = harness.gateway.prepare_authorization()
                callback = harness.transport.callback_for(prepared)
                result = self.complete_real_failure(
                    harness,
                    prepared,
                    callback,
                )
                self.assertEqual(result.status, "provider_unavailable")
                self.assertEqual(harness.transport.token_request_count, 1)
                self.assertEqual(harness.transport.jwks_request_count, 0)

    def test_raw_provider_outputs_are_not_retained_by_success(self):
        with gateway_database() as database:
            harness = self.keep_harness(make_real_gateway())
            prepared = harness.gateway.prepare_authorization()
            nonce = authorization_parameters(prepared)["nonce"]
            claims = valid_id_token_claims(nonce=nonce)
            claims["test_marker"] = "provider-raw-marker"
            raw_id_token = signed_id_token(claims)
            callback = harness.transport.callback_for(
                prepared,
                raw_id_token=raw_id_token,
            )
            vault = self.keep_vault()
            result = harness.gateway.complete_authorization(
                database.connection,
                prepared.transaction,
                callback,
                completion_policy(),
                vault,
            )
            self.assertEqual(result.status, "issued")
            reached = _ordinary_reachable_objects(result)
            for forbidden in (
                raw_id_token,
                "test-access-token-not-retained",
                "test-refresh-token-not-retained",
                "provider-raw-marker",
                DEFAULT_SUBJECT,
            ):
                with self.subTest(forbidden=forbidden[:24]):
                    self.assertNotIn(forbidden, reached)
                    self.assertNotIn(forbidden, repr(result))
            self.assertEqual(harness.transport.pending_authorization_count, 0)


class StrictCallbackDecodingMatrixTests(_SocketsBlockedTestCase):
    FIELDS = (
        "code",
        "state",
        "iss",
        "error",
        "error_description",
        "error_uri",
        "future_extension",
    )
    INVALID_COMPONENT_SUFFIXES = (
        ("incomplete_percent", "%"),
        ("non_hex_percent", "%GG"),
        ("invalid_utf8", "%FF"),
        ("replacement_character", "%EF%BF%BD"),
        ("nul", "%00"),
        ("c0_control", "%1F"),
        ("del", "%7F"),
        ("percent_decoded_control", "%0A"),
    )

    @staticmethod
    def _percent_ascii(value):
        return "".join(f"%{ord(character):02X}" for character in value)

    @staticmethod
    def _raw_callback(fields):
        return REDIRECT_URI + "?" + "&".join(
            f"{name}={value}" for name, value in fields
        )

    @classmethod
    def _fields_for_component(
        cls,
        prepared,
        target,
        *,
        raw_name,
        raw_value,
    ):
        state = authorization_parameters(prepared)["state"]
        code = "phase2-callback-no-exchange-code"
        if target == "code":
            return (
                (raw_name, raw_value),
                ("state", state),
                ("iss", "https://accounts.google.com"),
            )
        if target == "state":
            return (
                ("code", code),
                (raw_name, raw_value),
                ("iss", "https://accounts.google.com"),
            )
        if target == "iss":
            return (
                ("code", code),
                ("state", state),
                (raw_name, raw_value),
            )
        if target == "error":
            return (
                (raw_name, raw_value),
                ("state", state),
                ("iss", "https://accounts.google.com"),
            )
        if target in {"error_description", "error_uri"}:
            return (
                ("error", "access_denied"),
                (raw_name, raw_value),
                ("state", state),
                ("iss", "https://accounts.google.com"),
            )
        if target == "future_extension":
            return (
                ("code", code),
                ("state", state),
                ("iss", "https://accounts.google.com"),
                (raw_name, raw_value),
            )
        raise AssertionError("unknown_callback_field")

    def _assert_invalid_callback(self, fields):
        harness = make_real_gateway()
        prepared = harness.gateway.prepare_authorization()
        callback_fields = fields(prepared)
        callback = (
            callback_fields
            if type(callback_fields) is str
            else self._raw_callback(callback_fields)
        )
        original_exchange = gateway_module._exchange_code
        original_claims = gateway_module._validated_code_id_token
        original_resolve = gateway_module._resolve_durable_identity
        original_issue = (
            gateway_module.TrustedExternalIdentityAuthentication._issue
        )
        original_delegate = gateway_module.complete_trusted_login
        try:
            with (
                mock.patch.object(
                    gateway_module,
                    "_exchange_code",
                    side_effect=original_exchange,
                ) as exchange,
                mock.patch.object(
                    gateway_module,
                    "_validated_code_id_token",
                    side_effect=original_claims,
                ) as validate_claims,
                mock.patch.object(
                    gateway_module,
                    "_resolve_durable_identity",
                    side_effect=original_resolve,
                ) as resolve,
                mock.patch.object(
                    gateway_module.TrustedExternalIdentityAuthentication,
                    "_issue",
                    side_effect=original_issue,
                ) as issue_proof,
                mock.patch.object(
                    gateway_module,
                    "complete_trusted_login",
                    side_effect=original_delegate,
                ) as delegate,
                mock.patch("logging.Logger._log") as logger,
            ):
                result = self.complete_real_failure(
                    harness,
                    prepared,
                    callback,
                )
            self.assertEqual(result.status, "authentication_denied")
            exchange.assert_not_called()
            validate_claims.assert_not_called()
            resolve.assert_not_called()
            issue_proof.assert_not_called()
            delegate.assert_not_called()
            logger.assert_not_called()
            self.assertEqual(harness.transport.token_request_count, 0)
            self.assertEqual(harness.transport.jwks_request_count, 0)
            self.assertEqual(
                harness.transport.response_lifecycles,
                (),
            )
            self.assertEqual(prepared.transaction.status, "consumed")
            gateway_record = object.__getattribute__(
                harness.gateway,
                "_record",
            )
            transaction_record = gateway_record.transactions.get(
                prepared.transaction
            )
            if transaction_record is not None:
                self.assertEqual(
                    transaction_record.lifecycle,
                    "consumed",
                )
                self.assertTrue(
                    all(
                        value == bytearray()
                        for value in (
                            transaction_record.state,
                            transaction_record.nonce,
                            transaction_record.pkce_verifier,
                            transaction_record.b2d1_request_key,
                            transaction_record.authorization_url_buffer,
                        )
                    )
                )
            with self.assertRaises(TypeError):
                _ = prepared.authorization_url
            public_text = " ".join(
                (
                    repr(result),
                    repr(harness.gateway),
                    repr(prepared),
                    repr(prepared.transaction),
                )
            )
            self.assertNotIn(callback, public_text)
        finally:
            harness.close()

    def _assert_four_field_callback_rejected_before_lookup(self, fields):
        harness = self.keep_harness(make_real_gateway())
        prepared = harness.gateway.prepare_authorization()
        callback_fields = fields(prepared)
        self.assertEqual(len(callback_fields), 4)
        self.assertLess(
            len(callback_fields),
            gateway_module._CALLBACK_PARAMETER_LIMIT,
        )
        callback = self._raw_callback(callback_fields)
        with self.assertRaises(gateway_module._InvalidTransaction):
            gateway_module._durable_google_oidc_callback_state(
                harness.gateway,
                callback,
            )
        self.assertEqual(prepared.transaction.status, "fresh")
        gateway_record = object.__getattribute__(
            harness.gateway,
            "_record",
        )
        transaction_record = gateway_record.transactions.get(
            prepared.transaction
        )
        self.assertEqual(transaction_record.lifecycle, "fresh")
        self.assertEqual(harness.transport.token_request_count, 0)
        self.assertEqual(harness.transport.jwks_request_count, 0)
        self._assert_invalid_callback(fields)

    def _assert_extension_field_accepted_and_discarded(self, name, value):
        from authlib.integrations.requests_client import OAuth2Session

        harness = self.keep_harness(make_real_gateway())
        prepared = harness.gateway.prepare_authorization()
        state = authorization_parameters(prepared)["state"]
        code = "accepted-extension-code"
        harness.transport.callback_for(prepared, code=code)
        fields = (
            (name, value),
            ("state", state),
            ("iss", "https://accounts.google.com"),
            ("code", code),
        )
        callback = self._raw_callback(fields)
        authoritative_pairs, authoritative_values, has_error = (
            gateway_module._validated_callback_parameters(
                urlsplit(callback).query
            )
        )
        self.assertFalse(has_error)
        self.assertEqual(
            authoritative_pairs,
            (
                ("state", state),
                ("iss", "https://accounts.google.com"),
                ("code", code),
            ),
        )
        self.assertEqual(
            set(authoritative_values),
            {"code", "iss", "state"},
        )
        self.assertNotIn(name, authoritative_values)
        self.assertEqual(
            gateway_module._durable_google_oidc_callback_state(
                harness.gateway,
                callback,
            ),
            state,
        )
        self.assertEqual(prepared.transaction.status, "fresh")

        original_fetch = OAuth2Session.fetch_token
        original_validate = gateway_module._validated_code_id_token
        with (
            mock.patch.object(
                OAuth2Session,
                "fetch_token",
                autospec=True,
                side_effect=original_fetch,
            ) as fetch_token,
            mock.patch.object(
                gateway_module,
                "_validated_code_id_token",
                side_effect=original_validate,
            ) as validate_id_token,
            mock.patch("logging.Logger._log") as logger,
        ):
            result = self.complete_real_failure(
                harness,
                prepared,
                callback,
            )
        self.assertEqual(result.status, "unavailable")
        fetch_token.assert_called_once()
        validate_id_token.assert_called_once()
        logger.assert_not_called()
        authorization_response = fetch_token.call_args.kwargs[
            "authorization_response"
        ]
        self.assertEqual(
            {
                field
                for field, _item in parse_qsl(
                    urlsplit(authorization_response).query,
                    keep_blank_values=True,
                    strict_parsing=True,
                )
            },
            {"code", "iss", "state"},
        )
        self.assertNotIn(name, authorization_response)
        self.assertNotIn(value, authorization_response)
        self.assertNotIn(value, repr(validate_id_token.call_args))
        self.assertEqual(harness.transport.token_request_count, 1)
        self.assertEqual(harness.transport.jwks_request_count, 1)

    def test_valid_strict_component_matrix(self):
        value_forms = (
            (
                "literal_ascii",
                "phase2-value",
                "phase2-value",
            ),
            (
                "percent_ascii",
                "phase2%2Dvalue",
                "phase2-value",
            ),
            (
                "percent_utf8",
                "phase2-%E2%98%83",
                "phase2-\u2603",
            ),
            (
                "plus_space",
                "phase2+value",
                "phase2 value",
            ),
        )
        for field in self.FIELDS:
            for form_name, raw_value, expected_value in value_forms:
                with self.subTest(field=field, form=form_name):
                    raw_name = (
                        self._percent_ascii(field)
                        if form_name == "percent_ascii"
                        else field
                    )
                    self.assertEqual(
                        gateway_module._strict_callback_query(
                            f"{raw_name}={raw_value}"
                        ),
                        ((field, expected_value),),
                    )

    def test_response_issuer_rejection_categories_are_bounded(self):
        with self.assertRaises(gateway_module._ResponseIssuerMissing):
            gateway_module._validated_callback_parameters(
                "state=opaque&code=synthetic"
            )
        with self.assertRaises(gateway_module._ResponseIssuerMismatch):
            gateway_module._validated_callback_parameters(
                "state=opaque&code=synthetic&iss=accounts.google.com"
            )
        with self.assertRaises(gateway_module._ResponseIssuerMismatch):
            gateway_module._validated_callback_parameters(
                "state=opaque&code=synthetic&"
                "iss=accounts.google.com&scope=openid"
            )

    def test_duplicate_error_is_rejected_before_lookup_or_claim(self):
        def duplicate_error_fields(prepared):
            return (
                ("state", authorization_parameters(prepared)["state"]),
                ("error", "access_denied"),
                ("iss", "https://accounts.google.com"),
                ("error", "temporarily_unavailable"),
            )

        self._assert_four_field_callback_rejected_before_lookup(
            duplicate_error_fields
        )

    def test_authuser_callback_field_is_accepted_and_discarded(self):
        self._assert_extension_field_accepted_and_discarded(
            "authuser",
            "synthetic-authuser-extension",
        )

    def test_hd_callback_field_is_accepted_and_discarded(self):
        self._assert_extension_field_accepted_and_discarded(
            "hd",
            "synthetic-hd-extension.invalid",
        )

    def test_prompt_callback_field_is_accepted_and_discarded(self):
        self._assert_extension_field_accepted_and_discarded(
            "prompt",
            "synthetic-prompt-extension",
        )

    def test_scope_callback_field_is_accepted_and_discarded(self):
        self._assert_extension_field_accepted_and_discarded(
            "scope",
            "synthetic-scope-extension",
        )

    def test_generic_future_extension_is_accepted_and_discarded(self):
        self._assert_extension_field_accepted_and_discarded(
            "future_provider_extension",
            "synthetic-future-extension",
        )

    def test_invalid_name_and_value_matrix_stops_all_authority(self):
        for field in self.FIELDS:
            for case_name, suffix in self.INVALID_COMPONENT_SUFFIXES:
                with self.subTest(
                    field=field,
                    component="name",
                    case=case_name,
                ):
                    self._assert_invalid_callback(
                        lambda prepared, field=field, suffix=suffix: (
                            self._fields_for_component(
                                prepared,
                                field,
                                raw_name=field + suffix,
                                raw_value="phase2-valid-value",
                            )
                        )
                    )
                with self.subTest(
                    field=field,
                    component="value",
                    case=case_name,
                ):
                    self._assert_invalid_callback(
                        lambda prepared, field=field, suffix=suffix: (
                            self._fields_for_component(
                                prepared,
                                field,
                                raw_name=field,
                                raw_value="phase2-invalid" + suffix,
                            )
                        )
                    )

    def test_unique_extension_name_forms_are_decoded_and_discarded(self):
        extension_names = (
            ("literal_ascii", "phase2_unknown"),
            ("percent_ascii", "%70hase2_unknown"),
            ("percent_utf8", "phase2_%E2%98%83"),
        )
        for case_name, raw_name in extension_names:
            with self.subTest(case=case_name):
                authoritative_pairs, values, has_error = (
                    gateway_module._validated_callback_parameters(
                        "code=phase2-extension-code&state=opaque&"
                        "iss=https%3A%2F%2Faccounts.google.com&"
                        f"{raw_name}=phase2-valid-value"
                    )
                )
                self.assertFalse(has_error)
                self.assertEqual(
                    authoritative_pairs,
                    (
                        ("code", "phase2-extension-code"),
                        ("state", "opaque"),
                        ("iss", "https://accounts.google.com"),
                    ),
                )
                self.assertEqual(set(values), {"code", "iss", "state"})

    def test_callback_field_limit_accepts_nine_and_rejects_ten(self):
        accepted = (
            "state=opaque&error=access_denied&"
            "iss=https%3A%2F%2Faccounts.google.com&"
            "error_description=declined&error_uri=https%3A%2F%2Fexample.invalid&"
            "authuser=0&hd=example.invalid&prompt=none&scope=openid"
        )
        pairs, values, has_error = (
            gateway_module._validated_callback_parameters(accepted)
        )
        self.assertEqual(gateway_module._CALLBACK_PARAMETER_LIMIT, 9)
        self.assertTrue(has_error)
        self.assertEqual(
            set(values),
            {"error", "error_description", "error_uri", "iss", "state"},
        )
        self.assertEqual(len(pairs), 5)
        with self.assertRaises(gateway_module._CallbackQueryInvalid):
            gateway_module._validated_callback_parameters(
                accepted + "&future_extension=bounded-overflow"
            )

    def test_only_canonical_strict_callback_reaches_authlib(self):
        from authlib.integrations.requests_client import OAuth2Session

        cases = (
            ("code_ascii", "code", "phase2-code-ascii"),
            ("code_utf8", "code", "phase2-%E2%98%83"),
            ("state_percent_ascii", "state", None),
            ("issuer_percent_ascii", "iss", None),
            ("query_order", "order", None),
        )
        original_fetch = OAuth2Session.fetch_token
        for case_name, field, raw_value in cases:
            with self.subTest(case=case_name):
                harness = self.keep_harness(make_real_gateway())
                prepared = harness.gateway.prepare_authorization()
                state = authorization_parameters(prepared)["state"]
                if field == "code":
                    decoded_code = gateway_module._strict_callback_component(
                        raw_value
                    )
                    harness.transport.callback_for(
                        prepared,
                        code=decoded_code,
                    )
                    fields = (
                        ("code", raw_value),
                        ("state", state),
                        ("iss", "https://accounts.google.com"),
                    )
                elif field == "state":
                    code = f"phase2-canonical-{case_name}"
                    harness.transport.callback_for(prepared, code=code)
                    encoded_state = (
                        f"%{ord(state[0]):02X}" + state[1:]
                    )
                    fields = (
                        ("code", code),
                        ("state", encoded_state),
                        ("iss", "https://accounts.google.com"),
                    )
                elif field == "iss":
                    code = f"phase2-canonical-{case_name}"
                    harness.transport.callback_for(prepared, code=code)
                    fields = (
                        ("code", code),
                        ("state", state),
                        (
                            "iss",
                            self._percent_ascii(
                                "https://accounts.google.com"
                            ),
                        ),
                    )
                else:
                    code = f"phase2-canonical-{case_name}"
                    harness.transport.callback_for(prepared, code=code)
                    fields = (
                        ("iss", "https://accounts.google.com"),
                        ("state", state),
                        ("code", code),
                    )
                callback = self._raw_callback(fields)
                expected_canonical = (
                    gateway_module._validated_callback_url(
                        callback,
                        REDIRECT_URI,
                    )
                )
                with mock.patch.object(
                    OAuth2Session,
                    "fetch_token",
                    autospec=True,
                    side_effect=original_fetch,
                ) as fetch_token:
                    result = self.complete_real_failure(
                        harness,
                        prepared,
                        callback,
                    )
                self.assertEqual(result.status, "unavailable")
                fetch_token.assert_called_once()
                self.assertEqual(
                    fetch_token.call_args.kwargs[
                        "authorization_response"
                    ],
                    expected_canonical,
                )
                self.assertEqual(harness.transport.token_request_count, 1)
                self.assertEqual(harness.transport.jwks_request_count, 1)

    def test_callback_shape_preservation_matrix(self):
        def state(prepared):
            return authorization_parameters(prepared)["state"]

        cases = (
            (
                "duplicate_code",
                lambda prepared: (
                    ("code", "first"),
                    ("code", "second"),
                    ("state", state(prepared)),
                    ("iss", "https://accounts.google.com"),
                ),
            ),
            (
                "duplicate_state",
                lambda prepared: (
                    ("code", "phase2-shape-code"),
                    ("state", state(prepared)),
                    ("state", "second-state"),
                    ("iss", "https://accounts.google.com"),
                ),
            ),
            (
                "empty_code",
                lambda prepared: (
                    ("code", ""),
                    ("state", state(prepared)),
                    ("iss", "https://accounts.google.com"),
                ),
            ),
            (
                "empty_state",
                lambda _prepared: (
                    ("code", "phase2-shape-code"),
                    ("state", ""),
                    ("iss", "https://accounts.google.com"),
                ),
            ),
            (
                "parameter_cardinality",
                lambda prepared: (
                    ("code", "phase2-shape-code"),
                    ("state", state(prepared)),
                    ("iss", "https://accounts.google.com"),
                    ("authuser", "0"),
                    ("hd", "example.invalid"),
                    ("prompt", "none"),
                    ("scope", "openid"),
                    ("error", "access_denied"),
                    ("error_description", "denied"),
                    ("error_uri", "https://example.invalid/error"),
                ),
            ),
            (
                "callback_size",
                lambda prepared: self._raw_callback(
                    (
                        ("code", "x" * 8_200),
                        ("state", state(prepared)),
                        ("iss", "https://accounts.google.com"),
                    )
                ),
            ),
            (
                "field_size",
                lambda prepared: (
                    ("code", "x" * 4_097),
                    ("state", state(prepared)),
                    ("iss", "https://accounts.google.com"),
                ),
            ),
            (
                "wrong_scheme",
                lambda prepared: (
                    "http://accounts-d.test.invalid/callback"
                    f"?code=phase2-shape-code&state={state(prepared)}"
                    "&iss=https%3A%2F%2Faccounts.google.com"
                ),
            ),
            (
                "wrong_origin",
                lambda prepared: (
                    "https://other.test.invalid/callback"
                    f"?code=phase2-shape-code&state={state(prepared)}"
                    "&iss=https%3A%2F%2Faccounts.google.com"
                ),
            ),
            (
                "wrong_path",
                lambda prepared: (
                    "https://accounts-d.test.invalid/other"
                    f"?code=phase2-shape-code&state={state(prepared)}"
                    "&iss=https%3A%2F%2Faccounts.google.com"
                ),
            ),
            (
                "wrong_port",
                lambda prepared: (
                    "https://accounts-d.test.invalid:444/callback"
                    f"?code=phase2-shape-code&state={state(prepared)}"
                    "&iss=https%3A%2F%2Faccounts.google.com"
                ),
            ),
            (
                "fragment",
                lambda prepared: (
                    REDIRECT_URI
                    + f"?code=phase2-shape-code&state={state(prepared)}"
                    + "&iss=https%3A%2F%2Faccounts.google.com"
                    + "#fragment"
                ),
            ),
            (
                "code_and_error",
                lambda prepared: (
                    ("code", "phase2-shape-code"),
                    ("error", "access_denied"),
                    ("state", state(prepared)),
                    ("iss", "https://accounts.google.com"),
                ),
            ),
            (
                "code_and_error_description",
                lambda prepared: (
                    ("code", "phase2-shape-code"),
                    ("error_description", "denied"),
                    ("state", state(prepared)),
                    ("iss", "https://accounts.google.com"),
                ),
            ),
            (
                "code_and_error_uri",
                lambda prepared: (
                    ("code", "phase2-shape-code"),
                    ("error_uri", "https://example.invalid/error"),
                    ("state", state(prepared)),
                    ("iss", "https://accounts.google.com"),
                ),
            ),
        )
        for case_name, callback in cases:
            with self.subTest(case=case_name):
                self._assert_invalid_callback(callback)

    def test_replay_after_strict_callback_has_no_second_exchange(self):
        harness = self.keep_harness(make_real_gateway())
        prepared = harness.gateway.prepare_authorization()
        callback = harness.transport.callback_for(
            prepared,
            code="phase2-strict-replay-code",
        )
        first = self.complete_real_failure(
            harness,
            prepared,
            callback,
        )
        self.assertEqual(first.status, "unavailable")
        self.assertEqual(harness.transport.token_request_count, 1)
        replay = self.complete_real_failure(
            harness,
            prepared,
            callback,
        )
        self.assertEqual(
            replay.status,
            "invalid_or_expired_transaction",
        )
        self.assertEqual(harness.transport.token_request_count, 1)
        self.assertEqual(prepared.transaction.status, "consumed")


class ResponseContentEncodingMatrixTests(_SocketsBlockedTestCase):
    TOKEN_LIMIT = 64 * 1024
    JWKS_LIMIT = 256 * 1024

    @staticmethod
    def _gzip(body):
        return gzip.compress(body, compresslevel=9, mtime=0)

    @staticmethod
    def _deflate(body):
        return zlib.compress(body, level=9)

    @classmethod
    def _cases(cls, body, limit):
        at_limit = body + (b" " * (limit - len(body)))
        above_limit = at_limit + b" "
        return (
            ("absent_identity", None, body, True, None, 0),
            ("explicit_identity", "identity", body, True, None, 0),
            ("gzip", "gzip", cls._gzip(body), True, None, 0),
            ("deflate", "deflate", cls._deflate(body), True, None, 0),
            ("unknown_unencoded", "compress", body, False, None, 0),
            (
                "unknown_encoded",
                "br",
                cls._gzip(body),
                False,
                None,
                0,
            ),
            ("malformed_token", "gzip;q=1", body, False, None, 0),
            (
                "multiple_tokens",
                "gzip, deflate",
                cls._gzip(body),
                False,
                None,
                0,
            ),
            (
                "duplicate_declaration",
                "gzip, gzip",
                cls._gzip(body),
                False,
                None,
                0,
            ),
            (
                "conflicting_declaration",
                "identity, gzip",
                cls._gzip(body),
                False,
                None,
                0,
            ),
            (
                "mixed_case_gzip",
                "GZip",
                cls._gzip(body),
                True,
                None,
                0,
            ),
            (
                "ows_deflate",
                " \tDeFlAtE\t ",
                cls._deflate(body),
                True,
                None,
                0,
            ),
            ("empty_value", "", body, False, None, 0),
            (
                "gzip_at_decoded_limit",
                "gzip",
                cls._gzip(at_limit),
                True,
                None,
                0,
            ),
            (
                "gzip_above_decoded_limit",
                "gzip",
                cls._gzip(above_limit),
                False,
                None,
                0,
            ),
            (
                "malformed_gzip",
                "gzip",
                b"not-a-valid-gzip-stream",
                False,
                None,
                0,
            ),
            (
                "early_read_failure",
                None,
                body,
                False,
                RequestsConnectionError("phase2-early-read-failure"),
                0,
            ),
            (
                "partial_read_failure",
                None,
                body,
                False,
                RequestsConnectionError("phase2-partial-read-failure"),
                min(8, len(body)),
            ),
            (
                "identity_above_decoded_limit",
                "identity",
                above_limit,
                False,
                None,
                0,
            ),
            (
                "x_gzip_unsupported",
                "x-gzip",
                cls._gzip(body),
                False,
                None,
                0,
            ),
        )

    def test_token_response_content_encoding_matrix(self):
        body = b'{"error":"invalid_grant"}'
        original_validate = gateway_module._validate_token_response
        original_claims = gateway_module._validated_code_id_token
        original_issue = (
            gateway_module.TrustedExternalIdentityAuthentication._issue
        )
        original_delegate = gateway_module.complete_trusted_login
        for (
            name,
            content_encoding,
            encoded_body,
            accepted,
            read_exception,
            read_failure_after,
        ) in self._cases(body, self.TOKEN_LIMIT):
            with self.subTest(name=name):
                harness = self.keep_harness(make_real_gateway())
                prepared = harness.gateway.prepare_authorization()
                callback = harness.transport.callback_for(
                    prepared,
                    code=f"phase2-token-encoding-{name}",
                )
                headers = {}
                if content_encoding is not None:
                    headers["Content-Encoding"] = content_encoding
                harness.transport.queue_token_response(
                    body=encoded_body,
                    status=400,
                    headers=headers,
                    read_exception=read_exception,
                    read_failure_after=read_failure_after,
                )
                with (
                    mock.patch.object(
                        gateway_module,
                        "_validate_token_response",
                        side_effect=original_validate,
                    ) as validate_response,
                    mock.patch.object(
                        gateway_module,
                        "_validated_code_id_token",
                        side_effect=original_claims,
                    ) as validate_claims,
                    mock.patch.object(
                        gateway_module.TrustedExternalIdentityAuthentication,
                        "_issue",
                        side_effect=original_issue,
                    ) as issue_proof,
                    mock.patch.object(
                        gateway_module,
                        "complete_trusted_login",
                        side_effect=original_delegate,
                    ) as delegate,
                ):
                    result = self.complete_real_failure(
                        harness,
                        prepared,
                        callback,
                    )
                self.assertEqual(
                    result.status,
                    "authentication_denied"
                    if accepted
                    else "provider_unavailable",
                )
                self.assertEqual(
                    validate_response.call_count,
                    int(accepted),
                )
                validate_claims.assert_not_called()
                issue_proof.assert_not_called()
                delegate.assert_not_called()
                self.assertEqual(harness.transport.token_request_count, 1)
                self.assertEqual(harness.transport.jwks_request_count, 0)
                self.assertEqual(
                    prepared.transaction.status,
                    "consumed",
                )
                self.assertEqual(
                    harness.transport.pending_authorization_count,
                    0,
                )
                lifecycles = harness.transport.response_lifecycles
                self.assertEqual(len(lifecycles), 1)
                lifecycle = lifecycles[0]
                self.assertEqual(lifecycle.role, "token")
                self.assertGreaterEqual(lifecycle.close_count, 1)
                if accepted:
                    self.assertTrue(lifecycle.read_completed)
                if not accepted and name in {
                    "unknown_unencoded",
                    "unknown_encoded",
                    "malformed_token",
                    "multiple_tokens",
                    "duplicate_declaration",
                    "conflicting_declaration",
                    "empty_value",
                    "x_gzip_unsupported",
                }:
                    self.assertFalse(lifecycle.read_started)

    def test_jwks_response_content_encoding_matrix(self):
        body = json.dumps(
            jwks_document(PRIMARY_SIGNING_FIXTURE),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        from joserfc import jwt as real_jwt
        from joserfc.jwk import KeySet

        original_import = KeySet.import_key_set
        original_decode = real_jwt.decode
        original_claims = gateway_module._validated_code_id_token
        original_issue = (
            gateway_module.TrustedExternalIdentityAuthentication._issue
        )
        original_delegate = gateway_module.complete_trusted_login
        for (
            name,
            content_encoding,
            encoded_body,
            accepted,
            read_exception,
            read_failure_after,
        ) in self._cases(body, self.JWKS_LIMIT):
            with self.subTest(name=name):
                harness = self.keep_harness(make_real_gateway())
                prepared = harness.gateway.prepare_authorization()
                callback = harness.transport.callback_for(
                    prepared,
                    code=f"phase2-jwks-encoding-{name}",
                )
                headers = {}
                if content_encoding is not None:
                    headers["Content-Encoding"] = content_encoding
                harness.transport.queue_jwks_response(
                    body=encoded_body,
                    headers=headers,
                    read_exception=read_exception,
                    read_failure_after=read_failure_after,
                )
                with (
                    mock.patch.object(
                        KeySet,
                        "import_key_set",
                        side_effect=original_import,
                    ) as import_key_set,
                    mock.patch.object(
                        real_jwt,
                        "decode",
                        side_effect=original_decode,
                    ) as decode,
                    mock.patch.object(
                        gateway_module,
                        "_validated_code_id_token",
                        side_effect=original_claims,
                    ) as validate_claims,
                    mock.patch.object(
                        gateway_module.TrustedExternalIdentityAuthentication,
                        "_issue",
                        side_effect=original_issue,
                    ) as issue_proof,
                    mock.patch.object(
                        gateway_module,
                        "complete_trusted_login",
                        side_effect=original_delegate,
                    ) as delegate,
                ):
                    result = self.complete_real_failure(
                        harness,
                        prepared,
                        callback,
                    )
                self.assertEqual(
                    result.status,
                    "unavailable" if accepted else "provider_unavailable",
                )
                self.assertEqual(
                    import_key_set.call_count,
                    int(accepted),
                )
                self.assertEqual(decode.call_count, int(accepted))
                self.assertEqual(
                    validate_claims.call_count,
                    int(accepted),
                )
                issue_proof.assert_not_called()
                delegate.assert_not_called()
                self.assertEqual(harness.transport.token_request_count, 1)
                self.assertEqual(harness.transport.jwks_request_count, 1)
                self.assertEqual(
                    prepared.transaction.status,
                    "consumed",
                )
                lifecycles = harness.transport.response_lifecycles
                self.assertEqual(
                    [item.role for item in lifecycles],
                    ["token", "jwks"],
                )
                self.assertTrue(
                    all(item.close_count >= 1 for item in lifecycles)
                )
                if accepted:
                    self.assertTrue(lifecycles[-1].read_completed)
                else:
                    gateway_record = object.__getattribute__(
                        harness.gateway,
                        "_record",
                    )
                    self.assertIsNone(gateway_record.cache._key_set)
                if not accepted and name in {
                    "unknown_unencoded",
                    "unknown_encoded",
                    "malformed_token",
                    "multiple_tokens",
                    "duplicate_declaration",
                    "conflicting_declaration",
                    "empty_value",
                    "x_gzip_unsupported",
                }:
                    self.assertFalse(lifecycles[-1].read_started)

    def test_rejected_response_state_is_not_retained(self):
        marker = "phase2-rejected-response-marker"
        cases = (
            (
                "token",
                b'{"error":"' + marker.encode("ascii") + b'"}',
            ),
            (
                "jwks",
                b'{"keys":[],"marker":"'
                + marker.encode("ascii")
                + b'"}',
            ),
        )
        for role, body in cases:
            with self.subTest(role=role):
                harness = self.keep_harness(make_real_gateway())
                prepared = harness.gateway.prepare_authorization()
                callback = harness.transport.callback_for(
                    prepared,
                    code=f"phase2-retention-{role}",
                )
                plan = {
                    "body": body,
                    "headers": {"Content-Encoding": "unsupported"},
                }
                if role == "token":
                    harness.transport.queue_token_response(**plan)
                else:
                    harness.transport.queue_jwks_response(**plan)
                result = self.complete_real_failure(
                    harness,
                    prepared,
                    callback,
                )
                self.assertEqual(result.status, "provider_unavailable")
                reached = _ordinary_reachable_objects(
                    (harness.gateway, result)
                )
                self.assertFalse(
                    any(
                        type(value).__name__
                        in {
                            "Response",
                            "_TrackedResponse",
                            "_TrackedRawResponse",
                            "HTTPResponse",
                        }
                        for value in reached
                    )
                )
                self.assertFalse(
                    any(
                        isinstance(value, (str, bytes, bytearray))
                        and marker
                        in (
                            value
                            if isinstance(value, str)
                            else bytes(value).decode("utf-8", "ignore")
                        )
                        for value in reached
                    )
                )
                self.assertTrue(
                    all(
                        item.close_count >= 1
                        for item in harness.transport.response_lifecycles
                    )
                )


class JwksCacheTests(_SocketsBlockedTestCase):
    def valid_provider_attempt(self, harness, code):
        prepared = harness.gateway.prepare_authorization()
        callback = harness.transport.callback_for(prepared, code=code)
        result = self.complete_real_failure(harness, prepared, callback)
        self.assertEqual(result.status, "unavailable")
        return result

    def test_fallback_ttl_is_five_minutes_and_stale_keyset_is_not_used(self):
        harness = self.keep_harness(make_real_gateway())
        self.valid_provider_attempt(harness, "fallback-first")
        self.assertEqual(harness.transport.jwks_request_count, 1)
        harness.clock.advance_monotonic(299)
        self.valid_provider_attempt(harness, "fallback-before-boundary")
        self.assertEqual(harness.transport.jwks_request_count, 1)
        harness.clock.advance_monotonic(1)
        self.valid_provider_attempt(harness, "fallback-at-boundary")
        self.assertEqual(harness.transport.jwks_request_count, 2)

    def test_server_ttl_is_capped_at_six_hours(self):
        harness = self.keep_harness(make_real_gateway())
        harness.transport.queue_jwks_response(
            document=jwks_document(PRIMARY_SIGNING_FIXTURE),
            headers={"Cache-Control": "public, max-age=999999"},
        )
        self.valid_provider_attempt(harness, "cap-first")
        self.assertEqual(harness.transport.jwks_request_count, 1)
        harness.clock.advance_monotonic(21_599)
        self.valid_provider_attempt(harness, "cap-before-boundary")
        self.assertEqual(harness.transport.jwks_request_count, 1)
        harness.clock.advance_monotonic(1)
        self.valid_provider_attempt(harness, "cap-at-boundary")
        self.assertEqual(harness.transport.jwks_request_count, 2)

    def test_duplicate_malformed_and_aged_cache_directives_are_conservative(self):
        duplicate = self.keep_harness(make_real_gateway())
        duplicate.transport.queue_jwks_response(
            document=jwks_document(PRIMARY_SIGNING_FIXTURE),
            headers={"Cache-Control": "max-age=21600, max-age=1"},
        )
        self.valid_provider_attempt(duplicate, "duplicate-first")
        duplicate.clock.advance_monotonic(1)
        self.valid_provider_attempt(duplicate, "duplicate-boundary")
        self.assertEqual(duplicate.transport.jwks_request_count, 2)

        malformed = self.keep_harness(make_real_gateway())
        malformed.transport.queue_jwks_response(
            document=jwks_document(PRIMARY_SIGNING_FIXTURE),
            headers={
                "Cache-Control": "max-age=21600, max-age=invalid",
            },
        )
        self.valid_provider_attempt(malformed, "malformed-first")
        malformed.clock.advance_monotonic(300)
        self.valid_provider_attempt(malformed, "malformed-fallback")
        self.assertEqual(malformed.transport.jwks_request_count, 2)

        aged = self.keep_harness(make_real_gateway())
        aged.transport.queue_jwks_response(
            document=jwks_document(PRIMARY_SIGNING_FIXTURE),
            headers={"Cache-Control": "max-age=600", "Age": "599"},
        )
        self.valid_provider_attempt(aged, "aged-first")
        aged.clock.advance_monotonic(1)
        self.valid_provider_attempt(aged, "aged-boundary")
        self.assertEqual(aged.transport.jwks_request_count, 2)

        stale = self.keep_harness(make_real_gateway())
        stale.transport.queue_jwks_response(
            document=jwks_document(PRIMARY_SIGNING_FIXTURE),
            headers={"Cache-Control": "max-age=600", "Age": "600"},
        )
        prepared = stale.gateway.prepare_authorization()
        callback = stale.transport.callback_for(
            prepared,
            code="already-stale",
        )
        result = self.complete_real_failure(stale, prepared, callback)
        self.assertEqual(result.status, "provider_unavailable")

    def test_more_than_thirty_two_keys_fails_closed(self):
        harness = self.keep_harness(make_real_gateway())
        document = {
            "keys": [
                PRIMARY_SIGNING_FIXTURE.public_jwk()
                for _index in range(33)
            ]
        }
        harness.transport.queue_jwks_response(document=document)
        prepared = harness.gateway.prepare_authorization()
        callback = harness.transport.callback_for(prepared)
        result = self.complete_real_failure(harness, prepared, callback)
        self.assertEqual(result.status, "provider_unavailable")
        self.assertEqual(harness.transport.jwks_request_count, 1)

    def test_unknown_key_forced_refresh_is_rate_limited_for_sixty_seconds(self):
        harness = self.keep_harness(make_real_gateway())
        for code in ("unknown-first", "unknown-second"):
            prepared = harness.gateway.prepare_authorization()
            callback = harness.transport.callback_for(
                prepared,
                code=code,
                signing_fixture=ROTATED_SIGNING_FIXTURE,
            )
            result = self.complete_real_failure(harness, prepared, callback)
            self.assertEqual(result.status, "authentication_denied")
        self.assertEqual(harness.transport.jwks_request_count, 2)
        harness.clock.advance_monotonic(60)
        prepared = harness.gateway.prepare_authorization()
        callback = harness.transport.callback_for(
            prepared,
            code="unknown-after-limit",
            signing_fixture=ROTATED_SIGNING_FIXTURE,
        )
        result = self.complete_real_failure(harness, prepared, callback)
        self.assertEqual(result.status, "authentication_denied")
        self.assertEqual(harness.transport.jwks_request_count, 3)

    def test_caches_are_gateway_instance_local(self):
        first = self.keep_harness(make_real_gateway())
        second = self.keep_harness(make_real_gateway())
        self.valid_provider_attempt(first, "first-instance")
        self.valid_provider_attempt(second, "second-instance")
        self.assertEqual(first.transport.jwks_request_count, 1)
        self.assertEqual(second.transport.jwks_request_count, 1)

    def test_wrong_origin_or_redirected_jwks_response_fails_closed(self):
        plans = (
            {
                "document": jwks_document(PRIMARY_SIGNING_FIXTURE),
                "url": "https://attacker.invalid/certs",
            },
            {
                "document": jwks_document(PRIMARY_SIGNING_FIXTURE),
                "status": 302,
                "location": "https://attacker.invalid/certs",
            },
            {
                "document": jwks_document(PRIMARY_SIGNING_FIXTURE),
                "history": True,
            },
        )
        for plan in plans:
            with self.subTest(plan=tuple(sorted(plan))):
                harness = self.keep_harness(make_real_gateway())
                harness.transport.queue_jwks_response(**plan)
                prepared = harness.gateway.prepare_authorization()
                callback = harness.transport.callback_for(prepared)
                result = self.complete_real_failure(
                    harness,
                    prepared,
                    callback,
                )
                self.assertEqual(result.status, "provider_unavailable")


class ProviderClientAuthorityTests(_SocketsBlockedTestCase):
    def test_public_gateway_surface_has_no_provider_assertion_injection(self):
        public_methods = {
            name
            for name, value in inspect.getmembers(
                GoogleOidcGateway,
                predicate=callable,
            )
            if not name.startswith("_")
        }
        self.assertEqual(
            public_methods,
            {
                "close",
                "complete_authorization",
                "prepare_authorization",
            },
        )
        parameters = set(
            inspect.signature(
                GoogleOidcGateway.complete_authorization
            ).parameters
        )
        self.assertEqual(
            parameters,
            {
                "self",
                "connection",
                "transaction",
                "callback_url",
                "completion_policy",
                "request_secret_vault",
            },
        )
        for forbidden in (
            "provider_client",
            "claims",
            "verified_identity",
            "key_set",
            "decoder",
            "transport",
        ):
            self.assertNotIn(forbidden, parameters)
        self.assertNotIn("_VerifiedGoogleProjection", gateway_module.__all__)
        self.assertNotIn("_issue_test_gateway", gateway_module.__all__)

    def test_arbitrary_provider_transport_and_lookalikes_have_no_authority(self):
        class ArbitraryProvider:
            def verify(self, _callback_url):
                return {"sub": DEFAULT_SUBJECT, "verified": True}

        class ArbitraryTransport:
            def configure(self, _session, _role):
                return None

        forbidden_symbols = {
            "_TEST_AUTHORITY_CAPABILITY",
            "_CONFIGURATION_ISSUANCE_CAPABILITY",
            "_GATEWAY_ISSUANCE_CAPABILITY",
            "_PROJECTION_ISSUANCE_CAPABILITY",
            "_TestProviderAdapter",
            "_TestTransportConfigurer",
            "_issue_test_configuration",
            "_issue_test_gateway",
            "_issue_test_projection",
            "_attest_test_provider_adapter",
            "_attest_test_transport_configurer",
        }
        self.assertTrue(
            forbidden_symbols.isdisjoint(vars(gateway_module))
        )
        underscore_names = {
            name
            for name in vars(gateway_module)
            if name.startswith("_")
        }
        self.assertFalse(
            {
                name
                for name in underscore_names
                if re.search(r"(?:^|_)test(?:_|$)", name.casefold())
            }
        )
        self.assertFalse(
            any(
                value.__class__.__module__
                == "tests.google_oidc_gateway_test_support"
                for value in vars(gateway_module).values()
            )
        )
        for injected in (
            {"provider_adapter": ArbitraryProvider()},
            {"provider": ArbitraryProvider()},
            {"transport": ArbitraryTransport()},
            {"projection": object()},
            {"claims": {"sub": DEFAULT_SUBJECT}},
            {"clock": lambda: NOW},
        ):
            with self.subTest(injected=next(iter(injected))):
                secret = bytearray(CLIENT_SECRET)
                with self.assertRaises(TypeError):
                    GoogleOidcGateway(
                        client_id=CLIENT_ID,
                        client_secret=secret,
                        redirect_uri=REDIRECT_URI,
                        environment_namespace="test",
                        **injected,
                    )

        forbidden_ingress_names = (
            "configuration",
            "provider_adapter",
            "provider",
            "projection",
            "verified_projection",
            "claims",
            "verified_claims",
            "transport",
            "decoder",
            "key_set",
            "clock",
        )
        imported_underscore_values = tuple(
            (name, getattr(gateway_module, name))
            for name in sorted(vars(gateway_module))
            if name.startswith("_")
        )
        self.assertEqual(
            {name for name, _value in imported_underscore_values},
            underscore_names,
        )
        for name, imported_value in imported_underscore_values:
            with self.subTest(name=name, ingress="configuration_constructor"):
                with self.assertRaises(TypeError):
                    TrustedGoogleOidcConfiguration(imported_value)
            for ingress_name in forbidden_ingress_names:
                with self.subTest(name=name, ingress=ingress_name):
                    secret = bytearray(CLIENT_SECRET)
                    try:
                        with self.assertRaises(TypeError):
                            GoogleOidcGateway(
                                client_id=CLIENT_ID,
                                client_secret=secret,
                                redirect_uri=REDIRECT_URI,
                                environment_namespace="test",
                                **{ingress_name: imported_value},
                            )
                        self.assertEqual(secret, bytearray(CLIENT_SECRET))
                    finally:
                        secret.clear()

    def test_copied_reconstructed_or_altered_transport_cannot_assert_success(
        self,
    ):
        harness = self.keep_harness(make_real_gateway())
        for operation in (
            lambda: copy.copy(harness.transport),
            lambda: copy.deepcopy(harness.transport),
            lambda: pickle.loads(pickle.dumps(harness.transport)),
        ):
            with self.subTest(operation=operation.__code__.co_firstlineno):
                with self.assertRaises(TypeError):
                    operation()

        reconstructed = type(harness.transport)(
            clock=harness.clock,
            client_secret=CLIENT_SECRET,
        )
        try:
            secret = bytearray(CLIENT_SECRET)
            try:
                with self.assertRaises(TypeError):
                    GoogleOidcGateway(
                        client_id=CLIENT_ID,
                        client_secret=secret,
                        redirect_uri=REDIRECT_URI,
                        environment_namespace="test",
                        transport=reconstructed,
                    )
                self.assertEqual(secret, bytearray(CLIENT_SECRET))
            finally:
                secret.clear()
        finally:
            reconstructed.close()

        prepared = harness.gateway.prepare_authorization()
        callback = harness.transport.callback_for(prepared)
        harness.transport.queue_token_response(
            document={
                "provider_success": True,
                "verified": True,
                "verified_claims": {"sub": DEFAULT_SUBJECT},
            },
        )
        result = self.complete_real_failure(harness, prepared, callback)
        self.assertEqual(result.status, "provider_unavailable")
        self.assertEqual(harness.transport.token_request_count, 1)
        self.assertEqual(harness.transport.jwks_request_count, 0)

    def test_support_success_uses_authlib_and_joserfc_without_production_hooks(self):
        with gateway_database() as database:
            harness = self.keep_harness(
                make_fake_gateway(subject=database.subject)
            )
            prepared = harness.gateway.prepare_authorization()
            from authlib.integrations.requests_client import OAuth2Session
            from authlib.oidc.core import CodeIDToken
            from joserfc import jwt as real_jwt
            from joserfc.jwk import KeySet

            original_fetch_token = OAuth2Session.fetch_token
            original_import_key_set = KeySet.import_key_set
            original_decode = real_jwt.decode
            original_validate = CodeIDToken.validate
            with (
                mock.patch.object(
                    OAuth2Session,
                    "fetch_token",
                    autospec=True,
                    side_effect=original_fetch_token,
                ) as fetch_token,
                mock.patch.object(
                    KeySet,
                    "import_key_set",
                    side_effect=original_import_key_set,
                ) as import_key_set,
                mock.patch.object(
                    real_jwt,
                    "decode",
                    side_effect=original_decode,
                ) as decode,
                mock.patch.object(
                    CodeIDToken,
                    "validate",
                    autospec=True,
                    side_effect=original_validate,
                ) as validate,
            ):
                result = self.complete_fake(
                    harness,
                    database,
                    prepared=prepared,
                )
            self.assertEqual(result.status, "issued")
            self.assertEqual(fetch_token.call_count, 1)
            self.assertEqual(import_key_set.call_count, 1)
            self.assertEqual(decode.call_count, 1)
            self.assertEqual(validate.call_count, 1)

        source = GATEWAY_PATH.read_text(encoding="utf-8")
        self.assertNotIn("tests.google_oidc_gateway_test_support", source)
        self.assertNotIn("_route_http_adapter_send", source)

    def test_callback_extensions_cannot_assert_verification_authority(self):
        from authlib.integrations.requests_client import OAuth2Session

        original_fetch = OAuth2Session.fetch_token
        original_validate = gateway_module._validated_code_id_token
        for name in (
            "verified",
            "verified_identity",
            "verified_claims",
            "key_set",
            "provider_client",
        ):
            with self.subTest(name=name):
                harness = self.keep_harness(make_real_gateway())
                prepared = harness.gateway.prepare_authorization()
                callback = harness.transport.callback_for(
                    prepared,
                    code="extension-assertion-code",
                    extra_pairs=((name, "untrusted-extension-value"),),
                )
                with (
                    mock.patch.object(
                        OAuth2Session,
                        "fetch_token",
                        autospec=True,
                        side_effect=original_fetch,
                    ) as fetch_token,
                    mock.patch.object(
                        gateway_module,
                        "_validated_code_id_token",
                        side_effect=original_validate,
                    ) as validate_id_token,
                ):
                    result = self.complete_real_failure(
                        harness,
                        prepared,
                        callback,
                    )
                self.assertEqual(result.status, "unavailable")
                fetch_token.assert_called_once()
                validate_id_token.assert_called_once()
                authorization_response = fetch_token.call_args.kwargs[
                    "authorization_response"
                ]
                self.assertEqual(
                    {
                        field
                        for field, _item in parse_qsl(
                            urlsplit(authorization_response).query,
                            keep_blank_values=True,
                            strict_parsing=True,
                        )
                    },
                    {"code", "iss", "state"},
                )
                self.assertNotIn(name, authorization_response)
                self.assertNotIn(
                    "untrusted-extension-value",
                    repr(validate_id_token.call_args),
                )
                self.assertEqual(harness.transport.token_request_count, 1)
                self.assertEqual(harness.transport.jwks_request_count, 1)

    def test_each_gateway_owns_distinct_configuration_and_provider_state(self):
        first = self.keep_harness(make_fake_gateway())
        second = self.keep_harness(make_fake_gateway())
        for forbidden_registry in (
            "_CONFIGURATIONS",
            "_CREDENTIALS",
            "_GATEWAYS",
            "_PREPARATIONS",
            "_PROJECTIONS",
            "_TEST_PROVIDER_ADAPTERS",
            "_TEST_TRANSPORT_CONFIGURERS",
        ):
            self.assertFalse(
                hasattr(gateway_module, forbidden_registry),
                forbidden_registry,
            )
        self.assertIsNot(first.gateway, second.gateway)
        self.assertIsNot(first.configuration, second.configuration)
        self.assertIsNot(first.fake_provider, second.fake_provider)
        first_record = object.__getattribute__(first.gateway, "_record")
        second_record = object.__getattribute__(second.gateway, "_record")
        self.assertIsNot(first_record.transactions, second_record.transactions)
        self.assertIsNot(first_record.cache, second_record.cache)
        first_transaction = first.gateway.prepare_authorization().transaction
        rejected = second.gateway.complete_authorization(
            None,
            first_transaction,
            REDIRECT_URI,
            None,
            None,
        )
        self.assertEqual(
            rejected.status,
            "invalid_or_expired_transaction",
        )
        self.assertEqual(first_transaction.status, "fresh")

    def test_closed_gateway_cannot_be_reactivated(self):
        harness = make_fake_gateway()
        prepared = harness.gateway.prepare_authorization()
        harness.close()
        self.assertEqual(prepared.transaction.status, "consumed")
        after_close = harness.gateway.prepare_authorization()
        self.assertIs(type(after_close), GoogleOidcGatewayFailure)
        self.assertEqual(after_close.status, "unavailable")


class DurableIdentityResolutionTests(_SocketsBlockedTestCase):
    def issue_for_database(self, database, **gateway_kwargs):
        harness = self.keep_harness(
            make_fake_gateway(
                subject=database.subject,
                **gateway_kwargs,
            )
        )
        result = self.complete_fake(harness, database)
        return harness, result

    def test_exact_case_sensitive_subject_succeeds(self):
        with gateway_database(suffix="exact-subject") as database:
            _harness, result = self.issue_for_database(database)
            self.assertEqual(result.status, "issued")

        with gateway_database(suffix="case-sensitive") as database:
            harness = self.keep_harness(
                make_fake_gateway(subject=database.subject.swapcase())
            )
            result = self.complete_fake(harness, database)
            self.assertEqual(result.status, "authentication_denied")

    def test_email_is_never_an_identity_key_or_authority(self):
        with gateway_database(suffix="email-not-authority") as database:
            database.connection.execute(
                "UPDATE auth_identities SET verified_email = ? "
                "WHERE auth_identity_id = ?",
                ("different-address@example.test", database.identity_id),
            )
            database.connection.commit()
            _harness, result = self.issue_for_database(database)
            self.assertEqual(result.status, "issued")

        with gateway_database(suffix="email-cannot-resolve") as database:
            email = database.connection.execute(
                "SELECT verified_email FROM auth_identities "
                "WHERE auth_identity_id = ?",
                (database.identity_id,),
            ).fetchone()[0]
            harness = self.keep_harness(make_fake_gateway(subject=email))
            result = self.complete_fake(harness, database)
            self.assertEqual(result.status, "authentication_denied")

    def test_missing_and_disabled_identity_are_generic_denials(self):
        for state in ("missing", "disabled"):
            with self.subTest(state=state):
                with gateway_database(suffix=f"identity-{state}") as database:
                    if state == "missing":
                        database.connection.execute(
                            "DELETE FROM auth_identities "
                            "WHERE auth_identity_id = ?",
                            (database.identity_id,),
                        )
                    else:
                        database.connection.execute(
                            "UPDATE auth_identities SET disabled_at = ? "
                            "WHERE auth_identity_id = ?",
                            (NOW.isoformat(), database.identity_id),
                        )
                    database.connection.commit()
                    _harness, result = self.issue_for_database(database)
                    self.assertEqual(result.status, "authentication_denied")

    def test_inactive_account_is_generic_denial(self):
        with gateway_database(suffix="inactive-account") as database:
            database.connection.execute(
                "UPDATE users SET lifecycle_status = 'suspended', "
                "updated_at = ? WHERE user_id = ?",
                (NOW.isoformat(), database.account_id),
            )
            database.connection.commit()
            _harness, result = self.issue_for_database(database)
            self.assertEqual(result.status, "authentication_denied")

    def test_duplicate_identity_state_is_unavailable(self):
        with gateway_database(suffix="duplicate-identity") as database:
            connection = database.connection
            original_sql = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'auth_identities'"
            ).fetchone()[0]
            weakened_sql = original_sql.replace(
                "CREATE TABLE auth_identities",
                "CREATE TABLE auth_identities_duplicate",
                1,
            )
            weakened_sql = weakened_sql.replace(
                "  UNIQUE (provider, provider_subject),\n",
                "",
            ).replace(
                "  UNIQUE (user_id, provider),\n",
                "",
            )
            connection.commit()
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(weakened_sql)
            columns = tuple(
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(auth_identities)"
                )
            )
            rows = [
                list(row)
                for row in connection.execute(
                    "SELECT * FROM auth_identities"
                ).fetchall()
            ]
            duplicate = list(rows[0])
            duplicate[columns.index("auth_identity_id")] = (
                "duplicate-auth-identity-000000000000000001"
            )
            duplicate[columns.index("link_idempotency_key")] = (
                "duplicate-link-idempotency-key"
            )
            rows.append(duplicate)
            placeholders = ", ".join("?" for _column in columns)
            connection.executemany(
                "INSERT INTO auth_identities_duplicate "
                f"({', '.join(columns)}) VALUES ({placeholders})",
                rows,
            )
            connection.execute("DROP TABLE auth_identities")
            connection.execute(
                "ALTER TABLE auth_identities_duplicate "
                "RENAME TO auth_identities"
            )
            connection.commit()
            connection.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM auth_identities "
                    "WHERE provider = 'google' AND provider_subject = ?",
                    (database.subject,),
                ).fetchone()[0],
                2,
            )
            _harness, result = self.issue_for_database(database)
            self.assertEqual(result.status, "unavailable")

    def test_malformed_account_and_identity_are_unavailable(self):
        cases = (
            (
                "account",
                "UPDATE users SET row_version = 0 WHERE user_id = ?",
                lambda database: database.account_id,
            ),
            (
                "identity",
                "UPDATE auth_identities SET email_verified = 2 "
                "WHERE auth_identity_id = ?",
                lambda database: database.identity_id,
            ),
        )
        for name, statement, identifier in cases:
            with self.subTest(name=name):
                with gateway_database(suffix=f"malformed-{name}") as database:
                    database.connection.execute(
                        "PRAGMA ignore_check_constraints = ON"
                    )
                    database.connection.execute(
                        statement,
                        (identifier(database),),
                    )
                    database.connection.commit()
                    database.connection.execute(
                        "PRAGMA ignore_check_constraints = OFF"
                    )
                    _harness, result = self.issue_for_database(database)
                    self.assertEqual(result.status, "unavailable")

    def test_mismatched_account_relationship_is_unavailable(self):
        with gateway_database(suffix="relationship") as database:
            second = seed_existing_google_identity(
                database.connection,
                suffix="relationship-other",
            )
            database.connection.execute(
                "DELETE FROM auth_identities WHERE auth_identity_id = ?",
                (second.identity.auth_identity_id,),
            )
            database.connection.execute(
                "DROP TRIGGER trg_auth_identities_immutable_identity"
            )
            database.connection.execute(
                "UPDATE auth_identities SET user_id = ? "
                "WHERE auth_identity_id = ?",
                (second.user.user_id, database.identity_id),
            )
            database.connection.commit()
            _harness, result = self.issue_for_database(database)
            self.assertEqual(result.status, "unavailable")

    def test_chronology_contradictions_are_unavailable(self):
        with gateway_database(suffix="provider-chronology") as database:
            identity_created = database.connection.execute(
                "SELECT created_at FROM auth_identities "
                "WHERE auth_identity_id = ?",
                (database.identity_id,),
            ).fetchone()[0]
            authenticated_before_identity = (
                datetime.fromisoformat(identity_created)
                - timedelta(seconds=1)
            )
            _harness, result = self.issue_for_database(
                database,
                authenticated_at=authenticated_before_identity,
            )
            self.assertEqual(result.status, "unavailable")

        with gateway_database(suffix="durable-chronology") as database:
            database.connection.execute(
                "UPDATE auth_identities SET last_authenticated_at = ? "
                "WHERE auth_identity_id = ?",
                (
                    (NOW + timedelta(seconds=1)).isoformat(),
                    database.identity_id,
                ),
            )
            database.connection.commit()
            _harness, result = self.issue_for_database(database)
            self.assertEqual(result.status, "unavailable")

    def test_success_mutates_no_account_identity_profile_or_ownership_state(self):
        with gateway_database(suffix="read-only-resolution") as database:
            before_authority = _snapshot_authority_rows(database.connection)
            before_counts = durable_counts(database.connection)
            _harness, result = self.issue_for_database(database)
            self.assertEqual(result.status, "issued")
            after_counts = durable_counts(database.connection)
            self.assertEqual(
                _snapshot_authority_rows(database.connection),
                before_authority,
            )
            for table in (
                "users",
                "auth_identities",
                "persistent_profiles",
                "profile_ownership_bindings",
            ):
                if table in before_counts:
                    self.assertEqual(after_counts[table], before_counts[table])
            self.assertEqual(
                after_counts["account_sessions"],
                before_counts["account_sessions"] + 1,
            )
        source = GATEWAY_PATH.read_text(encoding="utf-8")
        self.assertIn("ORDER BY auth_identity_id LIMIT 2", source)


class TrustedLoginCompositionTests(_SocketsBlockedTestCase):
    def capture_delegation(
        self,
        database,
        *,
        authenticated_at=None,
        token_expires_at=None,
    ):
        harness = self.keep_harness(
            make_fake_gateway(
                subject=database.subject,
                authenticated_at=authenticated_at,
                token_expires_at=token_expires_at,
            )
        )
        prepared = harness.gateway.prepare_authorization()
        callback = harness.transport.callback_for(prepared)
        policy = completion_policy()
        vault = object()
        sentinel = object()
        with mock.patch.object(
            gateway_module,
            "complete_trusted_login",
            return_value=sentinel,
        ) as delegate:
            result = harness.gateway.complete_authorization(
                database.connection,
                prepared.transaction,
                callback,
                policy,
                vault,
            )
        self.assertIs(result, sentinel)
        delegate.assert_called_once()
        return prepared, policy, vault, delegate.call_args

    def test_exact_proof_request_and_completion_boundary_binding(self):
        with gateway_database(suffix="proof-binding") as database:
            prepared, policy, vault, call = self.capture_delegation(database)
            connection, proof, received_policy, received_vault = call.args
            self.assertIs(connection, database.connection)
            self.assertIs(received_policy, policy)
            self.assertIs(received_vault, vault)
            self.assertEqual(proof._provider, "google")
            self.assertEqual(proof._account_id, database.account_id)
            self.assertEqual(proof._identity_id, database.identity_id)
            self.assertEqual(proof._authenticated_at, NOW)
            self.assertEqual(proof._expires_at, NOW + timedelta(minutes=5))
            self.assertEqual(
                proof._assurance_policy_version,
                "google_oidc_v1",
            )
            self.assertEqual(proof._environment_namespace, "test")
            self.assertEqual(call.kwargs["trusted_now"], NOW)
            self.assertRegex(
                call.kwargs["idempotency_key"],
                r"^google-oidc-[A-Za-z0-9_-]{32,}$",
            )
            self.assertEqual(prepared.transaction.status, "consumed")

    def test_b2d1_control_flow_propagates_exactly_after_gateway_cleanup(self):
        for name, exception_type in (
            ("keyboard", KeyboardInterrupt),
            ("system", SystemExit),
            ("generator", GeneratorExit),
        ):
            with self.subTest(name=name):
                with gateway_database(suffix=f"b2d1-control-{name}") as database:
                    harness = self.keep_harness(
                        make_fake_gateway(subject=database.subject)
                    )
                    prepared = harness.gateway.prepare_authorization()
                    callback = harness.transport.callback_for(prepared)
                    control = exception_type(f"b2d1-{name}-control")
                    completion_frame = None
                    with mock.patch.object(
                        gateway_module,
                        "complete_trusted_login",
                        side_effect=control,
                    ):
                        try:
                            harness.gateway.complete_authorization(
                                database.connection,
                                prepared.transaction,
                                callback,
                                completion_policy(),
                                object(),
                            )
                        except exception_type as caught:
                            self.assertIs(caught, control)
                            traceback = caught.__traceback__
                            while traceback is not None:
                                if (
                                    traceback.tb_frame.f_code.co_name
                                    == "complete_authorization"
                                    and Path(
                                        traceback.tb_frame.f_code.co_filename
                                    ).resolve()
                                    == GATEWAY_PATH.resolve()
                                ):
                                    completion_frame = dict(
                                        traceback.tb_frame.f_locals
                                    )
                                traceback = traceback.tb_next
                        else:
                            self.fail("B2D1 control flow did not propagate")
                    self.assertIsNotNone(completion_frame)
                    for local_name in (
                        "callback_url",
                        "connection",
                        "projection",
                        "verified_identity",
                        "resolved",
                        "proof",
                        "request_key",
                        "transaction_record",
                    ):
                        self.assertIsNone(
                            completion_frame.get(local_name),
                            local_name,
                        )
                    self.assertEqual(prepared.transaction.status, "consumed")
                    with self.assertRaises(TypeError):
                        _ = prepared.authorization_url
                    self.assertIsNone(control.__cause__)
                    self.assertIsNone(control.__context__)
                    control.__traceback__ = None

    def test_proof_expiry_is_earliest_of_all_three_bounds(self):
        with gateway_database(suffix="token-expiry-bound") as database:
            _prepared, _policy, _vault, call = self.capture_delegation(
                database,
                token_expires_at=NOW + timedelta(minutes=4),
            )
            self.assertEqual(
                call.args[1]._expires_at,
                NOW + timedelta(minutes=4),
            )

        with gateway_database(suffix="transaction-expiry-bound") as database:
            prepared, _policy, _vault, call = self.capture_delegation(
                database,
                token_expires_at=NOW + timedelta(hours=1),
            )
            self.assertEqual(
                call.args[1]._expires_at,
                NOW + timedelta(minutes=10),
            )

        authenticated_at = NOW - timedelta(hours=23, minutes=59)
        with gateway_database(
            suffix="authentication-age-bound",
            created_at=authenticated_at - timedelta(minutes=5),
        ) as database:
            _prepared, _policy, _vault, call = self.capture_delegation(
                database,
                authenticated_at=authenticated_at,
                token_expires_at=NOW + timedelta(hours=1),
            )
            self.assertEqual(
                call.args[1]._expires_at,
                authenticated_at + timedelta(seconds=86_400),
            )

    def test_b2d1_request_key_uses_an_independent_random_draw(self):
        draws = (
            "S" * 43,
            "N" * 43,
            "V" * 86,
            "K" * 43,
        )
        with gateway_database(suffix="independent-request-key") as database:
            harness = self.keep_harness(
                make_fake_gateway(subject=database.subject)
            )
            with mock.patch.object(
                gateway_module.secrets,
                "token_urlsafe",
                side_effect=draws,
            ) as random_source:
                prepared = harness.gateway.prepare_authorization()
                parameters = authorization_parameters(prepared)
                callback = harness.transport.callback_for(prepared)
            sentinel = object()
            with mock.patch.object(
                gateway_module,
                "complete_trusted_login",
                return_value=sentinel,
            ) as delegate:
                result = harness.gateway.complete_authorization(
                    database.connection,
                    prepared.transaction,
                    callback,
                    completion_policy(),
                    object(),
                )
            self.assertIs(result, sentinel)
            call = delegate.call_args
        self.assertEqual(parameters["state"], draws[0])
        self.assertEqual(parameters["nonce"], draws[1])
        self.assertNotIn(draws[2], parameters.values())
        self.assertEqual(
            call.kwargs["idempotency_key"],
            "google-oidc-" + draws[3],
        )
        self.assertNotIn(call.kwargs["idempotency_key"], parameters.values())
        self.assertEqual(
            random_source.call_args_list,
            [
                mock.call(32),
                mock.call(32),
                mock.call(64),
                mock.call(32),
            ],
        )

    def test_assurance_and_environment_mismatch_are_b2d1_denials(self):
        cases = (
            {
                "expected_assurance_policy_version": "different_assurance_v1",
            },
            {
                "environment_namespace": "private_beta",
            },
        )
        for index, policy_kwargs in enumerate(cases):
            with self.subTest(policy=policy_kwargs):
                with gateway_database(
                    suffix=f"policy-coherence-{index}"
                ) as database:
                    harness = self.keep_harness(
                        make_fake_gateway(subject=database.subject)
                    )
                    result = self.complete_fake(
                        harness,
                        database,
                        policy=completion_policy(**policy_kwargs),
                    )
                    self.assertEqual(
                        result.status,
                        "authentication_denied",
                    )
                    self.assertNotIsInstance(
                        result,
                        GoogleOidcGatewayFailure,
                    )

    def test_success_creates_one_session_and_deposits_one_vault_entry(self):
        with gateway_database(suffix="issued-composition") as database:
            harness = self.keep_harness(
                make_fake_gateway(subject=database.subject)
            )
            vault = self.keep_vault()
            result = self.complete_fake(
                harness,
                database,
                vault=vault,
            )
            self.assertEqual(result.status, "issued")
            self.assertEqual(result.issued_session.status, "issued")
            self.assertEqual(vault_entry_count(vault), 1)
            sessions = database.connection.execute(
                "SELECT session_version, rotated_at, revoked_at "
                "FROM account_sessions"
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in sessions],
                [(1, None, None)],
            )

    def test_caller_owned_transaction_returns_pending_commit(self):
        with gateway_database(suffix="pending-composition") as database:
            harness = self.keep_harness(
                make_fake_gateway(subject=database.subject)
            )
            vault = self.keep_vault()
            database.connection.execute("BEGIN")
            result = self.complete_fake(
                harness,
                database,
                vault=vault,
            )
            self.assertEqual(result.status, "pending_commit")
            self.assertEqual(result.issued_session.status, "pending_commit")
            self.assertTrue(database.connection.in_transaction)
            self.assertEqual(vault_entry_count(vault), 1)
            database.connection.rollback()

    def test_exact_replay_is_credential_free_and_changed_replay_conflicts(self):
        with gateway_database(suffix="composition-replay") as database:
            with mock.patch.object(
                gateway_module.secrets,
                "token_urlsafe",
                return_value="A" * 43,
            ):
                first_harness = self.keep_harness(
                    make_fake_gateway(subject=database.subject)
                )
                first_vault = self.keep_vault()
                first = self.complete_fake(
                    first_harness,
                    database,
                    vault=first_vault,
                )
                self.assertEqual(first.status, "issued")

                replay_vault = self.keep_vault()
                replay = self.complete_fake(
                    first_harness,
                    database,
                    vault=replay_vault,
                )
                self.assertEqual(replay.status, "already_completed")
                self.assertEqual(vault_entry_count(replay_vault), 0)

                changed_harness = self.keep_harness(
                    make_fake_gateway(
                        subject=database.subject,
                        authenticated_at=NOW - timedelta(seconds=1),
                    )
                )
                changed_vault = self.keep_vault()
                changed = self.complete_fake(
                    changed_harness,
                    database,
                    vault=changed_vault,
                )
                self.assertEqual(changed.status, "idempotency_conflict")
                self.assertEqual(vault_entry_count(changed_vault), 0)
            self.assertEqual(
                database.connection.execute(
                    "SELECT COUNT(*) FROM account_sessions"
                ).fetchone()[0],
                1,
            )

    def test_b2d1_denial_and_unavailable_results_propagate_unchanged(self):
        with gateway_database(suffix="b2d1-denial") as database:
            harness = self.keep_harness(
                make_fake_gateway(subject=database.subject)
            )
            denied = self.complete_fake(
                harness,
                database,
                policy=completion_policy(
                    expected_assurance_policy_version="other_policy_v1"
                ),
            )
            self.assertEqual(denied.status, "authentication_denied")
            self.assertNotIsInstance(denied, GoogleOidcGatewayFailure)

        with gateway_database(suffix="b2d1-unavailable") as database:
            harness = self.keep_harness(
                make_fake_gateway(subject=database.subject)
            )
            prepared = harness.gateway.prepare_authorization()
            callback = harness.transport.callback_for(prepared)
            unavailable = harness.gateway.complete_authorization(
                database.connection,
                prepared.transaction,
                callback,
                completion_policy(),
                object(),
            )
            self.assertEqual(unavailable.status, "unavailable")
            self.assertNotIsInstance(unavailable, GoogleOidcGatewayFailure)

    def test_result_exposes_no_proof_subject_or_authority_mutation(self):
        with gateway_database(suffix="composition-privacy") as database:
            before = _snapshot_authority_rows(database.connection)
            before_counts = durable_counts(database.connection)
            harness = self.keep_harness(
                make_fake_gateway(subject=database.subject)
            )
            result = self.complete_fake(harness, database)
            self.assertEqual(result.status, "issued")
            reached = _ordinary_reachable_objects(result)
            for forbidden in (
                database.subject,
                database.identity_id,
                "TrustedExternalIdentityAuthentication",
            ):
                with self.subTest(forbidden=forbidden):
                    self.assertNotIn(forbidden, reached)
                    self.assertNotIn(forbidden, repr(result))
            self.assertIn(database.account_id, reached)
            self.assertNotIn(database.account_id, repr(result))
            self.assertEqual(
                _snapshot_authority_rows(database.connection),
                before,
            )
            after_counts = durable_counts(database.connection)
            for table in (
                "users",
                "auth_identities",
                "persistent_profiles",
                "profile_ownership_bindings",
            ):
                if table in before_counts:
                    self.assertEqual(after_counts[table], before_counts[table])


class PrivacyAndLifetimeTests(_SocketsBlockedTestCase):
    def test_repr_and_str_are_bounded_and_redacted(self):
        harness = self.keep_harness(make_fake_gateway())
        prepared = harness.gateway.prepare_authorization()
        values = (
            (
                harness.configuration,
                "TrustedGoogleOidcConfiguration(<redacted>)",
            ),
            (harness.gateway, "GoogleOidcGateway(<configured>)"),
            (
                prepared,
                "PreparedGoogleOidcAuthorization(<redacted>)",
            ),
            (
                prepared.transaction,
                "GoogleOidcAuthorizationTransaction(<redacted>)",
            ),
        )
        for value, expected in values:
            with self.subTest(value=type(value).__name__):
                self.assertEqual(repr(value), expected)
                self.assertEqual(str(value), expected)
                self.assertNotIn(CLIENT_SECRET.decode("ascii"), repr(value))
                self.assertNotIn(DEFAULT_SUBJECT, repr(value))

    def test_failure_equality_hashing_and_serialization_are_bounded(self):
        harness = self.keep_harness(
            make_fake_gateway(outcomes=("authentication_denied",))
        )

        def denied():
            prepared = harness.gateway.prepare_authorization()
            callback = harness.transport.callback_for(prepared)
            return harness.gateway.complete_authorization(
                None,
                prepared.transaction,
                callback,
                None,
                None,
            )

        first = denied()
        second = denied()
        unavailable_harness = self.keep_harness(
            make_fake_gateway(outcomes=("runtime_error",))
        )
        prepared = unavailable_harness.gateway.prepare_authorization()
        callback = unavailable_harness.transport.callback_for(prepared)
        unavailable = unavailable_harness.gateway.complete_authorization(
            None,
            prepared.transaction,
            callback,
            None,
            None,
        )
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))
        self.assertNotEqual(first, unavailable)
        self.assertEqual(first.as_dict(), {"status": "authentication_denied"})
        self.assertEqual(
            repr(first),
            "GoogleOidcGatewayFailure(status='authentication_denied')",
        )
        self.assertEqual(json.dumps(first.as_dict()), '{"status": "authentication_denied"}')
        assert_rejects_copy_pickle(first)
        with self.assertRaises(TypeError):
            json.dumps(first)

    def test_all_public_sealed_values_reject_copy_pickle_and_json(self):
        harness = self.keep_harness(make_fake_gateway())
        prepared = harness.gateway.prepare_authorization()
        failure_harness = self.keep_harness(
            make_fake_gateway(outcomes=("provider_unavailable",))
        )
        failure_prepared = failure_harness.gateway.prepare_authorization()
        failure_callback = failure_harness.transport.callback_for(
            failure_prepared
        )
        failure = failure_harness.gateway.complete_authorization(
            None,
            failure_prepared.transaction,
            failure_callback,
            None,
            None,
        )
        for value in (
            harness.configuration,
            harness.gateway,
            prepared,
            prepared.transaction,
            failure,
        ):
            with self.subTest(value=type(value).__name__):
                assert_rejects_copy_pickle(value)
                with self.assertRaises(TypeError):
                    json.dumps(value)

    def test_caught_dependency_exception_is_detached_from_public_failure(self):
        exception = ConnectTimeout("unique-sensitive-provider-timeout")
        harness = self.keep_harness(make_real_gateway())
        harness.transport.queue_token_response(exception=exception)
        prepared = harness.gateway.prepare_authorization()
        callback = harness.transport.callback_for(prepared)
        failure = self.complete_real_failure(harness, prepared, callback)
        self.assertEqual(failure.status, "provider_unavailable")
        self.assertIsNone(exception.__traceback__)
        self.assertIsNone(exception.__cause__)
        self.assertIsNone(exception.__context__)
        reached = _ordinary_reachable_objects(failure)
        self.assertNotIn(exception, reached)
        self.assertNotIn("unique-sensitive-provider-timeout", reached)
        self.assertNotIn("unique-sensitive-provider-timeout", repr(failure))

    def test_recursive_reachability_excludes_protocol_and_identity_material(self):
        with gateway_database() as database:
            harness = self.keep_harness(
                make_real_gateway()
            )
            prepared = harness.gateway.prepare_authorization()
            parameters = authorization_parameters(prepared)
            callback = harness.transport.callback_for(prepared)
            vault = self.keep_vault()
            result = harness.gateway.complete_authorization(
                database.connection,
                prepared.transaction,
                callback,
                completion_policy(),
                vault,
            )
            self.assertEqual(result.status, "issued")
            reached = _ordinary_reachable_objects(result)
            for forbidden in (
                parameters["state"],
                parameters["nonce"],
                parameters["code_challenge"],
                DEFAULT_SUBJECT,
                database.identity_id,
                "test-access-token-not-retained",
                "test-refresh-token-not-retained",
            ):
                with self.subTest(forbidden=forbidden[:24]):
                    self.assertNotIn(forbidden, reached)
            self.assertIn(database.account_id, reached)
            self.assertNotIn(database.account_id, repr(result))

    def test_long_lived_gateway_graph_releases_provider_intermediates(self):
        with gateway_database() as database:
            harness = self.keep_harness(make_real_gateway())
            prepared = harness.gateway.prepare_authorization()
            parameters = authorization_parameters(prepared)
            callback = harness.transport.callback_for(
                prepared,
                code="long-lived-provider-code",
            )
            vault = self.keep_vault()
            result = harness.gateway.complete_authorization(
                database.connection,
                prepared.transaction,
                callback,
                completion_policy(),
                vault,
            )
            self.assertEqual(result.status, "issued")
            reached = _ordinary_reachable_objects(
                (
                    harness.gateway,
                    harness.configuration,
                    prepared,
                    prepared.transaction,
                    result,
                )
            )
            for forbidden in (
                parameters["state"],
                parameters["nonce"],
                "long-lived-provider-code",
                "test-access-token-not-retained",
                "test-refresh-token-not-retained",
                DEFAULT_SUBJECT,
            ):
                with self.subTest(forbidden=forbidden[:24]):
                    self.assertFalse(
                        any(
                            type(value) is str and value == forbidden
                            for value in reached
                        )
                    )
            retained_type_names = {type(value).__name__ for value in reached}
            self.assertNotIn("OAuth2Session", retained_type_names)
            self.assertNotIn("CodeIDToken", retained_type_names)
            self.assertNotIn("Response", retained_type_names)

    def test_real_transport_cleanup_preserves_original_control_flow(self):
        original = KeyboardInterrupt("original-provider-control")
        replacement = SystemExit("cleanup-must-not-replace")
        request = PreparedRequest()
        request.prepare(
            method="GET",
            url="https://www.googleapis.com/oauth2/v3/certs",
        )

        class ExplodingResponse:
            url = request.url
            history = ()
            is_redirect = False
            headers = {}

            def iter_content(self, chunk_size):
                self.assert_chunk_size = chunk_size
                raise original

            def close(self):
                raise replacement

        response = ExplodingResponse()
        with self.assertRaises(KeyboardInterrupt) as caught:
            gateway_module._bounded_send(
                lambda _request, **_kwargs: response,
                request,
                {"allow_redirects": False, "verify": True},
                expected_url=request.url,
                expected_method="GET",
                maximum_bytes=256 * 1024,
                response_type=Response,
            )
        self.assertIs(caught.exception, original)
        self.assertIsNone(replacement.__traceback__)
        self.assertIsNone(replacement.__cause__)
        self.assertIsNone(replacement.__context__)

        session_cleanup = SystemExit("session-cleanup")
        session = types.SimpleNamespace(
            token={"id_token": "must-be-cleared"},
        )

        def fail_session_close():
            raise session_cleanup

        session.close = fail_session_close
        gateway_module._cleanup_preserving_exception(
            original,
            lambda: gateway_module._clear_oauth_session_token(session),
            session.close,
        )
        self.assertEqual(session.token, {})
        self.assertIsNone(session_cleanup.__traceback__)

        for name, response_url, headers, exception_type in (
            (
                "wrong_origin",
                "https://attacker.invalid/certs",
                {},
                SystemExit,
            ),
            (
                "oversized",
                request.url,
                {"Content-Length": "262145"},
                GeneratorExit,
            ),
        ):
            with self.subTest(cleanup_control=name):
                cleanup_control = exception_type(
                    f"{name}-cleanup-control"
                )

                class EarlyRejectedResponse:
                    url = response_url
                    history = ()
                    is_redirect = False
                    status_code = 200
                    reason = "OK"
                    encoding = None
                    elapsed = None

                    def __init__(self):
                        self.headers = dict(headers)
                        self.request = request

                    def close(self):
                        raise cleanup_control

                response = EarlyRejectedResponse()
                with self.assertRaises(exception_type) as caught:
                    gateway_module._bounded_send(
                        lambda _request, **_kwargs: response,
                        request,
                        {"allow_redirects": False, "verify": True},
                        expected_url=request.url,
                        expected_method="GET",
                        maximum_bytes=256 * 1024,
                        response_type=Response,
                    )
                self.assertIs(caught.exception, cleanup_control)
                self.assertIsNone(cleanup_control.__cause__)
                self.assertIsNone(cleanup_control.__context__)

    def test_mutable_secret_inputs_and_terminal_url_buffer_are_cleared(self):
        secret = bytearray(CLIENT_SECRET)
        harness = self.keep_harness(
            make_fake_gateway(
                client_secret=secret,
                outcomes=("authentication_denied",),
            )
        )
        self.assertEqual(secret, bytearray())
        prepared = harness.gateway.prepare_authorization()
        callback = harness.transport.callback_for(prepared)
        transaction = prepared.transaction
        transaction_record = (
            object.__getattribute__(harness.gateway, "_record")
            .transactions[transaction]
        )
        mutable_buffers = (
            transaction_record.state,
            transaction_record.nonce,
            transaction_record.pkce_verifier,
            transaction_record.b2d1_request_key,
            transaction_record.authorization_url_buffer,
        )
        self.assertTrue(all(buffer for buffer in mutable_buffers))
        self.assertTrue(prepared.authorization_url)
        failure = harness.gateway.complete_authorization(
            None,
            transaction,
            callback,
            None,
            None,
        )
        self.assertEqual(failure.status, "authentication_denied")
        self.assertEqual(transaction.status, "consumed")
        self.assertTrue(
            all(buffer == bytearray() for buffer in mutable_buffers)
        )
        with self.assertRaises(TypeError):
            _ = prepared.authorization_url

    def test_control_flow_exceptions_propagate_exactly_after_cleanup(self):
        cases = (
            ("keyboard_interrupt", KeyboardInterrupt),
            ("system_exit", SystemExit),
            ("generator_exit", GeneratorExit),
        )
        for outcome, exception_type in cases:
            with self.subTest(outcome=outcome):
                harness = self.keep_harness(
                    make_fake_gateway(outcomes=(outcome,))
                )
                prepared = harness.gateway.prepare_authorization()
                callback = harness.transport.callback_for(prepared)
                with self.assertRaises(exception_type) as caught:
                    harness.gateway.complete_authorization(
                        None,
                        prepared.transaction,
                        callback,
                        None,
                        None,
                    )
                self.assertIs(
                    caught.exception,
                    harness.fake_provider.control_flow_exception,
                )
                self.assertEqual(prepared.transaction.status, "consumed")
                with self.assertRaises(TypeError):
                    _ = prepared.authorization_url

    def test_claim_transition_control_flow_consumes_only_its_own_claim(self):
        original_claim = gateway_module._claim_transaction
        for name, exception_type in (
            ("keyboard", KeyboardInterrupt),
            ("system", SystemExit),
            ("generator", GeneratorExit),
        ):
            with self.subTest(name=name):
                harness = self.keep_harness(make_fake_gateway())
                prepared = harness.gateway.prepare_authorization()
                transaction_record = object.__getattribute__(
                    harness.gateway,
                    "_record",
                ).transactions[prepared.transaction]
                mutable_buffers = (
                    transaction_record.state,
                    transaction_record.nonce,
                    transaction_record.pkce_verifier,
                    transaction_record.b2d1_request_key,
                    transaction_record.authorization_url_buffer,
                )
                control = exception_type(f"claim-{name}-control")

                def claim_then_interrupt(*args, **kwargs):
                    claimed = original_claim(*args, **kwargs)
                    sensitive_state = bytes(claimed.state).decode("ascii")
                    if not sensitive_state:
                        raise AssertionError("claimed_state_not_reached")
                    raise control

                completion_frame = None
                with mock.patch.object(
                    gateway_module,
                    "_claim_transaction",
                    side_effect=claim_then_interrupt,
                ):
                    try:
                        harness.gateway.complete_authorization(
                            None,
                            prepared.transaction,
                            REDIRECT_URI,
                            None,
                            None,
                        )
                    except exception_type as caught:
                        self.assertIs(caught, control)
                        frame_names = []
                        traceback = caught.__traceback__
                        while traceback is not None:
                            frame_names.append(
                                traceback.tb_frame.f_code.co_name
                            )
                            if (
                                traceback.tb_frame.f_code.co_name
                                == "complete_authorization"
                                and Path(
                                    traceback.tb_frame.f_code.co_filename
                                ).resolve()
                                == GATEWAY_PATH.resolve()
                            ):
                                completion_frame = dict(
                                    traceback.tb_frame.f_locals
                                )
                            traceback = traceback.tb_next
                    else:
                        self.fail("claim control flow did not propagate")
                self.assertNotIn("claim_then_interrupt", frame_names)
                self.assertIsNotNone(completion_frame)
                for local_name in (
                    "callback_url",
                    "completion_policy",
                    "request_secret_vault",
                    "connection",
                    "transaction",
                    "gateway",
                    "transaction_record",
                    "claim_attempt",
                ):
                    self.assertIsNone(
                        completion_frame.get(local_name),
                        local_name,
                    )
                self.assertEqual(prepared.transaction.status, "consumed")
                self.assertTrue(
                    all(buffer == bytearray() for buffer in mutable_buffers)
                )
                self.assertEqual(harness.fake_provider.call_count, 0)
                with self.assertRaises(TypeError):
                    _ = prepared.authorization_url
                self.assertIsNone(control.__cause__)
                self.assertIsNone(control.__context__)
                control.__traceback__ = None

        owner = self.keep_harness(make_fake_gateway(block=True))
        prepared = owner.gateway.prepare_authorization()
        callback = owner.transport.callback_for(prepared)
        owner_results = []

        def complete_owner():
            owner_results.append(
                owner.gateway.complete_authorization(
                    None,
                    prepared.transaction,
                    callback,
                    None,
                    None,
                )
            )

        thread = threading.Thread(target=complete_owner)
        thread.start()
        self.assertTrue(owner.fake_provider.entered.wait(timeout=3))
        contender_control = KeyboardInterrupt("contender-control")
        try:
            with mock.patch.object(
                gateway_module,
                "_claim_transaction",
                side_effect=contender_control,
            ):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    owner.gateway.complete_authorization(
                        None,
                        prepared.transaction,
                        callback,
                        None,
                        None,
                    )
            self.assertIs(caught.exception, contender_control)
            self.assertEqual(prepared.transaction.status, "consumed")
            self.assertEqual(
                owner.gateway.prepare_authorization().status,
                "unavailable",
            )
        finally:
            owner.fake_provider.release.set()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(
            [item.status for item in owner_results],
            ["invalid_or_expired_transaction"],
        )
        self.assertEqual(prepared.transaction.status, "consumed")
        self.assertEqual(owner.fake_provider.call_count, 1)

    def test_durable_resolution_control_flow_detaches_sensitive_frames(self):
        for name, exception_type in (
            ("keyboard", KeyboardInterrupt),
            ("system", SystemExit),
            ("generator", GeneratorExit),
        ):
            with self.subTest(name=name):
                with gateway_database(
                    suffix=f"durable-control-{name}"
                ) as database:
                    harness = self.keep_harness(
                        make_fake_gateway(subject=database.subject)
                    )
                    prepared = harness.gateway.prepare_authorization()
                    callback = harness.transport.callback_for(prepared)
                    control = exception_type(f"durable-{name}-control")

                    def interrupted_resolver(
                        connection,
                        identity,
                        _now,
                    ):
                        sensitive_subject = identity.provider_subject
                        sensitive_rows = connection.execute(
                            "SELECT * FROM auth_identities"
                        ).fetchall()
                        if not sensitive_subject or not sensitive_rows:
                            raise AssertionError(
                                "sensitive_fixture_not_reached"
                            )
                        raise control

                    completion_frame = None
                    with mock.patch.object(
                        gateway_module,
                        "_resolve_durable_identity",
                        side_effect=interrupted_resolver,
                    ):
                        try:
                            harness.gateway.complete_authorization(
                                database.connection,
                                prepared.transaction,
                                callback,
                                completion_policy(),
                                object(),
                            )
                        except exception_type as caught:
                            self.assertIs(caught, control)
                            frame_names = []
                            traceback = caught.__traceback__
                            while traceback is not None:
                                frame_names.append(
                                    traceback.tb_frame.f_code.co_name
                                )
                                if (
                                    traceback.tb_frame.f_code.co_name
                                    == "complete_authorization"
                                    and Path(
                                        traceback.tb_frame.f_code.co_filename
                                    ).resolve()
                                    == GATEWAY_PATH.resolve()
                                ):
                                    completion_frame = dict(
                                        traceback.tb_frame.f_locals
                                    )
                                traceback = traceback.tb_next
                        else:
                            self.fail(
                                "durable control flow did not propagate"
                            )
                    self.assertNotIn("interrupted_resolver", frame_names)
                    self.assertIsNotNone(completion_frame)
                    for local_name in (
                        "callback_url",
                        "connection",
                        "projection",
                        "verified_identity",
                        "resolved",
                        "proof",
                        "request_key",
                        "transaction_record",
                    ):
                        self.assertIsNone(
                            completion_frame.get(local_name),
                            local_name,
                        )
                    self.assertEqual(prepared.transaction.status, "consumed")
                    with self.assertRaises(TypeError):
                        _ = prepared.authorization_url
                    self.assertIsNone(control.__cause__)
                    self.assertIsNone(control.__context__)
                    control.__traceback__ = None


class GatewayDeterminismTests(_SocketsBlockedTestCase):
    def gateway_failure(self, outcome):
        harness = self.keep_harness(make_fake_gateway(outcomes=(outcome,)))
        prepared = harness.gateway.prepare_authorization()
        callback = harness.transport.callback_for(prepared)
        return harness.gateway.complete_authorization(
            None,
            prepared.transaction,
            callback,
            None,
            None,
        )

    def test_each_pre_b2d1_category_has_one_exact_bounded_status(self):
        denied = self.gateway_failure("authentication_denied")
        provider = self.gateway_failure("provider_unavailable")
        unavailable = self.gateway_failure("runtime_error")
        invalid_harness = self.keep_harness(make_fake_gateway())
        invalid = invalid_harness.gateway.complete_authorization(
            None,
            object.__new__(GoogleOidcAuthorizationTransaction),
            REDIRECT_URI,
            None,
            None,
        )
        statuses = {
            denied.status,
            provider.status,
            unavailable.status,
            invalid.status,
        }
        self.assertEqual(
            statuses,
            {
                "authentication_denied",
                "provider_unavailable",
                "invalid_or_expired_transaction",
                "unavailable",
            },
        )
        self.assertTrue(
            all(
                type(item) is GoogleOidcGatewayFailure
                for item in (denied, provider, unavailable, invalid)
            )
        )

    def test_failure_hash_and_representation_are_repeatable(self):
        expected_hashes = {
            "authentication_denied": 0x32E7A11,
            "provider_unavailable": 0x4A1F9C2,
            "invalid_or_expired_transaction": 0x612B8D3,
            "unavailable": 0x7D903E4,
        }
        outcomes = {
            "authentication_denied": "authentication_denied",
            "provider_unavailable": "provider_unavailable",
            "unavailable": "runtime_error",
        }
        for status in expected_hashes:
            with self.subTest(status=status):
                if status == "invalid_or_expired_transaction":
                    harness = self.keep_harness(make_fake_gateway())

                    def invalid_failure():
                        return harness.gateway.complete_authorization(
                            None,
                            object.__new__(
                                GoogleOidcAuthorizationTransaction
                            ),
                            REDIRECT_URI,
                            None,
                            None,
                        )

                    first = invalid_failure()
                    second = invalid_failure()
                else:
                    first = self.gateway_failure(outcomes[status])
                    second = self.gateway_failure(outcomes[status])
                self.assertEqual(first, second)
                self.assertEqual(hash(first), expected_hashes[status])
                self.assertEqual(hash(second), expected_hashes[status])
                self.assertEqual(first.as_dict(), {"status": status})
                self.assertEqual(
                    repr(first),
                    f"GoogleOidcGatewayFailure(status={status!r})",
                )

    def test_typed_oauth_error_categories_map_without_message_matching(self):
        cases = (
            ("invalid_grant", "authentication_denied"),
            ("access_denied", "authentication_denied"),
            ("server_error", "provider_unavailable"),
            ("temporarily_unavailable", "provider_unavailable"),
        )
        for error, expected in cases:
            with self.subTest(error=error):
                harness = self.keep_harness(make_real_gateway())
                harness.transport.queue_token_response(
                    document={"error": error},
                    status=400,
                )
                prepared = harness.gateway.prepare_authorization()
                callback = harness.transport.callback_for(prepared)
                result = self.complete_real_failure(
                    harness,
                    prepared,
                    callback,
                )
                self.assertEqual(result.status, expected)

        tree = ast.parse(GATEWAY_PATH.read_text(encoding="utf-8"))
        message_matches = []
        for handler in (
            node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)
        ):
            if not handler.name:
                continue
            for node in ast.walk(handler):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "str"
                    and node.args
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == handler.name
                ):
                    message_matches.append(handler.lineno)
        self.assertEqual(message_matches, [])

    def test_manual_clocks_make_policy_shape_repeatable(self):
        first = self.keep_harness(make_real_gateway())
        second = self.keep_harness(make_real_gateway())
        first_prepared = first.gateway.prepare_authorization()
        second_prepared = second.gateway.prepare_authorization()
        self.assertEqual(
            first_prepared.transaction.created_at,
            second_prepared.transaction.created_at,
        )
        self.assertEqual(
            first_prepared.transaction.expires_at,
            second_prepared.transaction.expires_at,
        )
        volatile = {"state", "nonce", "code_challenge"}
        first_parameters = authorization_parameters(first_prepared)
        second_parameters = authorization_parameters(second_prepared)
        self.assertEqual(
            {
                key: value
                for key, value in first_parameters.items()
                if key not in volatile
            },
            {
                key: value
                for key, value in second_parameters.items()
                if key not in volatile
            },
        )


class ControlFlowTracebackReachabilityMatrixTests(_SocketsBlockedTestCase):
    _CONTROL_CASES = (
        ("keyboard_interrupt", KeyboardInterrupt),
        ("system_exit", SystemExit),
        ("generator_exit", GeneratorExit),
    )
    _STAGES = (
        "exchange",
        "claims_validation",
        "durable_lookup",
        "proof_issuance",
        "b2d1",
    )

    @contextlib.contextmanager
    def _interrupt_after_real_stage(self, stage, control):
        reached = []
        if stage == "exchange":
            original = gateway_module._exchange_code

            def interrupted(*args, **kwargs):
                stage_value = original(*args, **kwargs)
                reached.append(stage)
                raise control

            patcher = mock.patch.object(
                gateway_module,
                "_exchange_code",
                new=interrupted,
            )
        elif stage == "claims_validation":
            original = gateway_module._validated_code_id_token

            def interrupted(*args, **kwargs):
                stage_value = original(*args, **kwargs)
                reached.append(stage)
                raise control

            patcher = mock.patch.object(
                gateway_module,
                "_validated_code_id_token",
                new=interrupted,
            )
        elif stage == "durable_lookup":
            original = gateway_module._resolve_durable_identity

            def interrupted(*args, **kwargs):
                stage_value = original(*args, **kwargs)
                reached.append(stage)
                raise control

            patcher = mock.patch.object(
                gateway_module,
                "_resolve_durable_identity",
                new=interrupted,
            )
        elif stage == "proof_issuance":
            original = (
                gateway_module.TrustedExternalIdentityAuthentication._issue
            )

            def interrupted(_cls, *args, **kwargs):
                stage_value = original(*args, **kwargs)
                reached.append(stage)
                raise control

            patcher = mock.patch.object(
                gateway_module.TrustedExternalIdentityAuthentication,
                "_issue",
                new=classmethod(interrupted),
            )
        elif stage == "b2d1":
            original = gateway_module.complete_trusted_login

            def interrupted(*args, **kwargs):
                stage_value = original(*args, **kwargs)
                reached.append(stage)
                raise control

            patcher = mock.patch.object(
                gateway_module,
                "complete_trusted_login",
                new=interrupted,
            )
        else:
            raise AssertionError(f"unknown control-flow stage: {stage}")
        with patcher:
            yield reached

    def _gateway_traceback_frames(self, control):
        frames = []
        traceback = control.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            if Path(frame.f_code.co_filename).resolve() == GATEWAY_PATH.resolve():
                frames.append(frame)
            traceback = traceback.tb_next
        return tuple(frames)

    def _reachable_from_gateway_frame_locals(self, frames):
        pending = [
            (f"{frame.f_code.co_name}.{name}", value)
            for frame in frames
            for name, value in frame.f_locals.items()
        ]
        reached = []
        seen = set()
        while pending:
            path, value = pending.pop()
            marker = id(value)
            if marker in seen:
                continue
            seen.add(marker)
            reached.append((path, value))
            self.assertLess(
                len(reached),
                20_000,
                "gateway traceback reachability was unexpectedly broad",
            )
            if isinstance(
                value,
                (
                    str,
                    bytes,
                    bytearray,
                    int,
                    float,
                    bool,
                    type(None),
                    datetime,
                    timedelta,
                ),
            ):
                continue
            if isinstance(value, BaseException):
                pending.extend(
                    (f"{path}.args[{index}]", item)
                    for index, item in enumerate(value.args)
                )
                try:
                    namespace = vars(value)
                except TypeError:
                    namespace = None
                if namespace:
                    pending.extend(
                        (f"{path}.{name}", item)
                        for name, item in namespace.items()
                    )
                if value.__cause__ is not None:
                    pending.append((f"{path}.__cause__", value.__cause__))
                if value.__context__ is not None:
                    pending.append(
                        (f"{path}.__context__", value.__context__)
                    )
                continue
            if isinstance(value, dict):
                pending.extend(
                    (f"{path}[key]", item) for item in value.keys()
                )
                pending.extend(
                    (f"{path}[value]", item) for item in value.values()
                )
                continue
            if isinstance(value, (tuple, list, set, frozenset)):
                pending.extend(
                    (f"{path}[{index}]", item)
                    for index, item in enumerate(value)
                )
                continue
            if isinstance(value, weakref.ReferenceType):
                target = value()
                if target is not None:
                    pending.append((f"{path}()", target))
                continue
            if isinstance(value, types.MethodType):
                pending.append((f"{path}.__self__", value.__self__))
                value = value.__func__
            if isinstance(value, types.FunctionType):
                for index, cell in enumerate(value.__closure__ or ()):
                    try:
                        cell_value = cell.cell_contents
                    except ValueError:
                        continue
                    pending.append(
                        (f"{path}.__closure__[{index}]", cell_value)
                    )
                continue
            if isinstance(
                value,
                (
                    type,
                    types.ModuleType,
                    types.CodeType,
                    types.FrameType,
                    types.TracebackType,
                ),
            ):
                continue
            try:
                namespace = vars(value)
            except TypeError:
                namespace = None
            if namespace:
                pending.extend(
                    (f"{path}.{name}", item)
                    for name, item in namespace.items()
                )
            for cls in type(value).__mro__:
                slots = cls.__dict__.get("__slots__", ())
                if isinstance(slots, str):
                    slots = (slots,)
                for slot in slots:
                    if slot in {"__dict__", "__weakref__"}:
                        continue
                    try:
                        item = object.__getattribute__(value, slot)
                    except (AttributeError, TypeError):
                        continue
                    pending.append((f"{path}.{slot}", item))
        return tuple(reached)

    def _assert_no_live_authority_in_traceback(
        self,
        control,
        *,
        sensitive_markers,
    ):
        frames = self._gateway_traceback_frames(control)
        self.assertTrue(frames)
        public_frames = tuple(
            frame
            for frame in frames
            if frame.f_code.co_name == "complete_authorization"
        )
        self.assertEqual(len(public_frames), 1)
        public_locals = public_frames[0].f_locals
        self.assertIn("self", public_locals)
        self.assertIsNone(public_locals["self"])
        self.assertIn("gateway_object", public_locals)
        self.assertIsNone(public_locals["gateway_object"])
        for name in (
            "connection",
            "transaction",
            "callback_url",
            "completion_policy",
            "request_secret_vault",
            "result",
        ):
            self.assertIn(name, public_locals)
            self.assertIsNone(public_locals[name], name)

        reached = self._reachable_from_gateway_frame_locals(frames)
        forbidden_types = {
            "GoogleOidcGateway",
            "TrustedGoogleOidcConfiguration",
            "_GatewayRecord",
            "_ConfigurationRecord",
            "_GoogleClientCredential",
            "_CredentialRecord",
            "_RealGoogleOidcAdapter",
            "_GoogleOidcJwksCache",
            "_TransactionRecord",
            "_CommittedDelegationCapsule",
            "TrustedExternalIdentityAuthentication",
            "OAuth2Session",
            "BoundedOAuth2Session",
            "BoundedJwksSession",
            "Response",
            "KeySet",
        }
        for path, value in reached:
            with self.subTest(traceback_path=path):
                self.assertNotIn(type(value).__name__, forbidden_types)
                self.assertNotIsInstance(value, sqlite3.Connection)
                if isinstance(value, str):
                    text = value
                elif isinstance(value, (bytes, bytearray)):
                    text = bytes(value).decode("utf-8", "ignore")
                else:
                    continue
                for sensitive in sensitive_markers:
                    self.assertNotIn(sensitive, text)

    def _assert_gateway_is_poisoned(
        self,
        *,
        harness,
        gateway_record,
        configuration_record,
        credential_record,
        adapter,
        cache,
        transaction_record,
        transaction_buffers,
    ):
        self.assertTrue(gateway_record.closed)
        self.assertEqual(len(gateway_record.transactions), 0)
        for name in (
            "configuration",
            "configuration_record",
            "identity_verifier",
            "provider_adapter",
            "cache",
            "transaction_authority",
            "attestation",
        ):
            self.assertIsNone(getattr(gateway_record, name), name)
        self.assertTrue(configuration_record.closed)
        for name in (
            "credential",
            "authority",
            "client_configuration_identity",
            "attestation",
        ):
            self.assertIsNone(getattr(configuration_record, name), name)
        self.assertTrue(credential_record.closed)
        self.assertEqual(credential_record.secret_buffer, bytearray())
        self.assertEqual(credential_record.digest, b"")
        self.assertIsNone(credential_record.configuration_authority)
        self.assertIsNone(adapter._configuration)
        self.assertIsNone(adapter._cache)
        self.assertTrue(cache._closed)
        self.assertIsNone(cache._configuration)
        self.assertIsNone(cache._key_set)
        self.assertIsNone(cache._expires_at)
        self.assertIsNone(cache._flight)
        self.assertEqual(cache._generation, 0)
        self.assertEqual(transaction_record.lifecycle, "consumed")
        self.assertIsNone(transaction_record.claim_owner)
        self.assertIsNone(transaction_record.attestation)
        self.assertTrue(
            all(value == bytearray() for value in transaction_buffers)
        )
        unavailable = harness.gateway.prepare_authorization()
        self.assertEqual(unavailable.status, "unavailable")

    def test_control_flow_traceback_reachability_matrix(self):
        for stage in self._STAGES:
            for control_name, exception_type in self._CONTROL_CASES:
                with self.subTest(stage=stage, control=control_name):
                    with gateway_database(
                        suffix=f"control-matrix-{stage}-{control_name}"
                    ) as database:
                        secret_marker = (
                            f"control-matrix-secret-{stage}-{control_name}"
                        )
                        secret = bytearray(secret_marker.encode("ascii"))
                        harness = self.keep_harness(
                            make_real_gateway(
                                subject=database.subject,
                                client_secret=secret,
                            )
                        )
                        self.assertEqual(secret, bytearray())
                        prepared = harness.gateway.prepare_authorization()
                        parameters = authorization_parameters(prepared)
                        callback = harness.transport.callback_for(prepared)
                        callback_code = parse_qs(
                            urlsplit(callback).query,
                            keep_blank_values=True,
                        )["code"][0]
                        gateway_record = object.__getattribute__(
                            harness.gateway,
                            "_record",
                        )
                        configuration_record = (
                            gateway_record.configuration_record
                        )
                        credential = configuration_record.credential
                        credential_record = object.__getattribute__(
                            credential,
                            "_record",
                        )
                        adapter = gateway_record.provider_adapter
                        cache = gateway_record.cache
                        transaction_record = gateway_record.transactions[
                            prepared.transaction
                        ]
                        transaction_buffers = (
                            transaction_record.state,
                            transaction_record.nonce,
                            transaction_record.pkce_verifier,
                            transaction_record.b2d1_request_key,
                            transaction_record.authorization_url_buffer,
                        )
                        sensitive_markers = (
                            secret_marker,
                            parameters["state"],
                            parameters["nonce"],
                            bytes(
                                transaction_record.pkce_verifier
                            ).decode("ascii"),
                            bytes(
                                transaction_record.b2d1_request_key
                            ).decode("ascii"),
                            callback_code,
                            database.subject,
                            "test-access-token-not-retained",
                            "test-refresh-token-not-retained",
                        )
                        before_counts = durable_counts(database.connection)
                        vault = self.keep_vault()
                        control = exception_type(
                            f"{stage}-{control_name}-control"
                        )
                        with self._interrupt_after_real_stage(
                            stage,
                            control,
                        ) as reached:
                            try:
                                harness.gateway.complete_authorization(
                                    database.connection,
                                    prepared.transaction,
                                    callback,
                                    completion_policy(),
                                    vault,
                                )
                            except exception_type as caught:
                                self.assertIs(caught, control)
                            else:
                                self.fail(
                                    "control-flow exception did not propagate"
                                )
                        self.assertEqual(reached, [stage])
                        self.assertEqual(
                            harness.transport.token_request_count,
                            1,
                        )
                        self.assertEqual(
                            harness.transport.jwks_request_count,
                            0 if stage == "exchange" else 1,
                        )
                        self.assertEqual(
                            prepared.transaction.status,
                            "consumed",
                        )
                        with self.assertRaises(TypeError):
                            _ = prepared.authorization_url
                        self._assert_gateway_is_poisoned(
                            harness=harness,
                            gateway_record=gateway_record,
                            configuration_record=configuration_record,
                            credential_record=credential_record,
                            adapter=adapter,
                            cache=cache,
                            transaction_record=transaction_record,
                            transaction_buffers=transaction_buffers,
                        )
                        gc.collect()
                        self._assert_no_live_authority_in_traceback(
                            control,
                            sensitive_markers=sensitive_markers,
                        )
                        after_counts = durable_counts(database.connection)
                        expected_sessions = before_counts[
                            "account_sessions"
                        ] + (1 if stage == "b2d1" else 0)
                        self.assertEqual(
                            after_counts["account_sessions"],
                            expected_sessions,
                        )
                        self.assertEqual(
                            vault_entry_count(vault),
                            1 if stage == "b2d1" else 0,
                        )
                        replay = harness.gateway.complete_authorization(
                            database.connection,
                            prepared.transaction,
                            callback,
                            completion_policy(),
                            vault,
                        )
                        self.assertEqual(replay.status, "unavailable")
                        self.assertEqual(
                            durable_counts(database.connection)[
                                "account_sessions"
                            ],
                            expected_sessions,
                        )
                        self.assertEqual(
                            vault_entry_count(vault),
                            1 if stage == "b2d1" else 0,
                        )
                        self.assertIsNone(control.__cause__)
                        self.assertIsNone(control.__context__)
                        control.__traceback__ = None
