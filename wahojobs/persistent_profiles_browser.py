"""Dormant protected browser surface for read-only persistent profiles."""

from __future__ import annotations

from dataclasses import dataclass, field
import html
from http import HTTPStatus
import re
from urllib.parse import parse_qs, urlsplit

from wahojobs.persistent_profiles_application import (
    BrowserRequestContext,
    MAX_BROWSER_CURSOR,
    PersistentProfileApplicationService,
    PersistentProfilePageResult,
)


PERSISTENT_PROFILE_ROUTE = "/account/profile"
MAX_PROFILE_BROWSER_RESPONSE_BYTES = 1_048_576
MAX_PROFILE_QUERY_BYTES = 256

_CURSOR = re.compile(r"^[1-9][0-9]{0,9}$")
_BIDI_CONTROLS = dict.fromkeys(
    (
        0x061C,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    )
)
_SECURITY_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'",
    ),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
    ("Cache-Control", "no-store"),
)


@dataclass(frozen=True, slots=True, repr=False)
class PersistentProfileBrowserResponse:
    status: int
    body: bytes = field(repr=False)
    headers: tuple[tuple[str, str], ...]

    def __post_init__(self):
        if type(self.status) is not int or not 100 <= self.status <= 599:
            raise ValueError("invalid_persistent_profile_browser_response")
        if type(self.body) is not bytes or len(self.body) > MAX_PROFILE_BROWSER_RESPONSE_BYTES:
            raise ValueError("invalid_persistent_profile_browser_response")
        if type(self.headers) is not tuple:
            raise ValueError("invalid_persistent_profile_browser_response")
        for item in self.headers:
            if (
                type(item) is not tuple
                or len(item) != 2
                or any(type(value) is not str or "\r" in value or "\n" in value for value in item)
            ):
                raise ValueError("invalid_persistent_profile_browser_response")

    def __repr__(self) -> str:
        return (
            "PersistentProfileBrowserResponse("
            f"status={self.status}, body=<redacted>, header_count={len(self.headers)})"
        )


class PersistentProfileBrowserIntegration:
    """Render one GET/HEAD route from an explicitly injected application service."""

    __slots__ = ("_service",)

    def __init__(self, service: PersistentProfileApplicationService):
        if type(service) is not PersistentProfileApplicationService:
            raise ValueError("invalid_persistent_profile_browser_configuration")
        self._service = service

    def matches_route(self, path: str) -> bool:
        return path == PERSISTENT_PROFILE_ROUTE

    def handle(
        self,
        method: str,
        target: str,
        authentication_input=None,
    ) -> PersistentProfileBrowserResponse:
        if method not in {"GET", "HEAD"}:
            return _response(
                HTTPStatus.METHOD_NOT_ALLOWED,
                _generic_page("Method not allowed", "This profile page is read-only."),
                extra_headers=(("Allow", "GET, HEAD"),),
            )
        cursor, request_valid = _parse_request_target(target)
        if not request_valid:
            return _response(
                HTTPStatus.BAD_REQUEST,
                _generic_page("Profile request unavailable", "This profile request is not valid."),
            )
        try:
            result = self._service.read_my_profile(
                BrowserRequestContext(
                    method,
                    PERSISTENT_PROFILE_ROUTE,
                    authentication_input,
                ),
                before_revision_number=cursor,
            )
            content, status = render_persistent_profile_page(result)
        except Exception:
            content = _generic_page(
                "Profile temporarily unavailable",
                "Your persistent profile could not be loaded safely.",
            )
            status = HTTPStatus.SERVICE_UNAVAILABLE
        payload = content.encode("utf-8")
        if len(payload) > MAX_PROFILE_BROWSER_RESPONSE_BYTES:
            return _response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                _generic_page(
                    "Profile temporarily unavailable",
                    "Your persistent profile could not be displayed safely.",
                ),
            )
        return _response(status, content)


def _parse_request_target(target: str) -> tuple[int | None, bool]:
    if type(target) is not str or len(target.encode("utf-8", errors="ignore")) > MAX_PROFILE_QUERY_BYTES:
        return None, False
    try:
        parsed = urlsplit(target)
        if parsed.path != PERSISTENT_PROFILE_ROUTE or parsed.fragment:
            return None, False
        params = parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=4,
        ) if parsed.query else {}
    except (UnicodeError, ValueError):
        return None, False
    if set(params) - {"before"}:
        return None, False
    if not params:
        return None, True
    values = params.get("before", ())
    if len(values) != 1 or _CURSOR.fullmatch(values[0]) is None:
        return None, False
    cursor = int(values[0])
    if cursor > MAX_BROWSER_CURSOR:
        return None, False
    return cursor, True


