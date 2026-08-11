from __future__ import annotations

import base64
import contextlib
import copy
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import io
import json
from pathlib import Path
import socket
import threading
from types import MappingProxyType
from unittest import mock
from urllib.parse import parse_qsl, parse_qs, urlencode, urlsplit
import weakref

from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey
from requests import Response
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError as RequestsConnectionError
from urllib3.response import HTTPResponse

from tests.accounts_test_support import create_user, install_accounts
from tests.browser_session_lifecycle_test_support import (
    close_secret_vault,
    request_secret_vault,
    vault_entry_count,
)
from tests.trusted_login_completion_test_support import completion_policy
import wahojobs.google_oidc_gateway as _gateway
from wahojobs.ownership import ensure_account_native_principal


NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
IDENTITY_CREATED_AT = NOW - timedelta(minutes=5)
CLIENT_ID = "test-google-client.apps.test.invalid"
CLIENT_SECRET = b"test-google-client-secret-fixture-only"
REDIRECT_URI = "https://accounts-d.test.invalid/callback"
DEFAULT_SUBJECT = "google-subject-google-oidc"
DEFAULT_CODE = "test-authorization-code"

_DEFAULT = object()
_RESPONSE_PLAN_CAPABILITY = object()
_REGISTRY_LOCK = threading.Lock()
_REGISTERED_TRANSPORTS = weakref.WeakSet()
_REGISTERED_CLOCKS = {}
_ROUTER_LOCAL = threading.local()


class ManualClock:
    """Thread-safe wall and monotonic clocks which advance independently."""

    __slots__ = ("_lock", "_now", "_monotonic")

    def __init__(self, now=NOW, monotonic=10_000.0):
        if type(now) is not datetime or now.tzinfo is None:
            raise TypeError("aware_datetime_required")
        if type(monotonic) not in {int, float} or monotonic < 0:
            raise TypeError("nonnegative_monotonic_required")
        self._lock = threading.Lock()
        self._now = now.astimezone(timezone.utc).replace(microsecond=0)
        self._monotonic = float(monotonic)

    def __call__(self):
        with self._lock:
            return self._now

    def monotonic(self):
        with self._lock:
            return self._monotonic

    def advance(self, seconds):
        if type(seconds) not in {int, float}:
            raise TypeError("numeric_seconds_required")
        with self._lock:
            self._now += timedelta(seconds=seconds)
            self._monotonic += float(seconds)
            return self._now

    def advance_wall(self, seconds):
        if type(seconds) not in {int, float}:
            raise TypeError("numeric_seconds_required")
        with self._lock:
            self._now += timedelta(seconds=seconds)
            return self._now

    def advance_monotonic(self, seconds):
        if type(seconds) not in {int, float}:
            raise TypeError("numeric_seconds_required")
        with self._lock:
            self._monotonic += float(seconds)
            return self._monotonic


_PRIMARY_PRIVATE_JWK_JSON = r"""
{"d":"DRzefUsyKuDLY-uG5u-ciyGJ2OELePxIQ73WXGLKYcx1hXo4W5LcNvHVvtV4qXq0kpqkimnWGvGwcxqEhHBeGDfw9nyMqjx5-6XVJAnxHyNFTTv8b52nVwk9cNKPldykhTOA3Soo1CGxbHy7FPM50j62J14zhAao2ux5PaFbaoT7nUOxjuHN6DwvdbEZ3M1gl4WmIM972ECQN6N_DY-RTW1cKFhiCcvl9_R8mud1csoU9GEbsNNXHW1ihG5CTFJZdY4SL5OBk-Wa12j2dHuStNsq7V_qwoN10sVb68WMFY90dIMbTj28lTMh2KCukDEfZXYsQLeP4f79wRmwzNk2bQ","dp":"S0JL8qLOQGcDXLC46eK5BbTZOzu_iqcE8AhRHPjpZnBSeuAlGll6_0GpU04484_dDu1Mhl8wO-985Vq2YBT39-428-GbKyg104iR-PG2hPGcpVbwr-doqbRsU5AoAmm3K5TnX8Po_BsaOOhQvaYC_KaFnckuYl6Mw0vsrcEGcLM","dq":"n9mm_ASadmWeYGAkMCPqG5Y6AzPdLZNfFfg8_prVAROd_BjPf7ZjPR4d3Wd2VauCK0HXmlwdFSOKhrTo8R6zYOX4QnFr6bqoT7Vm1ciJYh29vLOp0FcRGjaHzbjt7JalSOI0eP52WhYiC_bxnacozA9d-eyI0DHJPpfJBNTaHJ0","e":"AQAB","kid":"fixture-key-1","kty":"RSA","n":"9riqxLoBDxOCLYM9OEAM8ODGUuVb4ojRzy5x6Ua5Ia-3bhD3LXLx6T6I7ICt9lNS_ORpqthvk2gDlgpVyqIZQJf6sNIjHZFz_6S5_Du3HzpgSvaByi7KF5YBG2bpoH77E5ao99satnsS3XSxWQOx2-dJGRN68HfprOyfDcBB8RF3__ikni_ZfcaauIv1KIrPJRviwItz6dGkX1h1HanfooIMDA4VaX37RhwoKercKhBtagYPeQlz8E-zmum2qbs3NGLSdfcMlAaE8HSV4GKm2bNB2ArbAUCSSMVAYnqt6NkfXho3DpUKEnUinTfG2sRKz4z6IRrMN2Eu7nq4PAg8fw","p":"_Gexxmtbwn7xO07Ll5vy9RXgle_RVzfr3W5BzvOShbpXvNqtCj1ACFc0o1VGi1K2bIfSCd_Hwq9twVj2ieOSfYG4oPz8zOLdKXgCOIMauImhUiYY2ZO3pYPOX-CAGw_22LZLjBNzgjwcMVgY6r9bR-3l_HJzGLT8wxqiISq__-s","q":"-jw_wHnZKhCZvZht7Exmp13eBayCc6LQJZ1cRdS9qLIC0sXjQD-c6Ud7pXVyeLtC8cVNYpz3RPxlcpViewRpcVkIp7SB-RTYjhicElD-JrOw_G5tB1AL8bBcF4KX9uFL3VZxOw6nRSgCtBCw63Pck-h1N9m2ikYMfSp6UFlR5L0","qi":"ZPrTcBSMSodjLoJZp7y7eojmz-eN0KfApIuS3Tjvyd4GhaCwWgt44hQD9IsVW7Bkhyt8GvkJU5efVxa3hk3fKAUYyTZ2K2tPIBAbluWvLqrWOvCv-zNNVOGFHPwqkPNAFHiqPp0U_GKIUz7xCXLQGksychxQHNQaWGHowhF4CQQ","use":"sig"}
"""

