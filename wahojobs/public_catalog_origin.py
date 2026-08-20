"""Production-grade, guest-only origin boundary for the public jobs catalog.

This module is deliberately independent from the staging and WorkOS launchers.
It exposes only ``/jobs`` plus loopback-only health targets, owns no login or
job-detail route, and consumes a read-only public projection database.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from http import HTTPStatus
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import urlsplit

from wahojobs.authenticated_profile_matches import (
    AuthenticatedProfileMatchesBrowserIntegration,
    AuthenticatedProfileMatchesService,
)
from wahojobs.matching.metadata_overlay import OpportunityMetadataOverlay


CONFIGURATION_VERSION = 1
PUBLIC_CATALOG_ROUTE = "/jobs"
LIVE_ROUTE = "/__origin/live"
READY_ROUTE = "/__origin/ready"
METRICS_ROUTE = "/__origin/metrics"
HEALTH_ROUTES = frozenset({LIVE_ROUTE, READY_ROUTE, METRICS_ROUTE})
ORIGIN_AUTH_ENVIRONMENT_VARIABLE = "WAHOJOBS_ORIGIN_AUTH_TOKEN"
MAX_CONFIGURATION_BYTES = 16_384
MAX_DATABASE_BYTES = 256 * 1024 * 1024

_ROOT = Path(__file__).resolve().parents[1]
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_DATABASE_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{16,96}$")
_AUTH_HEADER = "x-wahojobs-origin-auth"
_REQUEST_ID_HEADER = "x-wahojobs-origin-request-id"
_PROXY_HEADER_NAMES = frozenset(
    {
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-prefix",
        "x-forwarded-proto",
    }
)

# Only these tables may contain rows in the public projection.  Source bodies,
# account/session/profile state, WorkOS state, and M009 public-route identities
# are intentionally absent or empty.
PUBLIC_DATA_TABLES = frozenset(
    {
        "companies",
        "canonical_opportunities",
        "jobs",
        "crawl_runs",
        "opportunity_enrichments",
    }
)
EXPECTED_EMPTY_TABLES = frozenset(
    {
        "job_source_contents",
        "opportunity_enrichment_overrides",
        "opportunity_enrichment_runs",
        "opportunity_enrichment_run_diagnostics",
        "job_events",
        "user_profiles",
        "user_pipeline_items",
        "applicant_status_updates",
    }
)
EXPECTED_TABLES = PUBLIC_DATA_TABLES | EXPECTED_EMPTY_TABLES


class PublicCatalogOriginConfigurationError(Exception):
    """One sanitized failure for unavailable origin configuration."""

    def __init__(self, code="configuration_unavailable"):
        self.code = code
        super().__init__("The public catalog origin configuration is unavailable.")


@dataclass(frozen=True, slots=True, repr=False)
class PublicCatalogOriginConfiguration:
    deployment_environment: str
    bind_host: str
    bind_port: int
    public_origin: str
    public_authority: str
    database_path: Path
    database_sha256: str

    @property
    def bind_address(self):
        return self.bind_host, self.bind_port

    def __repr__(self):
        return (
            "PublicCatalogOriginConfiguration("
            f"environment={self.deployment_environment!r}, "
            f"bind={self.bind_host!r}:<redacted>, "
            f"public_origin={self.public_origin!r}, database=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class PublicProjectionAttestation:
    database_sha256: str
    company_count: int
    opportunity_count: int
    job_count: int
    enrichment_count: int


@dataclass(frozen=True, slots=True)
class PublicCatalogOriginResponse:
    status: int
    body: bytes
    headers: tuple[tuple[str, str], ...]


def load_public_catalog_origin_configuration(path_value):
    path = _absolute_external_file(path_value, "configuration_unavailable")
    try:
        payload = path.read_bytes()
        if not payload or len(payload) > MAX_CONFIGURATION_BYTES:
            raise ValueError
        document = json.loads(payload.decode("utf-8"))
        required = {
            "version",
            "deployment_environment",
            "bind_host",
            "bind_port",
            "public_origin",
            "database_path",
            "database_sha256",
        }
        if type(document) is not dict or set(document) != required:
            raise ValueError
        if document["version"] != CONFIGURATION_VERSION:
            raise ValueError
        environment = document["deployment_environment"]
        if environment not in {"preview", "production"}:
            raise ValueError
        bind_host = document["bind_host"]
        bind_port = document["bind_port"]
        if bind_host != "127.0.0.1" or type(bind_port) is not int or not 1024 <= bind_port <= 65535:
            raise ValueError
        origin, authority = _validated_public_origin(document["public_origin"])
        if environment == "preview" and authority in {"wahojobs.com", "www.wahojobs.com"}:
            raise ValueError
        database_path = _absolute_external_file(
            document["database_path"], "database_unavailable"
        )
        digest = document["database_sha256"]
        if type(digest) is not str or _DATABASE_DIGEST.fullmatch(digest) is None:
            raise ValueError
        return PublicCatalogOriginConfiguration(
            deployment_environment=environment,
            bind_host=bind_host,
            bind_port=bind_port,
            public_origin=origin,
            public_authority=authority,
            database_path=database_path,
            database_sha256=digest,
        )
    except PublicCatalogOriginConfigurationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise PublicCatalogOriginConfigurationError() from None


def load_origin_auth_token(environment=None):
    source = os.environ if environment is None else environment
    value = source.get(ORIGIN_AUTH_ENVIRONMENT_VARIABLE)
    if type(value) is not str or _TOKEN.fullmatch(value) is None:
        raise PublicCatalogOriginConfigurationError("origin_auth_unavailable")
    return value


def attest_public_projection(configuration):
    if type(configuration) is not PublicCatalogOriginConfiguration:
        raise PublicCatalogOriginConfigurationError("database_unavailable")
    path = configuration.database_path
    try:
        metadata = path.stat()
        if not path.is_file() or not 0 < metadata.st_size <= MAX_DATABASE_BYTES:
            raise ValueError
        actual_digest = _sha256_file(path)
        if not hmac.compare_digest(actual_digest, configuration.database_sha256):
            raise ValueError
        with _open_read_only(path) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            table_names = frozenset(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            )
            if integrity != "ok" or foreign_keys or table_names != EXPECTED_TABLES:
                raise ValueError
            counts = {
                table: int(
                    connection.execute(
                        f'SELECT COUNT(*) FROM "{table}"'
                    ).fetchone()[0]
                )
                for table in EXPECTED_TABLES
            }
            if any(counts[table] for table in EXPECTED_EMPTY_TABLES):
                raise ValueError
            if not counts["companies"] or not counts["canonical_opportunities"] or not counts["jobs"]:
                raise ValueError
        return PublicProjectionAttestation(
            database_sha256=actual_digest,
            company_count=counts["companies"],
            opportunity_count=counts["canonical_opportunities"],
            job_count=counts["jobs"],
            enrichment_count=counts["opportunity_enrichments"],
        )
    except PublicCatalogOriginConfigurationError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise PublicCatalogOriginConfigurationError("database_unavailable") from None


class PublicCatalogOriginIntegration:
    """Exact-path adapter around the existing public catalog renderer."""

    __slots__ = (
        "_configuration",
        "_delegate",
        "_origin_auth_token",
        "_attestation",
        "_closed",
        "_metrics",
        "_metrics_lock",
    )

    def __init__(self, configuration, *, origin_auth_token):
        if (
            type(configuration) is not PublicCatalogOriginConfiguration
            or type(origin_auth_token) is not str
            or _TOKEN.fullmatch(origin_auth_token) is None
        ):
            raise PublicCatalogOriginConfigurationError()
        attestation = attest_public_projection(configuration)
        provider = _ReadOnlyPublicProjectionProvider(configuration.database_path)
        service = object.__new__(AuthenticatedProfileMatchesService)
        delegate = AuthenticatedProfileMatchesBrowserIntegration(
            service,
            connection_provider=provider,
            metadata_overlay=OpportunityMetadataOverlay(
                path=configuration.database_path.with_suffix(".overlay-disabled.json"),
                records_by_key={},
            ),
            confirmed_profile_artifact_sink=lambda _artifact: None,
            completed_profile_confirmation_authenticator=lambda _artifact: None,
            public_origin=configuration.public_origin,
            now=lambda: datetime.now(timezone.utc),
        )
        self._configuration = configuration
        self._delegate = delegate
        self._origin_auth_token = origin_auth_token
        self._attestation = attestation
        self._closed = False
        self._metrics = {"jobs": 0, "health": 0, "rejected": 0}
        self._metrics_lock = threading.Lock()

    @property
    def attestation(self):
        return self._attestation

    def close(self):
        if self._closed:
            return True
        self._closed = True
        delegate = self._delegate
        self._delegate = None
        self._origin_auth_token = None
        return delegate.close() if delegate is not None else True

    def handle(self, method, target, headers, body_stream=None, *, loopback_peer=False):
        if self._closed:
            return _plain_response(HTTPStatus.SERVICE_UNAVAILABLE, b"Unavailable\n")
        items = _validated_header_items(headers)
        if items is None or not loopback_peer or not self._trusted_origin(items):
            return _plain_response(HTTPStatus.FORBIDDEN, b"Forbidden\n")
        parsed = _relative_target(target)
        if parsed is None:
            self._record("rejected")
            return _plain_response(HTTPStatus.BAD_REQUEST, b"Bad request\n")
        path = parsed.path
        if path in HEALTH_ROUTES:
            if parsed.query or method not in {"GET", "HEAD"}:
                self._record("rejected")
                return _plain_response(HTTPStatus.NOT_FOUND, b"Not found\n")
            self._record("health")
            if path == METRICS_ROUTE:
                with self._metrics_lock:
                    metrics = dict(self._metrics)
                payload = (
                    json.dumps(metrics, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode("ascii")
                return _plain_response(HTTPStatus.OK, payload, json_body=True)
            if path == READY_ROUTE:
                try:
                    current = attest_public_projection(self._configuration)
                except PublicCatalogOriginConfigurationError:
                    return _plain_response(
                        HTTPStatus.SERVICE_UNAVAILABLE, b'{"ready":false}\n', json_body=True
                    )
                if current != self._attestation:
                    return _plain_response(
                        HTTPStatus.SERVICE_UNAVAILABLE, b'{"ready":false}\n', json_body=True
                    )
            return _plain_response(HTTPStatus.OK, b'{"ready":true}\n', json_body=True)
        if path != PUBLIC_CATALOG_ROUTE:
            self._record("rejected")
            return _plain_response(HTTPStatus.NOT_FOUND, b"Not found\n")
        if method not in {"GET", "HEAD"}:
            self._record("rejected")
            return _plain_response(
                HTTPStatus.METHOD_NOT_ALLOWED,
                b"Method not allowed\n",
                extra_headers=(("Allow", "GET, HEAD"),),
            )
        if any(
            name.lower() in {"cookie", "authorization", "origin"}
            for name, _value in items
        ):
            self._record("rejected")
            return _plain_response(HTTPStatus.BAD_REQUEST, b"Bad request\n")
        self._record("jobs")
        canonical_headers = (("Host", self._configuration.public_authority),)
        response = self._delegate.handle(method, target, canonical_headers, body_stream)
        return PublicCatalogOriginResponse(
            status=response.status,
            body=response.body,
            headers=response.headers
            + (("X-Wahojobs-Origin", "public-catalog-preview"),),
        )

    def request_id(self, headers):
        items = _validated_header_items(headers)
        if items is None:
            return None
        values = tuple(
            value for name, value in items if name.lower() == _REQUEST_ID_HEADER
        )
        return values[0] if len(values) == 1 and _REQUEST_ID.fullmatch(values[0]) else None

    def _trusted_origin(self, items):
        values = tuple(value for name, value in items if name.lower() == _AUTH_HEADER)
        try:
            valid = len(values) == 1 and hmac.compare_digest(
                values[0].encode("ascii"), self._origin_auth_token.encode("ascii")
            )
        except (AttributeError, UnicodeError):
            valid = False
        return valid

    def _record(self, key):
        with self._metrics_lock:
            self._metrics[key] += 1


class _ReadOnlyPublicProjectionProvider:
    __slots__ = ("_path",)

    def __init__(self, path):
        self._path = path

    @contextmanager
    def __call__(self):
        with _open_read_only(self._path) as connection:
            yield connection


@contextmanager
def _open_read_only(path):
    connection = sqlite3.connect(
        path.as_uri() + "?mode=ro&immutable=1",
        uri=True,
        timeout=2.0,
        isolation_level="",
        check_same_thread=True,
        cached_statements=0,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        yield connection
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _plain_response(status, body, *, extra_headers=(), json_body=False):
    return PublicCatalogOriginResponse(
        status=int(status),
        body=body,
        headers=(
            (
                "Content-Type",
                "application/json; charset=utf-8"
                if json_body
                else "text/plain; charset=utf-8",
            ),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        )
        + tuple(extra_headers),
    )


def _validated_public_origin(value):
    if type(value) is not str or len(value) > 512:
        raise ValueError
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
    ):
        raise ValueError
    authority = parsed.netloc.lower()
    origin = "https://" + authority
    if value != origin:
        raise ValueError
    return origin, authority


def _absolute_external_file(value, code):
    if type(value) is not str or not value or "\x00" in value:
        raise PublicCatalogOriginConfigurationError(code)
    path = Path(value)
    try:
        if not path.is_absolute() or str(path) != value:
            raise OSError
        resolved = path.resolve(strict=True)
        if str(resolved) != value or not resolved.is_file():
            raise OSError
        try:
            resolved.relative_to(_ROOT)
        except ValueError:
            pass
        else:
            raise OSError
        return resolved
    except (OSError, RuntimeError, ValueError):
        raise PublicCatalogOriginConfigurationError(code) from None


def _relative_target(value):
    if type(value) is not str or not value or len(value.encode("utf-8")) > 4096:
        return None
    try:
        parsed = urlsplit(value)
    except (UnicodeError, ValueError):
        return None
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/"):
        return None
    return parsed


def _validated_header_items(headers):
    try:
        items = tuple(headers.items()) if hasattr(headers, "items") else tuple(headers)
    except (AttributeError, TypeError, ValueError):
        return None
    if len(items) > 64:
        return None
    result = []
    for item in items:
        if type(item) is not tuple or len(item) != 2:
            return None
        name, value = item
        if type(name) is not str or type(value) is not str or not name or "\r" in value or "\n" in value:
            return None
        result.append((name, value))
    return tuple(result)


def _sha256_file(path):
    digest = sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "CONFIGURATION_VERSION",
    "EXPECTED_EMPTY_TABLES",
    "EXPECTED_TABLES",
    "HEALTH_ROUTES",
    "LIVE_ROUTE",
    "METRICS_ROUTE",
    "ORIGIN_AUTH_ENVIRONMENT_VARIABLE",
    "PUBLIC_CATALOG_ROUTE",
    "PUBLIC_DATA_TABLES",
    "READY_ROUTE",
    "PublicCatalogOriginConfiguration",
    "PublicCatalogOriginConfigurationError",
    "PublicCatalogOriginIntegration",
    "PublicCatalogOriginResponse",
    "PublicProjectionAttestation",
    "attest_public_projection",
    "load_origin_auth_token",
    "load_public_catalog_origin_configuration",
]