def render_persistent_profile_page(
    result: PersistentProfilePageResult,
) -> tuple[str, HTTPStatus]:
    if type(result) is not PersistentProfilePageResult:
        raise ValueError("invalid_persistent_profile_page_result")
    if result.state == "authentication_required":
        return (
            _page(
                "Authentication required",
                "<section class='empty'><h1>Authentication required</h1>"
                "<p>Sign in to open your persistent profile.</p>"
                "<p><a class='primary-link' href='/login'>Continue to sign in</a></p>"
                "</section>",
            ),
            HTTPStatus.UNAUTHORIZED,
        )
    if result.state == "authorization_denied":
        return (
            _generic_page("Profile not found", "This profile page is not available."),
            HTTPStatus.NOT_FOUND,
        )
    if result.state == "empty":
        return (
            _page(
                "My persistent profile",
                _authenticated_navigation()
                + "<section class='empty'><h1>No persistent profile yet</h1>"
                "<p>Persistent-profile creation is not enabled in this milestone.</p>"
                "<p>Your existing About You information has not been copied or persisted.</p></section>",
            ),
            HTTPStatus.OK,
        )
    if result.state == "temporary_contention":
        return (
            _generic_page(
                "Profile temporarily unavailable",
                "Your profile is busy. Please try again shortly.",
            ),
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if result.state == "schema_unavailable":
        return (
            _generic_page(
                "Profile temporarily unavailable",
                "The persistent-profile capability is not available.",
            ),
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if result.state == "unavailable":
        return (
            _generic_page(
                "Profile temporarily unavailable",
                "Your persistent profile could not be loaded safely.",
            ),
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    return _render_available(result), HTTPStatus.OK


def _render_available(result: PersistentProfilePageResult) -> str:
    profile = result.profile
    lifecycle_note = {
        "active": "This profile is active and shown read-only.",
        "archived": "This profile is archived and remains read-only.",
        "deletion_requested": (
            "Deletion has been requested. Profile content is hidden while that request is pending."
        ),
    }[result.state]
    title = _safe_text(profile.display_name) or "My persistent profile"
    groups = "".join(
        "<section class='profile-group'>"
        f"<h2>{_safe_text(group.label)}</h2>"
        "<ul>"
        + "".join(f"<li>{_safe_text(value)}</li>" for value in group.values)
        + "</ul></section>"
        for group in profile.field_groups
    )
    if not groups and result.state != "deletion_requested":
        groups = "<p class='muted'>No additional profile details are available.</p>"
    history = "".join(
        "<li class='history-item'>"
        f"<strong>Revision {item.revision_number}</strong>"
        f"<span>{_safe_text(_humanize(item.revision_kind))}</span>"
        f"<span>{_safe_text(_humanize(item.lifecycle_status))}</span>"
        f"<time>{_safe_text(item.accepted_at)}</time>"
        "</li>"
        for item in result.history
    ) or "<li class='muted'>No revision history is available.</li>"
    next_link = (
        f"<a class='next' href='{PERSISTENT_PROFILE_ROUTE}?before={result.next_cursor}'>"
        "Older revisions</a>"
        if result.next_cursor is not None
        else ""
    )
    body = f"""
    <header class='profile-header'>
      <p class='eyebrow'>Account profile</p>
      <h1>{title}</h1>
      <p>{_safe_text(lifecycle_note)}</p>
      <dl class='meta'>
        <div><dt>Status</dt><dd>{_safe_text(_humanize(profile.lifecycle_status))}</dd></div>
        <div><dt>Current revision</dt><dd>{profile.revision_number}</dd></div>
        <div><dt>Last accepted update</dt><dd>{_safe_text(profile.updated_at)}</dd></div>
      </dl>
    </header>
    <div class='profile-grid'>{groups}</div>
    <section class='history'>
      <h2>Revision history</h2>
      <p class='muted'>History shows metadata only. Stored profile content and sources are omitted.</p>
      <ol>{history}</ol>
      {next_link}
    </section>
    """
    return _page("My persistent profile", _authenticated_navigation() + body)


def _response(status, content: str, *, extra_headers=()) -> PersistentProfileBrowserResponse:
    payload = content.encode("utf-8")
    headers = (
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(payload))),
        *_SECURITY_HEADERS,
        *extra_headers,
    )
    return PersistentProfileBrowserResponse(int(status), payload, tuple(headers))


def _safe_text(value) -> str:
    if type(value) is not str:
        value = str(value)
    return html.escape(value.translate(_BIDI_CONTROLS), quote=True)


def _humanize(value: str) -> str:
    return value.replace("_", " ").strip().title()


def _generic_page(title: str, message: str) -> str:
    return _page(
        title,
        f"<section class='empty'><h1>{_safe_text(title)}</h1>"
        f"<p>{_safe_text(message)}</p></section>",
    )


def _authenticated_navigation() -> str:
    return (
        "<nav class='account-nav' aria-label='Account'>"
        f"<a href='{PERSISTENT_PROFILE_ROUTE}'>My profile</a>"
        "<a href='/logout'>Sign out</a>"
        "</nav>"
    )


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>{_safe_text(title)} | Wahojobs</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, system-ui, sans-serif; color: #202523; background: #f4f6f5; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; }}
    main {{ width: min(960px, calc(100% - 32px)); margin: 0 auto; padding: 36px 0 64px; }}
    .account-nav {{ display: flex; justify-content: flex-end; gap: 16px; margin-bottom: 16px; }}
    .account-nav a, .primary-link {{ color: #174d3b; font-weight: 700; }}
    .profile-header, .history, .empty {{ background: white; border: 1px solid #dce2df; border-radius: 8px; padding: 24px; }}
    .profile-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin: 16px 0; }}
    .profile-group {{ background: white; border: 1px solid #dce2df; border-radius: 8px; padding: 18px; }}
    h1, h2, p {{ margin-top: 0; }}
    h1 {{ font-size: 30px; margin-bottom: 10px; }}
    h2 {{ font-size: 18px; }}
    .eyebrow {{ color: #466257; font-weight: 700; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 20px; margin-bottom: 0; }}
    .meta div {{ min-width: 150px; }}
    dt {{ color: #66716c; font-size: 13px; }}
    dd {{ margin: 3px 0 0; font-weight: 650; }}
    ul, ol {{ padding-left: 20px; }}
    .history-item {{ display: grid; grid-template-columns: 1fr 1fr 1fr 2fr; gap: 10px; padding: 10px 0; border-bottom: 1px solid #e8ecea; }}
    .muted {{ color: #66716c; }}
    .next {{ display: inline-block; margin-top: 12px; color: #174d3b; font-weight: 700; }}
    @media (max-width: 650px) {{ .history-item {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body><main>{body}</main></body>
</html>"""