_ROTATED_PRIVATE_JWK_JSON = r"""
{"d":"AavddImebhvw0O3ZIFrcdUSpQJwJw5lQMYpzt3fIKUei-nnxIcQpyxPr-jahgyXQ-AvUHIf2rwRfrZg4SO_aMrmEkySm_UFc5LZStyaiNzjoSH8NyzORF5PZI3VtmN3pNq9BiwtXBlC4OuT2v2olJ9u0vES3Bn-tKlk0SOywUw3WO0VuYdUpI1j6X02tYeA4BrnVd536tUbfV4qS_cFuGe-0-y20m_ENHFPd_HYZRTPZTFHfrPrL-J_-88a-lXG8DUdReNUtUPTIuyxptEeAkPajwCBxd8CxlleqeXIWGCp_0gILGeVauXdOWqMZOkmEt0BK4CKhTeD0tb8Qw1QIsQ","dp":"VW8G3zge-OjFjuKEuO1g0hHgqODCdqbkSrsdqUNca_5tt1zOYj52esdfzXSC43yrLLRuE_YprkByJ-6T5AWmXsOjKpfA79_vbiD5Uk60K89DjKylLtbpCq5ykoq_SlQNh3bOYsDoAxfT2zCk70LShQMhpCY1bO2ikFHNxK4l420","dq":"pFN7sjt60xYgQKa4NzbraOYyXiCfUb7ESs8_TZUXyghauJiwHZT0T6C5vnCn7BFcnpVbDldtbwmDExlkwQqWMVLriawH3NBYXlry1wgzRY07izNCwhK6uP459bGFZH6S6IT8i3-ABAWCTGCVak1D2SZbX4dBj5jcQUnD0ihzdck","e":"AQAB","kid":"fixture-key-2","kty":"RSA","n":"qm38k1P-I5kfxhwkue49eO_-6bC5UCIDcnSSOyZeqlITuNQRkbQQbVg_8b5TzEUJERY_y3OjV9oykvB1FrgT08DJVcTcyqVco6LiRmcSYoN8y4QokirlfVPQFbwmCZpNHOfpp4bHa7koMi-TfTqUu99BbwgfjWd4Gm-zu7evRxdOqg8SJv9mLrThZrWJcNNeI8iE3NOJ9MME3VZO1O6b3y8pXtL0MzNC-scb-Jd97P5dBnW-wz8UWOSrdF17o3vM0v5j0AGLU9S8XzycO2aK204VZojHGZiuv2spoKtHYRfFbrsphu0jBP8vldAVfpnxsJh67xfujKYDZGa3VSo4FQ","p":"7HojQBgzIZGtdML2A61e3vRoCwAiKP4pTGc9KymXTzrkDFpooUickuSIVDVqd6-hII0bf-ZfCyYS9_l3r789QWcohAbUBdkXGzf3_cBX0nP26scdll2yc1xGHeu269mM42fI1NzQi5rql5IQJwH56_jZcmFkOvspVaApand1iN0","q":"uH_1W-QKiXrEYRiEBOz_1AJh89jZt-8UeaQOz1VLpVIySoLY5v4pcqJ2a4pDFCP7zpcUEDz19oxqEUFr4v-kMoLUlFLlr1Rh5svqcsnDTN7-S7yJ0sWiORAIt--F7WnALvrFwU94y1jcYgZv9one6nHq4LXj4eOtHnNFV7PsXJk","qi":"4TcvAzK7Vev6RupEaXCuI_tkXAV1spg69hhtv8mkmcyiGauyH9yDCBTiy0yfmOQo95uEalMnQh25v0SwD_7DFjcIyG1qKuG0IabGbcQCelOzYFn1RxQPIGjFYBRvJOKcSFL47T_7wOP3F6JguKKins0TTpFiVqMJ-yjkWasYgCI","use":"sig"}
"""


class SigningFixture:
    """Redacted static RSA fixture imported through joserfc on every use."""

    __slots__ = ("_name", "_private_jwk")

    def __init__(self, name, private_jwk_json):
        document = json.loads(private_jwk_json)
        if (
            type(document) is not dict
            or document.get("kty") != "RSA"
            or type(document.get("kid")) is not str
        ):
            raise TypeError("rsa_fixture_required")
        self._name = name
        self._private_jwk = MappingProxyType(document)

    @property
    def kid(self):
        return self._private_jwk["kid"]

    def signing_key(self):
        return RSAKey.import_key(dict(self._private_jwk))

    def public_jwk(self):
        return self.signing_key().as_dict(private=False)

    def __repr__(self):
        return f"SigningFixture({self._name!r}, <redacted>)"


PRIMARY_SIGNING_FIXTURE = SigningFixture(
    "primary",
    _PRIMARY_PRIVATE_JWK_JSON,
)
ROTATED_SIGNING_FIXTURE = SigningFixture(
    "rotated",
    _ROTATED_PRIVATE_JWK_JSON,
)


