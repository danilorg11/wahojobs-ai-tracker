"""Dormant, auth-gated read-only persistent-profile application boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import sqlite3

from wahojobs.persistent_profiles import (
    PersistentProfileDomainError,
    TrustedPrincipalContext,
)
from wahojobs.persistent_profiles_repository import (
    read_current_profile,
    read_profile_history,
)


PROFILE_HISTORY_PAGE_SIZE = 20
MAX_BROWSER_CURSOR = 2_147_483_647

_ACTOR_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PAGE_STATES = frozenset(
    {
        "authentication_required",
        "authorization_denied",
        "empty",
        "active",
        "archived",
        "deletion_requested",
        "temporary_contention",
        "schema_unavailable",
        "unavailable",
    }
)
_PROFILE_STATES = frozenset({"active", "archived", "deletion_requested"})


def _configuration_error() -> ValueError:
    return ValueError("invalid_persistent_profile_application_configuration")


class BrowserRequestContext:
    """Bounded request facts made available to a trusted auth gateway."""

    __slots__ = ("method", "route", "_authentication_input", "_sealed")

    def __init__(self, method: str, route: str, authentication_input=None):
        if method not in {"GET", "HEAD"} or route != "/account/profile":
            raise _configuration_error()
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "route", route)
        object.__setattr__(self, "_authentication_input", authentication_input)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, _name, _value):
        raise AttributeError("browser_request_context_is_immutable")

    def authentication_input_for_gateway(self):
        return self._authentication_input

    def __repr__(self) -> str:
        return (
            "BrowserRequestContext("
            f"method={self.method!r}, route='/account/profile', authentication_input=<redacted>)"
        )

    def __reduce_ex__(self, _protocol):
        raise TypeError("browser_request_context_not_serializable")

    def __copy__(self):
        raise TypeError("browser_request_context_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("browser_request_context_not_copyable")


class TrustedAuthenticatedBrowserActor:
    """Opaque authentication outcome supplied by trusted composition code."""

    __slots__ = ("_actor_key", "_sealed")

    def __init__(self, actor_key: str):
        if type(actor_key) is not str or _ACTOR_KEY.fullmatch(actor_key) is None:
            raise _configuration_error()
        object.__setattr__(self, "_actor_key", actor_key)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, _name, _value):
        raise AttributeError("trusted_context_is_immutable")

    def __repr__(self) -> str:
        return "TrustedAuthenticatedBrowserActor(<redacted>)"

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_context_not_serializable")

    def __copy__(self):
        raise TypeError("trusted_context_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("trusted_context_not_copyable")


class TrustedProfileReadGrant:
    """Opaque authorization result selecting one trusted durable principal."""

    __slots__ = ("_principal", "_sealed")

    def __init__(self, principal: TrustedPrincipalContext):
        if type(principal) is not TrustedPrincipalContext:
            raise _configuration_error()
        object.__setattr__(self, "_principal", principal)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, _name, _value):
        raise AttributeError("trusted_context_is_immutable")

    def principal_for_repository(self) -> TrustedPrincipalContext:
        return self._principal

    def __repr__(self) -> str:
        return "TrustedProfileReadGrant(<redacted>)"

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_context_not_serializable")

    def __copy__(self):
        raise TypeError("trusted_context_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("trusted_context_not_copyable")


@dataclass(frozen=True, slots=True, repr=False)
class PersistentProfileFieldGroup:
    label: str
    values: tuple[str, ...]

    def __post_init__(self):
        if type(self.label) is not str or not 1 <= len(self.label) <= 64:
            raise _configuration_error()
        if type(self.values) is not tuple or not 1 <= len(self.values) <= 32:
            raise _configuration_error()
        if any(type(value) is not str or not 1 <= len(value) <= 4_096 for value in self.values):
            raise _configuration_error()

    def __repr__(self) -> str:
        return f"PersistentProfileFieldGroup(label={self.label!r}, values=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class PersistentProfileView:
    display_name: str
    lifecycle_status: str
    revision_number: int
    updated_at: str
    field_groups: tuple[PersistentProfileFieldGroup, ...]
    structured_content_visible: bool

    def __post_init__(self):
        if type(self.display_name) is not str or len(self.display_name) > 160:
            raise _configuration_error()
        if self.lifecycle_status not in _PROFILE_STATES:
            raise _configuration_error()
        if type(self.revision_number) is not int or self.revision_number < 1:
            raise _configuration_error()
        if type(self.updated_at) is not str or not self.updated_at:
            raise _configuration_error()
        if type(self.field_groups) is not tuple or len(self.field_groups) > 8:
            raise _configuration_error()
        if any(type(group) is not PersistentProfileFieldGroup for group in self.field_groups):
            raise _configuration_error()
        if type(self.structured_content_visible) is not bool:
            raise _configuration_error()
        if self.lifecycle_status == "deletion_requested" and (
            self.structured_content_visible or self.field_groups or self.display_name
        ):
            raise _configuration_error()

    def __repr__(self) -> str:
        return (
            "PersistentProfileView("
            f"lifecycle_status={self.lifecycle_status!r}, "
            f"revision_number={self.revision_number}, content=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class PersistentProfileHistoryView:
    revision_number: int
    revision_kind: str
    lifecycle_status: str
    accepted_at: str

    def __post_init__(self):
        if type(self.revision_number) is not int or self.revision_number < 1:
            raise _configuration_error()
        if type(self.revision_kind) is not str or not self.revision_kind:
            raise _configuration_error()
        if self.lifecycle_status not in _PROFILE_STATES:
            raise _configuration_error()
        if type(self.accepted_at) is not str or not self.accepted_at:
            raise _configuration_error()


@dataclass(frozen=True, slots=True, repr=False)
class PersistentProfilePageResult:
    state: str
    profile: PersistentProfileView | None = None
    history: tuple[PersistentProfileHistoryView, ...] = ()
    next_cursor: int | None = None

    def __post_init__(self):
        if self.state not in _PAGE_STATES:
            raise _configuration_error()
        if self.state in _PROFILE_STATES:
            if type(self.profile) is not PersistentProfileView:
                raise _configuration_error()
        elif self.profile is not None:
            raise _configuration_error()
        if type(self.history) is not tuple or len(self.history) > PROFILE_HISTORY_PAGE_SIZE:
            raise _configuration_error()
        if any(type(item) is not PersistentProfileHistoryView for item in self.history):
            raise _configuration_error()
        if self.state not in _PROFILE_STATES and (self.history or self.next_cursor is not None):
            raise _configuration_error()
        if self.next_cursor is not None and (
            type(self.next_cursor) is not int
            or not 1 <= self.next_cursor <= MAX_BROWSER_CURSOR
        ):
            raise _configuration_error()

    def __repr__(self) -> str:
        return (
            "PersistentProfilePageResult("
            f"state={self.state!r}, history_count={len(self.history)}, content=<redacted>)"
        )


class PersistentProfileApplicationService:
    """Authenticate, authorize, and orchestrate B2B2 read contracts only."""

    __slots__ = ("_authenticate", "_authorize", "_connection_provider")

    def __init__(self, *, authenticate, authorize, connection_provider):
        if not callable(authenticate) or not callable(authorize) or not callable(connection_provider):
            raise _configuration_error()
        self._authenticate = authenticate
        self._authorize = authorize
        self._connection_provider = connection_provider

    def read_my_profile(
        self,
        request_context: BrowserRequestContext,
        *,
        before_revision_number: int | None = None,
    ) -> PersistentProfilePageResult:
        if type(request_context) is not BrowserRequestContext:
            return _unavailable()
        if before_revision_number is not None and (
            type(before_revision_number) is not int
            or not 1 <= before_revision_number <= MAX_BROWSER_CURSOR
        ):
            return _unavailable()

        actor_failed = False
        actor = None
        try:
            actor = self._authenticate(request_context)
        except Exception:
            actor_failed = True
        if actor_failed:
            return _unavailable()
        if actor is None:
            return PersistentProfilePageResult("authentication_required")
        if type(actor) is not TrustedAuthenticatedBrowserActor:
            return _unavailable()

        grant_failed = False
        grant = None
        try:
            grant = self._authorize(actor)
        except Exception:
            grant_failed = True
        if grant_failed:
            return _unavailable()
        if grant is None:
            return PersistentProfilePageResult("authorization_denied")
        if type(grant) is not TrustedProfileReadGrant:
            return _unavailable()

        principal = grant.principal_for_repository()
        result = None
        connection = None
        owned_transaction = False
        cleanup_failed = False
        try:
            with self._connection_provider() as connection:
                if not isinstance(connection, sqlite3.Connection):
                    return _unavailable()
                if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                    return PersistentProfilePageResult("schema_unavailable")
                if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                    return PersistentProfilePageResult("schema_unavailable")
                if connection.in_transaction:
                    return _unavailable()
                connection.execute("BEGIN")
                owned_transaction = connection.in_transaction
                try:
                    result = self._read_authorized_profile(
                        connection,
                        principal,
                        before_revision_number=before_revision_number,
                    )
                except PersistentProfileDomainError as exc:
                    reason = exc.reason_code
                    exc = None
                    result = _result_for_domain_reason(reason)
                except sqlite3.Error as exc:
                    result = _result_for_sqlite_error(
                        getattr(exc, "sqlite_errorcode", None)
                    )
                    exc = None
                except Exception:
                    result = _unavailable()
                finally:
                    if owned_transaction:
                        try:
                            connection.rollback()
                        except Exception:
                            cleanup_failed = True
                        if connection.in_transaction:
                            cleanup_failed = True
        except sqlite3.Error as exc:
            result = _result_for_sqlite_error(getattr(exc, "sqlite_errorcode", None))
            exc = None
        except Exception:
            result = _unavailable()
        finally:
            connection = None
            principal = None
            grant = None
            actor = None
        if cleanup_failed or result is None:
            return _unavailable()
        return result

    @staticmethod
    def _read_authorized_profile(
        connection,
        principal,
        *,
        before_revision_number,
    ) -> PersistentProfilePageResult:
        summary = read_current_profile(
            connection,
            principal,
            include_structured_profile=False,
        )
        lifecycle = summary.lifecycle_status
        structured_profile = None
        if lifecycle != "deletion_requested":
            structured_summary = read_current_profile(
                connection,
                principal,
                include_structured_profile=True,
            )
            structured_profile = structured_summary.trusted_dict(
                include_structured_profile=True
            )["structured_profile"]
        profile = _build_profile_view(summary, structured_profile)
        structured_profile = None

        history_items = read_profile_history(
            connection,
            principal,
            page_size=PROFILE_HISTORY_PAGE_SIZE,
            before_revision_number=before_revision_number,
            include_structured_profile=False,
        )
        history = tuple(
            PersistentProfileHistoryView(
                revision_number=item.revision_number,
                revision_kind=item.revision_kind,
                lifecycle_status=item.lifecycle_status,
                accepted_at=item.created_at,
            )
            for item in history_items
        )
        next_cursor = history[-1].revision_number if len(history) == PROFILE_HISTORY_PAGE_SIZE else None
        return PersistentProfilePageResult(
            lifecycle,
            profile=profile,
            history=history,
            next_cursor=next_cursor,
        )


def _build_profile_view(summary, profile: dict | None) -> PersistentProfileView:
    if summary.lifecycle_status == "deletion_requested":
        return PersistentProfileView(
            display_name="",
            lifecycle_status=summary.lifecycle_status,
            revision_number=summary.revision_number,
            updated_at=summary.updated_at,
            field_groups=(),
            structured_content_visible=False,
        )
    if type(profile) is not dict:
        raise _configuration_error()

    groups = []
    languages = tuple(
        _language_label(item) for item in profile.get("languages", ()) if type(item) is dict
    )
    _append_group(groups, "Languages", languages)

    experience = profile.get("experience", {})
    domains = tuple(experience.get("professional_domains", ())) + tuple(
        experience.get("specialties", ())
    )
    _append_group(groups, "Professional domains", domains)
    experience_values = []
    if experience.get("seniority") not in {None, "", "unknown"}:
        experience_values.append(f"Seniority: {experience['seniority']}")
    if type(experience.get("total_years")) in {int, float}:
        experience_values.append(f"Total experience: {experience['total_years']} years")
    experience_values.extend(experience.get("recent_roles", ()))
    _append_group(groups, "Experience", tuple(experience_values))

    education = profile.get("education", {})
    education_values = []
    if education.get("education_level") not in {None, "", "unknown"}:
        education_values.append(f"Level: {education['education_level']}")
    education_values.extend(education.get("degrees", ()))
    education_values.extend(education.get("fields_or_domains", ()))
    _append_group(groups, "Education", tuple(education_values))

    credentials = profile.get("credentials", {})
    credential_values = tuple(credentials.get("certifications", ())) + tuple(
        credentials.get("licenses", ())
    )
    status = credentials.get("credential_status")
    if status not in {None, "", "unknown"}:
        credential_values += (f"Credential status: {status}",)
    _append_group(groups, "Credentials", credential_values)

    location = profile.get("location", {})
    location_values = tuple(
        value
        for value in (
            location.get("city"),
            location.get("region"),
            location.get("country"),
        )
        if value
    )
    if location.get("remote_eligibility") not in {None, "", "unknown"}:
        location_values += (f"Remote eligibility: {location['remote_eligibility']}",)
    if location.get("work_authorization") not in {None, "", "unknown"}:
        location_values += (f"Work authorization: {location['work_authorization']}",)
    _append_group(groups, "Location and eligibility", location_values)

    skills = profile.get("skills", {})
    _append_group(groups, "Skills", tuple(skills.get("normalized", ()))[:32])

    preferences = profile.get("preferences", {})
    preference_values = tuple(preferences.get("target_opportunity_types", ())) + tuple(
        preferences.get("work_preferences", ())
    )
    _append_group(groups, "Work preferences", preference_values[:32])

    display_name = profile.get("identity", {}).get("display_name", "")
    return PersistentProfileView(
        display_name=display_name,
        lifecycle_status=summary.lifecycle_status,
        revision_number=summary.revision_number,
        updated_at=summary.updated_at,
        field_groups=tuple(groups[:8]),
        structured_content_visible=True,
    )


def _language_label(item: dict) -> str:
    language = item.get("language", "")
    locale = item.get("locale", "")
    proficiency = item.get("proficiency", "")
    label = language + (f" ({locale})" if locale else "")
    return label + (f" - {proficiency}" if proficiency else "")


def _append_group(groups, label: str, values) -> None:
    cleaned = tuple(value for value in values if type(value) is str and value)
    if cleaned:
        groups.append(PersistentProfileFieldGroup(label, cleaned[:32]))


def _result_for_domain_reason(reason: str) -> PersistentProfilePageResult:
    if reason == "profile_not_found":
        return PersistentProfilePageResult("empty")
    if reason == "temporary_contention":
        return PersistentProfilePageResult("temporary_contention")
    if reason == "schema_capability_unavailable":
        return PersistentProfilePageResult("schema_unavailable")
    return _unavailable()


def _result_for_sqlite_error(error_code) -> PersistentProfilePageResult:
    if type(error_code) is int and (error_code & 0xFF) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }:
        return PersistentProfilePageResult("temporary_contention")
    return _unavailable()


def _unavailable() -> PersistentProfilePageResult:
    return PersistentProfilePageResult("unavailable")
