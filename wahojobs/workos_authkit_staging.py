"""Explicit local Staging activation for the accepted WorkOS AuthKit slice.

Importing this module reads no configuration, opens no database or socket, creates
no provider client, and starts no thread.  All activation requires one explicit
external configuration path.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import binascii
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import threading

from wahojobs.account_reconciliation import attest_account_schema
from wahojobs.closed_schema_authority import current_closed_schema_is_exact
from wahojobs.database_lifetime_ownership import (
    DatabaseLifetimeOwnershipError,
    ROLE_DURABLE_RUNTIME,
    ROLE_OFFLINE_OPERATOR,
    acquire_database_lifetime_ownership,
    release_database_lifetime_ownership,
    require_database_lifetime_ownership,
)
from wahojobs.workos_authkit import (
    CALLBACK_PATH,
    WorkOSAuthKitConfiguration,
    WorkOSAuthKitGateway,
    create_workos_sdk_boundary,
)
from wahojobs.workos_authkit_browser import WorkOSAuthKitBrowserIntegration
from wahojobs.workos_authkit_schema import attest_workos_authkit_schema


CONFIGURATION_VERSION = 1
STAGING_BIND_HOST = "127.0.0.1"
STAGING_BIND_PORT = 8443
STAGING_PUBLIC_ORIGIN = "https://127.0.0.1:8443"
STAGING_REDIRECT_URI = STAGING_PUBLIC_ORIGIN + CALLBACK_PATH
STAGING_ENVIRONMENT_NAMESPACE = "private_beta"
CONFIGURATION_MAX_BYTES = 16_384

_ROOT = Path(__file__).resolve().parents[1]
_PATH_TYPE = type(Path())
_WINDOWS_REPARSE_ATTRIBUTE = getattr(
    stat,
    "FILE_ATTRIBUTE_REPARSE_POINT",
    0x400,
)
_CLIENT_ID = re.compile(r"^client_[A-Za-z0-9]{8,192}$")
_BASE64 = re.compile(r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$")
_CONFIGURATION_FIELDS = frozenset(
    {
        "version",
        "environment_namespace",
        "database_path",
        "public_origin",
        "redirect_uri",
        "workos_client_id",
        "workos_api_key",
        "wahojobs_invitation_lookup_key_base64",
        "session_idle_ttl_seconds",
        "session_absolute_ttl_seconds",
    }
)
_ERROR_MESSAGES = {
    "configuration_file_unavailable": "The explicit Staging configuration file is unavailable.",
    "configuration_invalid": "The Staging configuration is invalid.",
    "secret_invalid": "The Staging secret material is invalid.",
    "database_unavailable": "The explicit Staging database is unavailable.",
    "database_m008_required": "The explicit Staging database must already have exact M008.",
    "database_ownership_unavailable": (
        "The explicit Staging database is already owned or unavailable."
    ),
    "provider_configuration_unavailable": "The WorkOS Staging client could not be configured.",
    "tls_unavailable": "The local HTTPS prerequisites are unavailable.",
    "listener_unavailable": "The local HTTPS listener is unavailable.",
    "runtime_unavailable": "The AuthKit Staging runtime could not be constructed.",
    "shutdown_incomplete": "The AuthKit Staging runtime did not shut down cleanly.",
    "migration_unavailable": "The explicit offline M008 operation failed.",
}


class WorkOSAuthKitStagingError(Exception):
    """One fixed, secret-free Staging activation error."""

    __slots__ = ("code",)

    def __init__(self, code):
        if code not in _ERROR_MESSAGES:
            code = "runtime_unavailable"
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])

    def __repr__(self):
        return f"WorkOSAuthKitStagingError(code={self.code!r})"


@dataclass(slots=True, repr=False)
class WorkOSAuthKitStagingConfiguration:
    """Validated explicit configuration with deliberately redacted repr."""

    environment_namespace: str
    database_path: Path
    public_origin: str
    redirect_uri: str
    workos_client_id: str
    workos_api_key: str | None
    invitation_lookup_key: bytearray | None
    session_idle_ttl: timedelta
    session_absolute_ttl: timedelta

    @property
    def bind_address(self):
        return (STAGING_BIND_HOST, STAGING_BIND_PORT)

    def clear_secrets(self):
        self.workos_api_key = None
        _clear_buffer(self.invitation_lookup_key)
        self.invitation_lookup_key = None

    def __repr__(self):
        return "WorkOSAuthKitStagingConfiguration(<redacted>)"


class _StagingDatabaseConnections:
    __slots__ = ("_path", "_ownership", "_lock", "_closed")

    def __init__(self, path, ownership):
        self._path = path
        self._ownership = ownership
        self._lock = threading.Lock()
        self._closed = False

    def require_available(self):
        with self._lock:
            if self._closed:
                raise WorkOSAuthKitStagingError("database_unavailable")
            path = self._path
            ownership = self._ownership
        try:
            return require_database_lifetime_ownership(
                ownership,
                role=ROLE_DURABLE_RUNTIME,
                database_path=path,
            )
        except DatabaseLifetimeOwnershipError:
            raise WorkOSAuthKitStagingError(
                "database_ownership_unavailable"
            ) from None

    def open_writable_connection(self):
        self.require_available()
        return _open_database_connection(self._path, mode="rw")

    @contextmanager
    def read_only_connection_provider(self):
        self.require_available()
        connection = _open_database_connection(self._path, mode="ro")
        try:
            yield connection
        finally:
            _close_connection(connection)

    @contextmanager
    def writable_connection_provider(self):
        self.require_available()
        connection = _open_database_connection(self._path, mode="rw")
        try:
            yield connection
        finally:
            _close_connection(connection)

    def close(self):
        with self._lock:
            if self._closed:
                return True
            self._closed = True
            self._path = None
            self._ownership = None
        return True

    def __repr__(self):
        with self._lock:
            state = "closed" if self._closed else "configured"
        return f"_StagingDatabaseConnections(<{state}>)"


class WorkOSAuthKitStagingRuntime:
    """Own the small AuthKit composition and its database lifetime lease."""

    __slots__ = (
        "browser_integration",
        "bind_address",
        "public_origin",
        "_database_path",
        "_ownership",
        "_connections",
        "_gateway",
        "_profile_integration",
        "_lock",
        "_closed",
    )

    def __init__(
        self,
        *,
        browser_integration,
        bind_address,
        public_origin,
        database_path,
        ownership,
        connections,
        gateway,
        profile_integration,
    ):
        self.browser_integration = browser_integration
        self.bind_address = bind_address
        self.public_origin = public_origin
        self._database_path = database_path
        self._ownership = ownership
        self._connections = connections
        self._gateway = gateway
        self._profile_integration = profile_integration
        self._lock = threading.Lock()
        self._closed = False

    def close(self):
        with self._lock:
            if self._closed:
                return True
            self._closed = True
            resources = (
                self.browser_integration,
                self._profile_integration,
                self._gateway,
                self._connections,
            )
            ownership = self._ownership
            database_path = self._database_path
            self.browser_integration = None
            self._profile_integration = None
            self._gateway = None
            self._connections = None
            self._ownership = None
            self._database_path = None
        failed = False
        for resource in resources:
            if resource is None:
                continue
            try:
                close = getattr(resource, "close", None)
                if callable(close) and close() is False:
                    failed = True
            except BaseException as exc:
                failed = True
                _detach_exception(exc)
        if ownership is not None:
            try:
                release_database_lifetime_ownership(
                    ownership,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=database_path,
                )
            except BaseException as exc:
                failed = True
                _detach_exception(exc)
        if failed:
            raise WorkOSAuthKitStagingError("shutdown_incomplete")
        return True

    def __repr__(self):
        with self._lock:
            state = "closed" if self._closed else "active"
        return f"WorkOSAuthKitStagingRuntime(<{state}>)"


def load_workos_authkit_staging_configuration(configuration_path):
    """Load one strict, permission-restricted external JSON document."""

    raw = None
    document = None
    invitation_key = None
    try:
        path = _validated_external_file(configuration_path, configuration=True)
        raw = _read_bounded_file(path, maximum=CONFIGURATION_MAX_BYTES)
        try:
            document = json.loads(
                raw.decode("utf-8", "strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, ValueError, TypeError):
            raise WorkOSAuthKitStagingError("configuration_invalid") from None
        if type(document) is not dict or frozenset(document) != _CONFIGURATION_FIELDS:
            raise WorkOSAuthKitStagingError("configuration_invalid")
        if any(type(name) is not str for name in document):
            raise WorkOSAuthKitStagingError("configuration_invalid")
        if (
            type(document["version"]) is not int
            or document["version"] != CONFIGURATION_VERSION
        ):
            raise WorkOSAuthKitStagingError("configuration_invalid")
        if document["environment_namespace"] != STAGING_ENVIRONMENT_NAMESPACE:
            raise WorkOSAuthKitStagingError("configuration_invalid")
        if document["public_origin"] != STAGING_PUBLIC_ORIGIN:
            raise WorkOSAuthKitStagingError("configuration_invalid")
        if document["redirect_uri"] != STAGING_REDIRECT_URI:
            raise WorkOSAuthKitStagingError("configuration_invalid")
        client_id = document["workos_client_id"]
        api_key = document["workos_api_key"]
        if type(client_id) is not str or _CLIENT_ID.fullmatch(client_id) is None:
            raise WorkOSAuthKitStagingError("configuration_invalid")
        if (
            type(api_key) is not str
            or not 16 <= len(api_key) <= 512
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in api_key)
        ):
            raise WorkOSAuthKitStagingError("secret_invalid")
        invitation_key = _decode_invitation_key(
            document["wahojobs_invitation_lookup_key_base64"]
        )
        idle = _validated_ttl(document["session_idle_ttl_seconds"], maximum_days=30)
        absolute = _validated_ttl(
            document["session_absolute_ttl_seconds"],
            maximum_days=90,
        )
        if idle > absolute:
            raise WorkOSAuthKitStagingError("configuration_invalid")
        database_path = _validated_external_file(
            document["database_path"],
            database=True,
        )
        configuration = WorkOSAuthKitStagingConfiguration(
            environment_namespace=STAGING_ENVIRONMENT_NAMESPACE,
            database_path=database_path,
            public_origin=STAGING_PUBLIC_ORIGIN,
            redirect_uri=STAGING_REDIRECT_URI,
            workos_client_id=client_id,
            workos_api_key=api_key,
            invitation_lookup_key=invitation_key,
            session_idle_ttl=idle,
            session_absolute_ttl=absolute,
        )
        invitation_key = None
        return configuration
    except WorkOSAuthKitStagingError:
        raise
    except (OSError, ValueError, TypeError):
        raise WorkOSAuthKitStagingError("configuration_file_unavailable") from None
    finally:
        _clear_buffer(raw)
        _clear_buffer(invitation_key)
        if type(document) is dict:
            for name in tuple(document):
                document[name] = None
            document.clear()
        configuration_path = None


def validate_workos_authkit_staging_database(connection):
    """Require one writable, exact, internally consistent M008 database."""

    if (
        type(connection) is not sqlite3.Connection
        or connection.in_transaction
        or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
        or connection.execute("PRAGMA query_only").fetchone()[0] != 0
    ):
        raise WorkOSAuthKitStagingError("database_unavailable")
    report = attest_workos_authkit_schema(connection)
    if report.get("state") != "correctly_installed":
        raise WorkOSAuthKitStagingError("database_m008_required")
    try:
        exact = current_closed_schema_is_exact(connection)
    except Exception:
        exact = False
    if not exact or not attest_account_schema(connection):
        raise WorkOSAuthKitStagingError("database_m008_required")
    integrity = connection.execute("PRAGMA quick_check(1)").fetchone()
    if integrity is None or tuple(integrity) != ("ok",):
        raise WorkOSAuthKitStagingError("database_unavailable")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise WorkOSAuthKitStagingError("database_unavailable")
    connection.execute("BEGIN IMMEDIATE")
    connection.rollback()
    if connection.in_transaction:
        raise WorkOSAuthKitStagingError("database_unavailable")
    return True


def build_workos_authkit_staging_runtime(
    configuration,
    *,
    sdk_boundary_factory=create_workos_sdk_boundary,
    clock=None,
):
    """Construct the existing AuthKit/profile/session composition explicitly."""

    if type(configuration) is not WorkOSAuthKitStagingConfiguration or not callable(
        sdk_boundary_factory
    ):
        raise WorkOSAuthKitStagingError("configuration_invalid")
    if clock is None:
        clock = lambda: datetime.now(timezone.utc)
    if not callable(clock):
        raise WorkOSAuthKitStagingError("configuration_invalid")

    ownership = None
    connections = None
    gateway = None
    profile_integration = None
    browser_integration = None
    completed = False
    try:
        _require_no_sqlite_sidecars(configuration.database_path)
        try:
            ownership = acquire_database_lifetime_ownership(
                configuration.database_path,
                role=ROLE_DURABLE_RUNTIME,
            )
        except DatabaseLifetimeOwnershipError:
            raise WorkOSAuthKitStagingError(
                "database_ownership_unavailable"
            ) from None
        connections = _StagingDatabaseConnections(
            configuration.database_path,
            ownership,
        )
        connection = connections.open_writable_connection()
        try:
            validate_workos_authkit_staging_database(connection)
        finally:
            _close_connection(connection)
        _require_no_sqlite_sidecars(configuration.database_path)

        try:
            boundary = sdk_boundary_factory(
                api_key=configuration.workos_api_key,
                client_id=configuration.workos_client_id,
            )
        except Exception as exc:
            _detach_exception(exc)
            raise WorkOSAuthKitStagingError(
                "provider_configuration_unavailable"
            ) from None
        authkit_configuration = WorkOSAuthKitConfiguration(
            client_id=configuration.workos_client_id,
            redirect_uri=configuration.redirect_uri,
            environment_namespace=configuration.environment_namespace,
        )
        gateway = WorkOSAuthKitGateway(
            boundary=boundary,
            configuration=authkit_configuration,
            invitation_lookup_key=bytes(configuration.invitation_lookup_key),
            clock=clock,
        )
        profile_integration = _build_profile_integration(
            connections,
            configuration,
            clock,
        )
        from wahojobs.trusted_login_completion import (
            create_workos_authkit_trusted_login_completion_policy,
        )

        completion_policy = create_workos_authkit_trusted_login_completion_policy(
            environment_namespace=configuration.environment_namespace,
            idle_ttl=configuration.session_idle_ttl,
            absolute_ttl=configuration.session_absolute_ttl,
        )
        browser_integration = WorkOSAuthKitBrowserIntegration(
            public_origin=configuration.public_origin,
            profile_integration=profile_integration,
            connection_factory=connections.open_writable_connection,
            gateway=gateway,
            completion_policy=completion_policy,
            clock=clock,
            process_guard=connections.require_available,
        )
        runtime = WorkOSAuthKitStagingRuntime(
            browser_integration=browser_integration,
            bind_address=configuration.bind_address,
            public_origin=configuration.public_origin,
            database_path=configuration.database_path,
            ownership=ownership,
            connections=connections,
            gateway=gateway,
            profile_integration=profile_integration,
        )
        completed = True
        ownership = None
        connections = None
        gateway = None
        profile_integration = None
        browser_integration = None
        return runtime
    except WorkOSAuthKitStagingError:
        raise
    except Exception as exc:
        _detach_exception(exc)
        raise WorkOSAuthKitStagingError("runtime_unavailable") from None
    finally:
        configuration.clear_secrets()
        if not completed:
            for resource in (
                browser_integration,
                profile_integration,
                gateway,
                connections,
            ):
                try:
                    close = getattr(resource, "close", None)
                    if callable(close):
                        close()
                except BaseException as exc:
                    _detach_exception(exc)
            if ownership is not None:
                try:
                    release_database_lifetime_ownership(
                        ownership,
                        role=ROLE_DURABLE_RUNTIME,
                        database_path=configuration.database_path,
                    )
                except BaseException as exc:
                    _detach_exception(exc)


def apply_m008_to_explicit_database(database_path):
    """Run the accepted M008 wrapper under explicit offline ownership."""

    path = _validated_external_file(database_path, database=True)
    ownership = None
    connection = None
    try:
        _require_no_sqlite_sidecars(path)
        ownership = acquire_database_lifetime_ownership(
            path,
            role=ROLE_OFFLINE_OPERATOR,
        )
        connection = _open_database_connection(path, mode="rw")
        from scripts.workos_authkit_provider_migration import (
            apply_workos_authkit_provider_migration,
        )

        result = apply_workos_authkit_provider_migration(connection)
        _require_no_sqlite_sidecars(path)
        return result
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except Exception as exc:
        _detach_exception(exc)
        raise WorkOSAuthKitStagingError("migration_unavailable") from None
    finally:
        _close_connection(connection)
        if ownership is not None:
            primary_active = sys.exc_info()[0] is not None
            try:
                release_database_lifetime_ownership(
                    ownership,
                    role=ROLE_OFFLINE_OPERATOR,
                    database_path=path,
                )
            except BaseException as exc:
                _detach_exception(exc)
                if not primary_active:
                    raise WorkOSAuthKitStagingError(
                        "migration_unavailable"
                    ) from None


def _build_profile_integration(connections, configuration, clock):
    from wahojobs.authenticated_profile_matches import (
        AuthenticatedProfileMatchesBrowserIntegration,
        AuthenticatedProfileMatchesService,
    )
    from wahojobs.browser_session_authentication import (
        DurableBrowserSessionAuthenticationGateway,
    )
    from wahojobs.matching.metadata_overlay import DEFAULT_OVERLAY_PATH, load_overlay
    from wahojobs.persistent_profile_corrections import (
        ConfirmedProfileCorrectionArtifactVault,
        PersistentProfileCorrectionService,
    )
    from wahojobs.persistent_profile_creation import (
        ConfirmedProfileArtifactVault,
        DurablePersistentProfileCreateAuthorizationGateway,
        PersistentProfileCreationService,
    )
    from wahojobs.persistent_profile_read_authorization import (
        DurablePersistentProfileReadAuthorizationGateway,
    )
    from wahojobs.persistent_profiles_application import (
        PersistentProfileApplicationService,
    )
    from wahojobs.persistent_profiles_browser import (
        PersistentProfileBrowserIntegration,
    )
    import secrets
    import time

    authentication_gateway = DurableBrowserSessionAuthenticationGateway(
        trusted_environment_namespace=configuration.environment_namespace,
        clock=clock,
    )
    authorization_gateway = DurablePersistentProfileReadAuthorizationGateway()
    creation_authorization_gateway = DurablePersistentProfileCreateAuthorizationGateway(
        authorization_gateway
    )
    profile_service = PersistentProfileApplicationService(
        durable_authentication_gateway=authentication_gateway,
        durable_authorization_gateway=authorization_gateway,
        connection_provider=connections.read_only_connection_provider,
    )
    creation_service = PersistentProfileCreationService(
        authentication_gateway=authentication_gateway,
        authorization_gateway=creation_authorization_gateway,
        read_connection_provider=connections.read_only_connection_provider,
        write_connection_provider=connections.writable_connection_provider,
        vault=ConfirmedProfileArtifactVault(
            monotonic=time.monotonic,
            token_factory=lambda: secrets.token_urlsafe(32),
        ),
        clock=clock,
        token_factory=lambda: secrets.token_urlsafe(32),
    )
    correction_service = PersistentProfileCorrectionService(
        authentication_gateway=authentication_gateway,
        authorization_gateway=authorization_gateway,
        read_connection_provider=connections.read_only_connection_provider,
        write_connection_provider=connections.writable_connection_provider,
        vault=ConfirmedProfileCorrectionArtifactVault(monotonic=time.monotonic),
        clock=clock,
        token_factory=lambda: secrets.token_urlsafe(32),
        binding_secret=secrets.token_bytes(32),
    )
    integration = PersistentProfileBrowserIntegration(
        profile_service,
        creation_service=creation_service,
        correction_service=correction_service,
        public_origin=configuration.public_origin,
    )
    matches_service = AuthenticatedProfileMatchesService(
        authentication_gateway=authentication_gateway,
        authorization_gateway=authorization_gateway,
        connection_provider=connections.read_only_connection_provider,
        clock=clock,
        binding_secret=secrets.token_bytes(32),
    )
    matches_integration = AuthenticatedProfileMatchesBrowserIntegration(
        matches_service,
        connection_provider=connections.read_only_connection_provider,
        metadata_overlay=load_overlay(path=DEFAULT_OVERLAY_PATH, required=False),
        confirmed_profile_artifact_sink=integration.issue_confirmed_artifact,
        completed_profile_confirmation_authenticator=(
            integration.authenticate_completed_profile_replay
        ),
        public_origin=configuration.public_origin,
        now=clock,
    )
    if integration.attach_matches_integration(matches_integration) is not True:
        raise WorkOSAuthKitStagingError("runtime_unavailable")
    if integration.activate() is not True:
        raise WorkOSAuthKitStagingError("runtime_unavailable")
    return integration


def _open_database_connection(path, *, mode):
    if type(path) is not _PATH_TYPE or mode not in {"ro", "rw"}:
        raise WorkOSAuthKitStagingError("database_unavailable")
    uri = path.as_uri() + "?mode=" + mode
    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=2.0,
            isolation_level="",
            check_same_thread=True,
            cached_statements=0,
        )
        connection.row_factory = sqlite3.Row
        connection.text_factory = str
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = " + ("ON" if mode == "ro" else "OFF"))
        if mode == "rw":
            connection.execute("PRAGMA recursive_triggers = ON")
        if (
            connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
            or connection.execute("PRAGMA query_only").fetchone()[0]
            != (1 if mode == "ro" else 0)
        ):
            raise WorkOSAuthKitStagingError("database_unavailable")
        return connection
    except WorkOSAuthKitStagingError:
        _close_connection(locals().get("connection"))
        raise
    except (OSError, sqlite3.Error, ValueError):
        _close_connection(locals().get("connection"))
        raise WorkOSAuthKitStagingError("database_unavailable") from None


def _close_connection(connection):
    if type(connection) is not sqlite3.Connection:
        return
    try:
        if connection.in_transaction:
            connection.rollback()
    except sqlite3.Error:
        pass
    try:
        connection.close()
    except sqlite3.Error:
        pass


def _validated_external_file(value, *, configuration=False, database=False):
    if type(value) is not str or not value or "\x00" in value:
        raise WorkOSAuthKitStagingError(
            "configuration_file_unavailable" if configuration else "database_unavailable"
        )
    path = Path(value)
    try:
        if not path.is_absolute() or str(path) != value:
            raise OSError
        _require_no_reparse_components(path)
        resolved = path.resolve(strict=True)
        if str(resolved) != value:
            raise OSError
        _require_no_reparse_components(resolved)
        if _inside_repository(resolved):
            raise OSError
        metadata = os.lstat(resolved)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (
                configuration
                and os.name != "nt"
                and metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            )
        ):
            raise OSError
        return resolved
    except (OSError, RuntimeError, ValueError):
        code = "configuration_file_unavailable" if configuration else "database_unavailable"
        raise WorkOSAuthKitStagingError(code) from None


def _require_no_reparse_components(path):
    current = path
    while True:
        metadata = os.lstat(current)
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & _WINDOWS_REPARSE_ATTRIBUTE:
            raise OSError
        parent = current.parent
        if parent == current:
            return
        current = parent


def _inside_repository(path):
    try:
        if os.path.commonpath(
            (os.path.normcase(path), os.path.normcase(_ROOT))
        ) == os.path.normcase(_ROOT):
            return True
    except ValueError:
        pass
    return any((parent / ".git").exists() for parent in (path.parent, *path.parents))


def _read_bounded_file(path, *, maximum):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    payload = bytearray()
    try:
        before = os.lstat(path)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError
        while True:
            chunk = os.read(descriptor, min(4096, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > maximum:
                raise OSError
        after = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino, opened.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
            or not payload
        ):
            raise OSError
        return payload
    except Exception:
        _clear_buffer(payload)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_configuration_field")
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise ValueError("invalid_json_constant")


def _decode_invitation_key(value):
    if type(value) is not str or not 44 <= len(value) <= 684 or _BASE64.fullmatch(value) is None:
        raise WorkOSAuthKitStagingError("secret_invalid")
    try:
        decoded = bytearray(base64.b64decode(value, validate=True))
    except (binascii.Error, ValueError):
        raise WorkOSAuthKitStagingError("secret_invalid") from None
    if not 32 <= len(decoded) <= 512 or base64.b64encode(decoded).decode("ascii") != value:
        _clear_buffer(decoded)
        raise WorkOSAuthKitStagingError("secret_invalid")
    return decoded


def _validated_ttl(value, *, maximum_days):
    if type(value) is not int or not 60 <= value <= maximum_days * 86_400:
        raise WorkOSAuthKitStagingError("configuration_invalid")
    return timedelta(seconds=value)


def _require_no_sqlite_sidecars(path):
    for suffix in ("-journal", "-wal", "-shm"):
        if Path(str(path) + suffix).exists():
            raise WorkOSAuthKitStagingError("database_unavailable")


def _clear_buffer(value):
    if type(value) is bytearray:
        for index in range(len(value)):
            value[index] = 0
        value.clear()


def _detach_exception(exc):
    try:
        exc.__traceback__ = None
        exc.__cause__ = None
        exc.__context__ = None
    except (AttributeError, TypeError):
        pass


__all__ = [
    "CONFIGURATION_VERSION",
    "STAGING_BIND_HOST",
    "STAGING_BIND_PORT",
    "STAGING_ENVIRONMENT_NAMESPACE",
    "STAGING_PUBLIC_ORIGIN",
    "STAGING_REDIRECT_URI",
    "WorkOSAuthKitStagingConfiguration",
    "WorkOSAuthKitStagingError",
    "WorkOSAuthKitStagingRuntime",
    "apply_m008_to_explicit_database",
    "build_workos_authkit_staging_runtime",
    "load_workos_authkit_staging_configuration",
    "validate_workos_authkit_staging_database",
]