def authorization_parameters(prepared):
    pairs = parse_qsl(
        urlsplit(prepared.authorization_url).query,
        keep_blank_values=True,
        strict_parsing=True,
        max_num_fields=16,
    )
    values = {}
    for name, value in pairs:
        if name in values:
            raise AssertionError("duplicate_authorization_parameter")
        values[name] = value
    return values


def valid_id_token_claims(
    *,
    nonce,
    now=NOW,
    subject=DEFAULT_SUBJECT,
    issuer="https://accounts.google.com",
    audience=CLIENT_ID,
    authenticated_at=None,
    issued_at=None,
    expires_at=None,
    include_azp=True,
    include_nbf=True,
    authorized_party=None,
):
    authenticated_at = authenticated_at or now
    issued_at = issued_at or now
    expires_at = expires_at or now + timedelta(minutes=5)
    claims = {
        "iss": issuer,
        "sub": subject,
        "aud": audience,
        "exp": int(expires_at.timestamp()),
        "iat": int(issued_at.timestamp()),
        "nonce": nonce,
        "auth_time": int(authenticated_at.timestamp()),
    }
    if include_azp:
        claims["azp"] = (
            authorized_party
            if authorized_party is not None
            else audience
        )
    if include_nbf:
        claims["nbf"] = int(issued_at.timestamp())
    return claims


def signed_id_token(
    claims,
    *,
    signing_fixture=PRIMARY_SIGNING_FIXTURE,
    algorithm="RS256",
    header_overrides=None,
):
    header = {
        "alg": algorithm,
        "kid": signing_fixture.kid,
        "typ": "JWT",
    }
    if header_overrides:
        header.update(header_overrides)
    if algorithm == "RS256":
        key = signing_fixture.signing_key()
    elif algorithm == "HS256":
        key = OctKey.import_key(
            b"test-only-unapproved-hs256-key-32",
            {"kid": header.get("kid", "fixture-oct-key")},
        )
    else:
        raise ValueError("unsupported_test_signing_algorithm")
    return jwt.encode(
        header,
        dict(claims),
        key,
        algorithms=[algorithm],
    )


def jwks_document(*signing_fixtures):
    fixtures = signing_fixtures or (PRIMARY_SIGNING_FIXTURE,)
    return {"keys": [fixture.public_jwk() for fixture in fixtures]}


@dataclass(frozen=True, slots=True)
class RequestObservation:
    role: str
    method: str
    url: str
    timeout: object
    verify_enabled: bool
    streamed: bool
    accept_encoding: str | None
    parameter_names: tuple[str, ...]
    exact_client: bool | None
    exact_redirect: bool | None
    exact_pkce: bool | None


class _AuthorizationRecord:
    __slots__ = (
        "nonce",
        "code_challenge",
        "claims_overrides",
        "missing_claims",
        "signing_fixture",
        "algorithm",
        "header_overrides",
        "raw_id_token",
    )

    def __init__(
        self,
        *,
        nonce,
        code_challenge,
        claims_overrides,
        missing_claims,
        signing_fixture,
        algorithm,
        header_overrides,
        raw_id_token,
    ):
        self.nonce = bytearray(nonce.encode("ascii"))
        self.code_challenge = code_challenge
        self.claims_overrides = dict(claims_overrides or ())
        self.missing_claims = tuple(missing_claims)
        self.signing_fixture = signing_fixture
        self.algorithm = algorithm
        self.header_overrides = dict(header_overrides or ())
        self.raw_id_token = raw_id_token

    def clear(self):
        self.nonce[:] = b"\x00" * len(self.nonce)
        self.nonce.clear()
        self.code_challenge = None
        self.claims_overrides.clear()
        self.missing_claims = ()
        self.signing_fixture = None
        self.algorithm = None
        self.header_overrides.clear()
        self.raw_id_token = None


class _ResponsePlan:
    __slots__ = (
        "body",
        "status",
        "content_type",
        "url",
        "declared_length",
        "location",
        "history",
        "exception",
        "read_exception",
        "read_failure_after",
        "headers",
    )

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("response_plan_not_constructible")

    @classmethod
    def _issue(
        cls,
        capability,
        *,
        body,
        status,
        content_type,
        url,
        declared_length,
        location,
        history,
        exception,
        read_exception,
        read_failure_after,
        headers,
    ):
        if capability is not _RESPONSE_PLAN_CAPABILITY:
            raise TypeError("response_plan_authority_required")
        instance = object.__new__(cls)
        instance.body = body
        instance.status = status
        instance.content_type = content_type
        instance.url = url
        instance.declared_length = declared_length
        instance.location = location
        instance.history = history
        instance.exception = exception
        instance.read_exception = read_exception
        instance.read_failure_after = read_failure_after
        instance.headers = dict(headers or ())
        return instance


def _response_plan(
    *,
    document=_DEFAULT,
    body=None,
    status=200,
    content_type="application/json",
    url=None,
    declared_length=None,
    location=None,
    history=False,
    exception=None,
    read_exception=None,
    read_failure_after=0,
    headers=None,
):
    if document is not _DEFAULT and body is not None:
        raise TypeError("response_plan_body_is_ambiguous")
    if document is not _DEFAULT:
        body = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    elif body is None:
        body = b""
    elif type(body) is str:
        body = body.encode("utf-8")
    elif type(body) is bytearray:
        body = bytes(body)
    elif type(body) is not bytes:
        raise TypeError("response_plan_body_invalid")
    if (
        read_exception is not None
        and (
            not isinstance(read_exception, BaseException)
            or type(read_failure_after) is not int
            or read_failure_after < 0
        )
    ):
        raise TypeError("response_plan_read_failure_invalid")
    return _ResponsePlan._issue(
        _RESPONSE_PLAN_CAPABILITY,
        body=body,
        status=status,
        content_type=content_type,
        url=url,
        declared_length=declared_length,
        location=location,
        history=history,
        exception=exception,
        read_exception=read_exception,
        read_failure_after=read_failure_after,
        headers=headers,
    )


