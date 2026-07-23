import contextlib
from datetime import timedelta

from tests.browser_session_lifecycle_test_support import (
    NOW,
    close_secret_vault,
    connect,
    consume_issued,
    lifecycle_database,
    request_secret_vault,
    vault_entry_count,
)
from wahojobs.trusted_login_completion import (
    TrustedExternalIdentityAuthentication,
    TrustedLoginCompletionPolicy,
    TrustedLoginSessionPolicy,
    _ASSERTION_ISSUANCE_CAPABILITY,
    _COMPLETION_POLICY_ISSUANCE_CAPABILITY,
    complete_trusted_login as _complete_trusted_login,
    finalize_pending_trusted_login,
)


TRUSTED_NOW = NOW + timedelta(minutes=1)


class _TestTrustedExternalAuthenticationIssuer:
    __slots__ = ()

    def issue(
        self,
        created,
        *,
        account_id=None,
        identity_id=None,
        provider="google",
        authenticated_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        assurance_policy_version="google_oidc_v1",
        environment_namespace="test",
    ):
        return TrustedExternalIdentityAuthentication._issue(
            _ASSERTION_ISSUANCE_CAPABILITY,
            account_id=account_id or created.user.user_id,
            identity_id=identity_id or created.identity.auth_identity_id,
            provider=provider,
            authenticated_at=authenticated_at,
            expires_at=expires_at,
            assurance_policy_version=assurance_policy_version,
            environment_namespace=environment_namespace,
        )


_TEST_ASSERTION_ISSUER = _TestTrustedExternalAuthenticationIssuer()


class _TestTrustedLoginCompletionPolicyIssuer:
    __slots__ = ()

    def issue(
        self,
        *,
        expected_provider="google",
        expected_assurance_policy_version="google_oidc_v1",
        environment_namespace="test",
        completion_policy_version="trusted_login_completion_v1",
        idle_ttl=timedelta(hours=1),
        absolute_ttl=timedelta(days=7),
    ):
        return TrustedLoginCompletionPolicy._issue(
            _COMPLETION_POLICY_ISSUANCE_CAPABILITY,
            expected_provider=expected_provider,
            expected_assurance_policy_version=expected_assurance_policy_version,
            environment_namespace=environment_namespace,
            completion_policy_version=completion_policy_version,
            session_policy=session_policy(
                environment_namespace=environment_namespace,
                idle_ttl=idle_ttl,
                absolute_ttl=absolute_ttl,
            ),
        )


_TEST_COMPLETION_POLICY_ISSUER = _TestTrustedLoginCompletionPolicyIssuer()


def trusted_assertion(created, **kwargs):
    return _TEST_ASSERTION_ISSUER.issue(created, **kwargs)


def session_policy(
    *,
    environment_namespace="test",
    idle_ttl=timedelta(hours=1),
    absolute_ttl=timedelta(days=7),
):
    return TrustedLoginSessionPolicy(
        environment_namespace=environment_namespace,
        idle_ttl=idle_ttl,
        absolute_ttl=absolute_ttl,
    )


def completion_policy(**kwargs):
    return _TEST_COMPLETION_POLICY_ISSUER.issue(**kwargs)


def complete_login(
    connection,
    assertion,
    *,
    policy=None,
    vault=None,
    trusted_now=TRUSTED_NOW,
    key="trusted-login-completion-001",
    **kwargs,
):
    vault = vault or request_secret_vault()
    result = _complete_trusted_login(
        connection,
        assertion,
        policy or completion_policy(),
        vault,
        trusted_now=trusted_now,
        idempotency_key=key,
        **kwargs,
    )
    return result, vault


def finalize_login(connection, result, vault, *, trusted_now=TRUSTED_NOW):
    return finalize_pending_trusted_login(
        connection,
        result,
        vault,
        trusted_now=trusted_now,
    )


def consume_login(result, vault, *, now=TRUSTED_NOW):
    return consume_issued(result.issued_session, vault=vault, now=now)


@contextlib.contextmanager
def login_database(*, suffix="trusted-login"):
    with lifecycle_database(suffix=suffix) as state:
        yield state


__all__ = (
    "NOW",
    "TRUSTED_NOW",
    "close_secret_vault",
    "completion_policy",
    "complete_login",
    "connect",
    "consume_login",
    "finalize_login",
    "login_database",
    "request_secret_vault",
    "session_policy",
    "trusted_assertion",
    "vault_entry_count",
)
