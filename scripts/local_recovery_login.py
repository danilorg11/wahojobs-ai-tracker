"""Loopback-only composition of the existing controlled provider and product.

Private launch configuration pins an existing identity; this module provisions
neither accounts nor profiles. It is not a production identity provider.
"""
from contextlib import contextmanager, closing, ExitStack
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import json
import sqlite3
import time
from datetime import datetime, timezone

from scripts.durable_google_login_fixture_demo import _ControlledProviderBridge
from tests.durable_google_login_browser_test_support import (
    TemporaryBrowserLoginState, loopback_and_in_memory_provider_only,
)
from tests.google_oidc_gateway_test_support import make_real_gateway
from wahojobs.durable_google_login_browser import DurableGoogleLoginBrowserIntegration
from wahojobs.durable_google_login_runtime import (
    _load_construction_configuration, _load_authority_material,
)
from wahojobs.public_job_canary import PublicJobCanaryRoutingGate
from wahojobs.workos_authkit_staging import _build_profile_integration, _StagingDatabaseConnections


RETURN_COOKIE = "__Host-wahojobs_local_return"
SAFE_RETURNS = frozenset({"/account/profile", "/find-matches"})


class RealtimeClock:
    def __call__(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return time.monotonic()


class _ResponseView:
    """Keep session delivery callbacks attached to the ORIGINAL response."""
    def __init__(self, response, *, body=None, headers=None):
        self._response = response
        self.status = response.status
        self.body = response.body if body is None else body
        self.headers = response.headers if headers is None else headers

    def __getattr__(self, name):
        return getattr(self._response, name)


class LocalLoginNavigation:
    def __init__(self, delegate):
        self.delegate = delegate

    def matches_route(self, path):
        return self.delegate.matches_route(path)

    def handle(self, method, target, headers, body_stream=None):
        parsed = urlsplit(target)
        login = method == "GET" and parsed.path == "/login"
        destination = parse_qs(parsed.query).get("next", ["/find-matches"])[0]
        if destination not in SAFE_RETURNS:
            destination = "/find-matches"
        response = self.delegate.handle(
            method, "/login" if login else target, headers, body_stream,
        )
        if login and response.status == 200:
            return _ResponseView(response, headers=(*response.headers,
                ("Set-Cookie", f"{RETURN_COOKIE}={destination}; Path=/; Max-Age=600; Secure; HttpOnly; SameSite=Lax")))
        if parsed.path == "/auth/google/callback" and response.status == 303:
            cookies = SimpleCookie()
            try:
                cookies.load(headers.get("Cookie", ""))
                destination = cookies[RETURN_COOKIE].value
            except (KeyError, ValueError):
                destination = "/account/profile"
            if destination not in SAFE_RETURNS:
                destination = "/account/profile"
            return _ResponseView(response, headers=tuple(
                (name, destination if name.lower() == "location" else value)
                for name, value in response.headers
            ) + (("Set-Cookie", f"{RETURN_COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax"),))
        if response.status == 401 and method == "GET" and parsed.path in SAFE_RETURNS:
            body = response.body
            if b'href="/login' not in body and b"href='/login" not in body:
                link = f'<p><a href="/login?next={parsed.path}">Sign in to continue</a></p>'.encode()
                body = body.replace(b"</body>", link + b"</body>")
            return _ResponseView(response, body=body, headers=tuple(
                (name, str(len(body)) if name.lower() == "content-length" else value)
                for name, value in response.headers))
        return response


@contextmanager
def existing_owner_local_login(configuration_path, *, account_id, clock,
                               professional_background_preparer=None):
    """Use the stored provider identity, never an owner chosen by a request."""
    configuration_path = Path(configuration_path).resolve(strict=True)
    document = json.loads(configuration_path.read_text(encoding="utf-8"))
    origin = document["public_origin"]
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or parsed.hostname != "localhost"
            or document["bind_host"] != "127.0.0.1"
            or parsed.port != document["bind_port"]):
        raise ValueError("local_login_requires_loopback")
    path = Path(document["database_path"])
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        identities = connection.execute(
            "SELECT provider_subject FROM auth_identities WHERE user_id=? "
            "AND provider='google' AND disabled_at IS NULL", (account_id,),
        ).fetchall()
        if len(identities) != 1:
            raise ValueError("existing_local_identity_required")
        bindings = connection.execute(
            "SELECT p.principal_id FROM product_profiles p "
            "JOIN principal_account_bindings b ON b.principal_id=p.principal_id "
            "WHERE b.user_id=? AND p.environment_namespace=? "
            "AND b.environment_namespace=p.environment_namespace AND b.binding_status='active'",
            (account_id, document["environment"]),
        ).fetchall()
        if len(bindings) != 1:
            raise ValueError("existing_profile_owner_required")
    state = TemporaryBrowserLoginState(
        directory=configuration_path.parent, database_path=path,
        configuration_path=configuration_path, public_origin=origin,
        redirect_uri=document["google_redirect_uri"], subject=identities[0][0],
        account_id=account_id, principal_id=bindings[0][0], profile_id="", clock=clock,
    )
    with controlled_local_product(state, professional_background_preparer=professional_background_preparer) as result:
        yield result