@dataclass(frozen=True, slots=True)
class ResponseLifecycleObservation:
    role: str
    close_count: int
    read_started: bool
    read_completed: bool
    decoded_bytes: int


class _ResponseLifecycle:
    __slots__ = (
        "role",
        "close_count",
        "read_started",
        "read_completed",
        "decoded_bytes",
    )

    def __init__(self):
        self.role = None
        self.close_count = 0
        self.read_started = False
        self.read_completed = False
        self.decoded_bytes = 0

    def snapshot(self):
        return ResponseLifecycleObservation(
            role=self.role,
            close_count=self.close_count,
            read_started=self.read_started,
            read_completed=self.read_completed,
            decoded_bytes=self.decoded_bytes,
        )


class _TrackedRawResponse:
    __slots__ = (
        "_raw",
        "_lifecycle",
        "_read_exception",
        "_read_failure_after",
        "_failed",
    )

    def __init__(
        self,
        body,
        headers,
        lifecycle,
        read_exception,
        read_failure_after,
    ):
        raw_headers = dict(headers)
        raw_headers.pop("Content-Length", None)
        self._raw = HTTPResponse(
            body=io.BytesIO(body),
            headers=raw_headers,
            preload_content=False,
            decode_content=False,
        )
        self._lifecycle = lifecycle
        self._read_exception = read_exception
        self._read_failure_after = read_failure_after
        self._failed = False

    @property
    def headers(self):
        return self._raw.headers

    def stream(self, amt=8192, decode_content=True):
        self._lifecycle.read_started = True
        if (
            self._read_exception is not None
            and self._read_failure_after == 0
        ):
            self._failed = True
            raise self._read_exception
        for chunk in self._raw.stream(
            amt=amt,
            decode_content=decode_content,
        ):
            if self._read_exception is not None and not self._failed:
                remaining = (
                    self._read_failure_after
                    - self._lifecycle.decoded_bytes
                )
                if remaining <= 0:
                    self._failed = True
                    raise self._read_exception
                if len(chunk) > remaining:
                    partial = chunk[:remaining]
                    self._lifecycle.decoded_bytes += len(partial)
                    if partial:
                        yield partial
                    self._failed = True
                    raise self._read_exception
            self._lifecycle.decoded_bytes += len(chunk)
            yield chunk
        self._lifecycle.read_completed = True

    def close(self):
        self._raw.close()

    def release_conn(self):
        self._raw.release_conn()

    def __getattr__(self, name):
        return getattr(self._raw, name)


class _TrackedResponse(Response):
    def __init__(
        self,
        *,
        body,
        headers,
        read_exception,
        read_failure_after,
    ):
        super().__init__()
        lifecycle = _ResponseLifecycle()
        self._fixture_lifecycle = lifecycle
        self.headers.update(headers)
        self.raw = _TrackedRawResponse(
            body,
            self.headers,
            lifecycle,
            read_exception,
            read_failure_after,
        )
        self._content = False
        self._content_consumed = False

    def close(self):
        self._fixture_lifecycle.close_count += 1
        super().close()


class InMemoryGoogleTransport:
    """Support-owned HTTP fixture reached only through ``sockets_blocked``."""

    __slots__ = (
        "_lock",
        "_clock",
        "_client_id",
        "_redirect_uri",
        "_secret_digest",
        "_subject",
        "_authenticated_at",
        "_token_expires_at",
        "_outcomes",
        "_next_outcome",
        "_call_count",
        "_control_flow_exception",
        "_authorizations",
        "_token_plans",
        "_jwks_plans",
        "_observations",
        "_response_lifecycles",
        "_signing_fixture",
        "_jwks_fixtures",
        "_block_next_jwks",
        "_closed",
        "entered",
        "release",
        "jwks_entered",
        "jwks_release",
        "__weakref__",
    )

    def __init__(
        self,
        *,
        clock,
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        client_secret=CLIENT_SECRET,
        subject=DEFAULT_SUBJECT,
        authenticated_at=None,
        token_expires_at=None,
        outcomes=("success",),
        block=False,
    ):
        if type(client_secret) is bytearray:
            secret_bytes = bytes(client_secret)
        elif type(client_secret) is bytes:
            secret_bytes = client_secret
        else:
            raise TypeError("bytes_client_secret_required")
        allowed_outcomes = {
            "success",
            "authentication_denied",
            "provider_unavailable",
            "runtime_error",
            "keyboard_interrupt",
            "system_exit",
            "generator_exit",
        }
        outcomes = tuple(outcomes)
        if not outcomes or any(item not in allowed_outcomes for item in outcomes):
            raise TypeError("in_memory_google_transport_outcome_invalid")
        self._lock = threading.Lock()
        self._clock = clock
        self._client_id = client_id
        self._redirect_uri = redirect_uri
        self._secret_digest = hashlib.sha256(secret_bytes).digest()
        self._subject = subject
        self._authenticated_at = authenticated_at
        self._token_expires_at = token_expires_at
        self._outcomes = outcomes
        self._next_outcome = 0
        self._call_count = 0
        self._control_flow_exception = None
        self._authorizations = {}
        self._token_plans = deque()
        self._jwks_plans = deque()
        self._observations = []
        self._response_lifecycles = []
        self._signing_fixture = PRIMARY_SIGNING_FIXTURE
        self._jwks_fixtures = (PRIMARY_SIGNING_FIXTURE,)
        self._block_next_jwks = False
        self._closed = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.jwks_entered = threading.Event()
        self.jwks_release = threading.Event()
        self.jwks_release.set()
        if not block:
            self.release.set()
        _register_transport(self)

    def __reduce_ex__(self, _protocol):
        raise TypeError("in_memory_google_transport_not_serializable")

    def __copy__(self):
        raise TypeError("in_memory_google_transport_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("in_memory_google_transport_not_copyable")

    @property
    def observations(self):
        with self._lock:
            return tuple(self._observations)

    @property
    def response_lifecycles(self):
        with self._lock:
            return tuple(
                lifecycle.snapshot()
                for lifecycle in self._response_lifecycles
            )

    @property
    def token_request_count(self):
        return sum(item.role == "token" for item in self.observations)

    @property
    def jwks_request_count(self):
        return sum(item.role == "jwks" for item in self.observations)

    @property
    def pending_authorization_count(self):
        with self._lock:
            return len(self._authorizations)

    @property
    def call_count(self):
        with self._lock:
            return self._call_count

    @property
    def control_flow_exception(self):
        with self._lock:
            return self._control_flow_exception

    def use_signing_fixture(self, fixture):
        if type(fixture) is not SigningFixture:
            raise TypeError("signing_fixture_required")
        with self._lock:
            self._signing_fixture = fixture

    def use_jwks_fixtures(self, *fixtures):
        if any(type(item) is not SigningFixture for item in fixtures):
            raise TypeError("signing_fixture_required")
        with self._lock:
            self._jwks_fixtures = tuple(fixtures)

    def block_next_jwks(self):
        with self._lock:
            if self._closed:
                raise AssertionError("closed_in_memory_transport")
            self._block_next_jwks = True
            self.jwks_entered.clear()
            self.jwks_release.clear()

    def callback_for(
        self,
        prepared,
        *,
        code=DEFAULT_CODE,
        state=_DEFAULT,
        issuer="https://accounts.google.com",
        error=None,
        base_uri=REDIRECT_URI,
        extra_pairs=(),
        claims_overrides=None,
        missing_claims=(),
        signing_fixture=None,
        algorithm="RS256",
        header_overrides=None,
        raw_id_token=None,
    ):
        parameters = authorization_parameters(prepared)
        if state is _DEFAULT:
            state = parameters["state"]
        pairs = []
        if error is None:
            pairs.append(("code", code))
            self._register_authorization(
                code,
                nonce=parameters["nonce"],
                code_challenge=parameters["code_challenge"],
                claims_overrides=claims_overrides,
                missing_claims=missing_claims,
                signing_fixture=signing_fixture,
                algorithm=algorithm,
                header_overrides=header_overrides,
                raw_id_token=raw_id_token,
            )
        else:
            pairs.append(("error", error))
        if state is not None:
            pairs.append(("state", state))
        if issuer is not None:
            pairs.append(("iss", issuer))
        pairs.extend(tuple(extra_pairs))
        return base_uri + "?" + urlencode(pairs)

    def discard_authorization(self, code=DEFAULT_CODE):
        key = hashlib.sha256(code.encode("utf-8")).digest()
        with self._lock:
            record = self._authorizations.pop(key, None)
        if record is not None:
            record.clear()

    def queue_token_response(self, **kwargs):
        plan = _response_plan(**kwargs)
        with self._lock:
            self._token_plans.append(plan)

    def queue_jwks_response(self, **kwargs):
        plan = _response_plan(**kwargs)
        with self._lock:
            self._jwks_plans.append(plan)

    def close(self):
        self.release.set()
        self.jwks_release.set()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            records = tuple(self._authorizations.values())
            self._authorizations.clear()
            self._token_plans.clear()
            self._jwks_plans.clear()
            self._observations.clear()
            self._response_lifecycles.clear()
            self._signing_fixture = None
            self._jwks_fixtures = ()
            self._block_next_jwks = False
            self._secret_digest = b""
            self._outcomes = ()
            self._subject = None
            self._authenticated_at = None
            self._token_expires_at = None
        for record in records:
            record.clear()
        _unregister_transport(self)

    def _register_authorization(
        self,
        code,
        *,
        nonce,
        code_challenge,
        claims_overrides,
        missing_claims,
        signing_fixture,
        algorithm,
        header_overrides,
        raw_id_token,
    ):
        if type(code) is not str or not code:
            raise TypeError("authorization_code_required")
        fixture = signing_fixture
        with self._lock:
            if fixture is None:
                fixture = self._signing_fixture
            key = hashlib.sha256(code.encode("utf-8")).digest()
            previous = self._authorizations.pop(key, None)
            self._authorizations[key] = _AuthorizationRecord(
                nonce=nonce,
                code_challenge=code_challenge,
                claims_overrides=claims_overrides,
                missing_claims=missing_claims,
                signing_fixture=fixture,
                algorithm=algorithm,
                header_overrides=header_overrides,
                raw_id_token=raw_id_token,
            )
        if previous is not None:
            previous.clear()

    def _token_match_rank(self, request):
        form = _request_form(request.body)
        code = _single(form, "code")
        if type(code) is not str:
            return 0
        code_key = hashlib.sha256(code.encode("utf-8")).digest()
        with self._lock:
            if self._closed:
                return 0
            record = self._authorizations.get(code_key)
            if record is None:
                return 0
            verifier = _single(form, "code_verifier")
            if (
                type(verifier) is str
                and hmac.compare_digest(
                    _pkce_challenge(verifier),
                    record.code_challenge,
                )
            ):
                return 2
            return 1

    def _send(self, role, request, *, stream, timeout, verify):
        if role == "token":
            response = self._send_token(
                request,
                stream=stream,
                timeout=timeout,
                verify=verify,
            )
        elif role == "jwks":
            response = self._send_jwks(
                request,
                stream=stream,
                timeout=timeout,
                verify=verify,
            )
        else:
            raise AssertionError("unexpected_transport_role")
        lifecycle = getattr(response, "_fixture_lifecycle", None)
        if type(lifecycle) is _ResponseLifecycle:
            lifecycle.role = role
            with self._lock:
                self._response_lifecycles.append(lifecycle)
        return response

    def _send_token(self, request, *, stream, timeout, verify):
        form = _request_form(request.body)
        code = _single(form, "code")
        code_key = (
            hashlib.sha256(code.encode("utf-8")).digest()
            if type(code) is str
            else None
        )
        with self._lock:
            record = self._authorizations.get(code_key)
            plan = self._token_plans.popleft() if self._token_plans else None
        exact_client = _single(form, "client_id") == self._client_id
        exact_redirect = _single(form, "redirect_uri") == self._redirect_uri
        supplied_secret = _single(form, "client_secret")
        exact_secret = (
            type(supplied_secret) is str
            and hmac.compare_digest(
                hashlib.sha256(supplied_secret.encode("utf-8")).digest(),
                self._secret_digest,
            )
        )
        exact_pkce = False
        if record is not None:
            verifier = _single(form, "code_verifier")
            if type(verifier) is str:
                exact_pkce = hmac.compare_digest(
                    _pkce_challenge(verifier),
                    record.code_challenge,
                )
        observation = RequestObservation(
            role="token",
            method=request.method,
            url=request.url,
            timeout=timeout,
            verify_enabled=verify is True,
            streamed=stream is True,
            accept_encoding=request.headers.get("Accept-Encoding"),
            parameter_names=tuple(sorted(form)),
            exact_client=exact_client and exact_secret,
            exact_redirect=exact_redirect,
            exact_pkce=exact_pkce,
        )
        self._record_observation(observation)
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise RequestsConnectionError(
                "in_memory_google_transport_release_timeout"
            )
        with self._lock:
            if self._closed:
                raise RequestsConnectionError(
                    "closed_in_memory_google_transport"
                )
            index = min(self._next_outcome, len(self._outcomes) - 1)
            outcome = self._outcomes[index]
            self._next_outcome += 1
            self._call_count += 1
        if outcome == "authentication_denied":
            return _json_response(
                request,
                {"error": "invalid_grant"},
                status=400,
            )
        if outcome == "provider_unavailable":
            raise RequestsConnectionError(
                "deterministic_google_provider_unavailable"
            )
        if outcome == "runtime_error":
            raise RuntimeError("deterministic_google_provider_failure")
        if outcome in {"keyboard_interrupt", "system_exit", "generator_exit"}:
            exception_type = {
                "keyboard_interrupt": KeyboardInterrupt,
                "system_exit": SystemExit,
                "generator_exit": GeneratorExit,
            }[outcome]
            exception = exception_type("deterministic_control_flow")
            with self._lock:
                self._control_flow_exception = exception
            raise exception

        consumed_record = None
        try:
            if plan is not None:
                response = _planned_response(plan, request)
                with self._lock:
                    if self._authorizations.get(code_key) is record:
                        consumed_record = self._authorizations.pop(code_key)
                return response
            valid = (
                record is not None
                and request.method == "POST"
                and exact_client
                and exact_secret
                and exact_redirect
                and exact_pkce
                and _single(form, "grant_type") == "authorization_code"
            )
            if not valid:
                return _json_response(
                    request,
                    {"error": "invalid_grant"},
                    status=400,
                )
            id_token = record.raw_id_token
            if id_token is None:
                nonce = bytes(record.nonce).decode("ascii")
                now = self._clock()
                claims = valid_id_token_claims(
                    nonce=nonce,
                    now=now,
                    subject=self._subject,
                    audience=self._client_id,
                    authenticated_at=self._authenticated_at,
                    expires_at=self._token_expires_at,
                )
                claims["azp"] = self._client_id
                claims.update(record.claims_overrides)
                for name in record.missing_claims:
                    claims.pop(name, None)
                id_token = signed_id_token(
                    claims,
                    signing_fixture=record.signing_fixture,
                    algorithm=record.algorithm,
                    header_overrides=record.header_overrides,
                )
            with self._lock:
                if self._authorizations.get(code_key) is record:
                    consumed_record = self._authorizations.pop(code_key)
            return _json_response(
                request,
                {
                    "access_token": "test-access-token-not-retained",
                    "refresh_token": "test-refresh-token-not-retained",
                    "token_type": "Bearer",
                    "expires_in": 300,
                    "id_token": id_token,
                },
            )
        finally:
            if consumed_record is not None:
                consumed_record.clear()

    def _send_jwks(self, request, *, stream, timeout, verify):
        with self._lock:
            plan = self._jwks_plans.popleft() if self._jwks_plans else None
            fixtures = self._jwks_fixtures
            should_block = self._block_next_jwks
            self._block_next_jwks = False
        self._record_observation(
            RequestObservation(
                role="jwks",
                method=request.method,
                url=request.url,
                timeout=timeout,
                verify_enabled=verify is True,
                streamed=stream is True,
                accept_encoding=request.headers.get("Accept-Encoding"),
                parameter_names=(),
                exact_client=None,
                exact_redirect=None,
                exact_pkce=None,
            )
        )
        self.jwks_entered.set()
        if should_block and not self.jwks_release.wait(timeout=10):
            raise RequestsConnectionError(
                "in_memory_google_jwks_release_timeout"
            )
        if plan is not None:
            return _planned_response(plan, request)
        document = (
            jwks_document(*fixtures)
            if fixtures
            else {"keys": []}
        )
        return _json_response(request, document)

    def _record_observation(self, observation):
        with self._lock:
            self._observations.append(observation)


def _register_transport(transport):
    with _REGISTRY_LOCK:
        _REGISTERED_TRANSPORTS.add(transport)


def _unregister_transport(transport):
    with _REGISTRY_LOCK:
        _REGISTERED_TRANSPORTS.discard(transport)
    if getattr(_ROUTER_LOCAL, "transport", None) is transport:
        _ROUTER_LOCAL.transport = None


def _transport_snapshot():
    with _REGISTRY_LOCK:
        return tuple(_REGISTERED_TRANSPORTS)


def _select_token_transport(request):
    transports = _transport_snapshot()
    ranked = tuple(
        (transport._token_match_rank(request), transport)
        for transport in transports
    )
    best_rank = max((rank for rank, _transport in ranked), default=0)
    if best_rank:
        matches = tuple(
            transport
            for rank, transport in ranked
            if rank == best_rank
        )
        if len(matches) == 1:
            return matches[0]
        previous = getattr(_ROUTER_LOCAL, "transport", None)
        if previous in matches:
            return previous
        raise AssertionError("ambiguous_in_memory_token_transport")
    previous = getattr(_ROUTER_LOCAL, "transport", None)
    if previous in transports:
        return previous
    if len(transports) == 1:
        return transports[0]
    raise AssertionError("unregistered_in_memory_token_request")


def _route_http_adapter_send(
    _adapter,
    request,
    stream=False,
    timeout=None,
    verify=True,
    cert=None,
    proxies=None,
):
    cert = None
    proxies = None
    if request.url == "https://oauth2.googleapis.com/token":
        transport = _select_token_transport(request)
        _ROUTER_LOCAL.transport = transport
        return transport._send(
            "token",
            request,
            stream=stream,
            timeout=timeout,
            verify=verify,
        )
    if request.url == "https://www.googleapis.com/oauth2/v3/certs":
        transport = getattr(_ROUTER_LOCAL, "transport", None)
        transports = _transport_snapshot()
        if transport not in transports:
            if len(transports) != 1:
                raise AssertionError("unregistered_in_memory_jwks_request")
            transport = transports[0]
            _ROUTER_LOCAL.transport = transport
        return transport._send(
            "jwks",
            request,
            stream=stream,
            timeout=timeout,
            verify=verify,
        )
    raise AssertionError("live_http_access_forbidden")


def _gateway_support_records(gateway):
    record = object.__getattribute__(gateway, "_record")
    configuration = record.configuration
    configuration_record = record.configuration_record
    return configuration, configuration_record


def _register_gateway_clock(gateway, clock):
    _configuration, configuration_record = _gateway_support_records(gateway)
    with _REGISTRY_LOCK:
        _REGISTERED_CLOCKS[id(configuration_record)] = (
            configuration_record,
            clock,
        )


def _unregister_configuration_clock(configuration):
    try:
        configuration_record = object.__getattribute__(
            configuration,
            "_record",
        )
    except (AttributeError, TypeError):
        return
    with _REGISTRY_LOCK:
        entry = _REGISTERED_CLOCKS.get(id(configuration_record))
        if entry is not None and entry[0] is configuration_record:
            _REGISTERED_CLOCKS.pop(id(configuration_record), None)


def _registered_clock(configuration):
    with _REGISTRY_LOCK:
        entry = _REGISTERED_CLOCKS.get(id(configuration))
    if entry is None or entry[0] is not configuration:
        return None
    return entry[1]


def _request_form(body):
    try:
        if type(body) is bytes:
            text = body.decode("ascii")
        elif type(body) is str:
            text = body
        else:
            return {}
        return parse_qs(
            text,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=16,
        )
    except (UnicodeError, ValueError):
        return {}


def _single(form, name):
    values = form.get(name)
    if type(values) is list and len(values) == 1:
        return values[0]
    return None


def _pkce_challenge(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _json_response(request, document, *, status=200):
    body = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _make_response(
        request,
        body=body,
        status=status,
        content_type="application/json",
    )


def _planned_response(plan, request):
    if plan.exception is not None:
        raise plan.exception
    return _make_response(
        request,
        body=plan.body,
        status=plan.status,
        content_type=plan.content_type,
        url=plan.url,
        declared_length=plan.declared_length,
        location=plan.location,
        history=plan.history,
        read_exception=plan.read_exception,
        read_failure_after=plan.read_failure_after,
        headers=plan.headers,
    )


def _make_response(
    request,
    *,
    body,
    status,
    content_type,
    url=None,
    declared_length=None,
    location=None,
    history=False,
    read_exception=None,
    read_failure_after=0,
    headers=None,
):
    response_headers = dict(headers or ())
    if content_type is not None:
        response_headers["Content-Type"] = content_type
    if declared_length is not None:
        response_headers["Content-Length"] = str(declared_length)
    if location is not None:
        response_headers["Location"] = location
    response = _TrackedResponse(
        body=body,
        headers=response_headers,
        read_exception=read_exception,
        read_failure_after=read_failure_after,
    )
    response.status_code = status
    response.url = url or request.url
    response.request = request
    response.encoding = "utf-8"
    if history:
        previous = Response()
        previous.status_code = 302
        previous.url = request.url
        previous.headers["Location"] = response.url
        response.history = [previous]
    return response


@dataclass(frozen=True, slots=True)
class GatewayHarness:
    gateway: _gateway.GoogleOidcGateway
    configuration: _gateway.TrustedGoogleOidcConfiguration
    clock: ManualClock
    fake_provider: object | None = None
    transport: InMemoryGoogleTransport | None = None

    def close(self):
        _unregister_configuration_clock(self.configuration)
        self.gateway.close()
        if self.transport is not None:
            self.transport.close()


@dataclass(frozen=True, slots=True)
class GatewayDatabase:
    path: Path
    connection: object
    created: object
    subject: str

    @property
    def account_id(self):
        return self.created.user.user_id

    @property
    def identity_id(self):
        return self.created.identity.auth_identity_id


def make_fake_gateway(
    *,
    clock=None,
    subject=DEFAULT_SUBJECT,
    authenticated_at=None,
    token_expires_at=None,
    outcomes=("success",),
    block=False,
    client_secret=None,
):
    return make_real_gateway(
        clock=clock,
        subject=subject,
        authenticated_at=authenticated_at,
        token_expires_at=token_expires_at,
        outcomes=outcomes,
        block=block,
        client_secret=client_secret,
        expose_transport_as_fake_provider=True,
    )


def make_real_gateway(
    *,
    clock=None,
    client_id=CLIENT_ID,
    client_secret=None,
    redirect_uri=REDIRECT_URI,
    subject=DEFAULT_SUBJECT,
    authenticated_at=None,
    token_expires_at=None,
    outcomes=("success",),
    block=False,
    expose_transport_as_fake_provider=False,
    invitation_lookup_key=None,
    configure_account_native_bootstrap=True,
):
    clock = clock or ManualClock()
    gateway = None
    secret = client_secret
    if secret is None:
        secret = bytearray(CLIENT_SECRET)
    if type(secret) is not bytearray:
        raise TypeError("mutable_client_secret_required")
    transport = InMemoryGoogleTransport(
        clock=clock,
        client_id=client_id,
        redirect_uri=redirect_uri,
        client_secret=secret,
        subject=subject,
        authenticated_at=authenticated_at,
        token_expires_at=token_expires_at,
        outcomes=outcomes,
        block=block,
    )
    try:
        gateway = _gateway.GoogleOidcGateway(
            client_id=client_id,
            client_secret=secret,
            redirect_uri=redirect_uri,
            environment_namespace="test",
        )
        if invitation_lookup_key is not None:
            _gateway._configure_invitation_provisioning(
                gateway,
                invitation_lookup_key,
            )
        if configure_account_native_bootstrap:
            _gateway._configure_account_native_bootstrap(
                gateway,
                ensure_account_native_principal,
            )
    except BaseException:
        if gateway is not None:
            gateway.close()
        transport.close()
        raise
    configuration, _configuration_record = _gateway_support_records(gateway)
    _register_gateway_clock(gateway, clock)
    return GatewayHarness(
        gateway=gateway,
        configuration=configuration,
        clock=clock,
        fake_provider=(
            transport if expose_transport_as_fake_provider else None
        ),
        transport=transport,
    )


def seed_existing_google_identity(
    connection,
    *,
    suffix="google-oidc",
    created_at=IDENTITY_CREATED_AT,
):
    _invitation, created = create_user(
        connection,
        suffix=suffix,
        now=created_at,
    )
    return created


@contextlib.contextmanager
def gateway_database(
    *,
    suffix="google-oidc",
    created_at=IDENTITY_CREATED_AT,
):
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"{suffix}.sqlite"
        connection = install_accounts(path)
        try:
            created = seed_existing_google_identity(
                connection,
                suffix=suffix,
                created_at=created_at,
            )
            yield GatewayDatabase(
                path=path,
                connection=connection,
                created=created,
                subject=f"google-subject-{suffix}",
            )
        finally:
            connection.close()


def durable_counts(connection):
    names = (
        "users",
        "auth_identities",
        "account_sessions",
        "persistent_profiles",
        "profile_ownership_bindings",
    )
    counts = {}
    for name in names:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        if row is not None:
            counts[name] = connection.execute(
                f'SELECT COUNT(*) FROM "{name}"'
            ).fetchone()[0]
    return counts


@contextlib.contextmanager
def sockets_blocked():
    def deny(*_args, **_kwargs):
        raise AssertionError("live_socket_access_forbidden")

    original_clock_now = _gateway._clock_now
    original_monotonic_now = _gateway._monotonic_now

    def clock_now(configuration):
        clock = _registered_clock(configuration)
        if clock is None:
            return original_clock_now(configuration)
        return clock()

    def monotonic_now(configuration):
        clock = _registered_clock(configuration)
        if clock is None:
            return original_monotonic_now(configuration)
        return clock.monotonic()

    with (
        mock.patch.object(socket, "socket", deny),
        mock.patch.object(socket, "create_connection", deny),
        mock.patch.object(socket, "getaddrinfo", deny),
        mock.patch.object(
            HTTPAdapter,
            "send",
            new=_route_http_adapter_send,
        ),
        mock.patch.object(_gateway, "_clock_now", new=clock_now),
        mock.patch.object(
            _gateway,
            "_monotonic_now",
            new=monotonic_now,
        ),
    ):
        try:
            yield
        finally:
            _ROUTER_LOCAL.transport = None


def assert_rejects_copy_pickle(value):
    import pickle

    for operation in (
        lambda: copy.copy(value),
        lambda: copy.deepcopy(value),
        lambda: pickle.dumps(value),
    ):
        try:
            operation()
        except (TypeError, AttributeError):
            continue
        raise AssertionError("sealed_value_was_copyable_or_serializable")


__all__ = (
    "CLIENT_ID",
    "CLIENT_SECRET",
    "DEFAULT_CODE",
    "DEFAULT_SUBJECT",
    "GatewayDatabase",
    "GatewayHarness",
    "IDENTITY_CREATED_AT",
    "InMemoryGoogleTransport",
    "ManualClock",
    "NOW",
    "PRIMARY_SIGNING_FIXTURE",
    "REDIRECT_URI",
    "ROTATED_SIGNING_FIXTURE",
    "RequestObservation",
    "SigningFixture",
    "assert_rejects_copy_pickle",
    "authorization_parameters",
    "close_secret_vault",
    "completion_policy",
    "durable_counts",
    "gateway_database",
    "jwks_document",
    "make_fake_gateway",
    "make_real_gateway",
    "request_secret_vault",
    "seed_existing_google_identity",
    "signed_id_token",
    "sockets_blocked",
    "valid_id_token_claims",
    "vault_entry_count",
)