@contextmanager
def controlled_local_product(state, *, professional_background_preparer=None, allow_invited=False):
    """Shared local fixture composition; new subjects require an enabled invitation.

    The caller supplies a private synthetic state, never a browser-selected owner.
    Existing-owner recovery always uses allow_invited=False.
    """
    configuration_path = state.configuration_path
    document = json.loads(configuration_path.read_text(encoding='utf-8'))
    origin = document['public_origin']
    path = state.database_path
    clock = state.clock
    parsed = urlsplit(origin)
    if (parsed.hostname != 'localhost' or parsed.scheme != 'https' or document['bind_host'] != '127.0.0.1'
            or parsed.port != document['bind_port']):
        raise ValueError('local_login_requires_loopback')
    if allow_invited and (parsed.port == 8802 or not (state.directory/'first-time-candidate.json').is_file()):
        raise ValueError('synthetic_invitation_fixture_required')
    if allow_invited:
        marker = json.loads((state.directory/'first-time-candidate.json').read_text(encoding='utf-8'))
        if (type(state) is not TemporaryBrowserLoginState
                or marker.get('synthetic_first_time_candidate_v1') is not True
                or path.resolve().parent != state.directory.resolve()
                or configuration_path.resolve().parent != state.directory.resolve()):
            raise ValueError('synthetic_invitation_fixture_required')
    from wahojobs.database_lifetime_ownership import (
        acquire_database_lifetime_ownership, release_database_lifetime_ownership,
        ROLE_DURABLE_RUNTIME,
    )
    from wahojobs.durable_google_login_runtime import current_closed_schema_is_exact
    from wahojobs.google_oidc_transaction_protection import GoogleOidcTransactionKeyAuthority
    from wahojobs.browser_session_lifecycle import (
        create_request_scoped_session_secret_vault, discard_request_scoped_session_secret_vault,
    )
    from wahojobs.trusted_login_completion import create_trusted_login_completion_policy, prepare_session_delivery
    from wahojobs.accounts import validate_session_csrf, revoke_current_session, SessionUnavailable, StaleSessionVersion

    def validate_logout(connection, *, session_token, csrf_credential, now):
        try:
            validate_session_csrf(connection, session_token=session_token, csrf_secret=csrf_credential, now=now)
        except (SessionUnavailable, sqlite3.Error, ValueError, TypeError):
            return False
        return True

    def revoke_logout(connection, *, session_token, csrf_credential, now):
        try:
            session = validate_session_csrf(connection, session_token=session_token, csrf_secret=csrf_credential, now=now)
            revoke_current_session(connection, session_token=session_token,
                expected_session_version=session.session_version, reason="user_logout", now=now)
        except (SessionUnavailable, StaleSessionVersion, sqlite3.Error, ValueError, TypeError):
            return False
        return True

    with ExitStack() as stack:
        stack.enter_context(loopback_and_in_memory_provider_only())
        config = _load_construction_configuration(configuration_path)
        ownership = acquire_database_lifetime_ownership(path, role=ROLE_DURABLE_RUNTIME)
        stack.callback(release_database_lifetime_ownership, ownership, role=ROLE_DURABLE_RUNTIME, database_path=path)
        connections = _StagingDatabaseConnections(path, ownership)
        stack.callback(connections.close)
        with connections.read_only_connection_provider() as connection:
            # Current closed-schema attestation includes the later intake/public
            # migrations, unlike the older Google launcher's M006 prerequisite.
            if (current_closed_schema_is_exact(connection) is not True
                    or connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok'
                    or connection.execute('PRAGMA foreign_key_check').fetchone() is not None):
                raise ValueError('recovery_database_attestation_failed')
        secret, invitation, lookup, protection = _load_authority_material(config)
        if invitation is not None and not allow_invited:
            raise ValueError('local_recovery_does_not_provision_accounts')
        harness = make_real_gateway(
            clock=clock, client_id=config.google_client_id, client_secret=secret,
            redirect_uri=config.google_redirect_uri, subject=state.subject,
            environment_namespace=config.environment,
            invitation_lookup_key=invitation,
        )
        state.gateway_harnesses.append(harness)
        stack.callback(state.close_harnesses)
        authority = GoogleOidcTransactionKeyAuthority.from_mutable_keys(
            lookup_keys=lookup, protection_keys=protection,
            active_lookup_version=config.oidc_lookup_active_version,
            active_protection_version=config.oidc_protection_active_version,
        )
        stack.callback(authority.close)
        product = _build_profile_integration(connections, SimpleNamespace(
            environment_namespace=document["environment"], public_origin=origin,
            public_job_canary_gate=PublicJobCanaryRoutingGate.disabled(),
        ), clock, professional_background_preparer=professional_background_preparer)
        stack.callback(product.close)
        browser = DurableGoogleLoginBrowserIntegration(
            public_origin=origin, profile_integration=product,
            connection_factory=connections.open_writable_connection,
            gateway=harness.gateway, key_authority=authority,
            completion_policy=create_trusted_login_completion_policy(
                environment_namespace=config.environment, idle_ttl=config.session_idle_ttl,
                absolute_ttl=config.session_absolute_ttl),
            request_secret_vault_factory=create_request_scoped_session_secret_vault,
            prepare_session_delivery=prepare_session_delivery,
            discard_request_secret_vault=discard_request_scoped_session_secret_vault,
            validate_logout=validate_logout, revoke_logout=revoke_logout,
            now=clock, process_guard=connections.require_available,
        )
        stack.callback(browser.close)
        bridge = _ControlledProviderBridge(browser, state, [], claims_overrides=(
            {'email': 'new-candidate@example.test', 'email_verified': True} if allow_invited else None))
        yield config.public_configuration, LocalLoginNavigation(bridge)
