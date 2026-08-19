"""Staging public-SEO route policy and deterministic text/XML documents.

This module is deliberately configuration-free.  Callers inject an immutable
redirect/gone policy; the staging runtime uses the empty policy unless a test
supplies one explicitly.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import islice
from types import MappingProxyType
import re
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape


ROBOTS_ROUTE = "/robots.txt"
SITEMAP_INDEX_ROUTE = "/sitemap.xml"
STATIC_SITEMAP_ROUTE = "/sitemaps/static.xml"
JOBS_SITEMAP_ROUTE = "/sitemaps/jobs.xml"
COMPANIES_SITEMAP_ROUTE = "/sitemaps/companies.xml"
PUBLIC_SEO_DOCUMENT_ROUTES = frozenset(
    {
        ROBOTS_ROUTE,
        SITEMAP_INDEX_ROUTE,
        STATIC_SITEMAP_ROUTE,
        JOBS_SITEMAP_ROUTE,
        COMPANIES_SITEMAP_ROUTE,
    }
)

MAX_PUBLIC_SEO_POLICY_ENTRIES = 50_000
MAX_PUBLIC_SEO_PATH_BYTES = 2_048
MAX_SITEMAP_URLS = 50_000

_COMPANY_PATH = re.compile(r"^/company/[a-z0-9]+(?:-[a-z0-9]+)*$")
_JOB_PATH = re.compile(r"^/job/opportunity-[1-9][0-9]*$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class PublicSeoRouteDecision:
    kind: str
    location: str | None = None

    def __post_init__(self):
        if self.kind == "redirect":
            if not _valid_redirect_target(self.location):
                raise ValueError("invalid_public_seo_route_decision")
        elif self.kind == "gone":
            if self.location is not None:
                raise ValueError("invalid_public_seo_route_decision")
        else:
            raise ValueError("invalid_public_seo_route_decision")


class PublicSeoRoutePolicy:
    """Exact, bounded, immutable path directives supplied by composition."""

    __slots__ = ("_decisions", "_redirects", "_gone")

    def __init__(self, redirects: Mapping[str, str], gone: Iterable[str]):
        if not isinstance(redirects, Mapping) or isinstance(gone, (str, bytes)):
            raise ValueError("invalid_public_seo_route_policy")
        try:
            if len(redirects) > MAX_PUBLIC_SEO_POLICY_ENTRIES:
                raise ValueError("invalid_public_seo_route_policy")
            redirect_copy = dict(redirects)
            gone_values = tuple(
                islice(
                    gone,
                    MAX_PUBLIC_SEO_POLICY_ENTRIES - len(redirect_copy) + 1,
                )
            )
        except (TypeError, ValueError):
            raise ValueError("invalid_public_seo_route_policy") from None
        try:
            gone_set = set(gone_values)
        except TypeError:
            raise ValueError("invalid_public_seo_route_policy") from None
        if (
            len(gone_values) != len(gone_set)
            or len(redirect_copy) + len(gone_values) > MAX_PUBLIC_SEO_POLICY_ENTRIES
        ):
            raise ValueError("invalid_public_seo_route_policy")

        sources = set(redirect_copy)
        if sources & gone_set:
            raise ValueError("invalid_public_seo_route_policy")
        for source in (*redirect_copy.keys(), *gone_values):
            if not _valid_local_path(source):
                raise ValueError("invalid_public_seo_route_policy")
        for source, target in redirect_copy.items():
            if (
                not _valid_redirect_target(target)
                or source == target
                or target in sources
                or target in gone_set
            ):
                raise ValueError("invalid_public_seo_route_policy")

        decisions = {
            source: PublicSeoRouteDecision("redirect", target)
            for source, target in redirect_copy.items()
        }
        decisions.update(
            {source: PublicSeoRouteDecision("gone") for source in gone_values}
        )
        self._redirects = MappingProxyType(redirect_copy)
        self._gone = frozenset(gone_values)
        self._decisions = MappingProxyType(decisions)

    @classmethod
    def empty(cls) -> PublicSeoRoutePolicy:
        return cls({}, ())

    @property
    def redirects(self):
        return self._redirects

    @property
    def gone(self):
        return self._gone

    def owns_path(self, path):
        return type(path) is str and path in self._decisions

    def resolve_path(self, path: str) -> PublicSeoRouteDecision | None:
        if type(path) is not str:
            return None
        return self._decisions.get(path)


def render_robots(public_origin):
    origin = _validated_origin(public_origin)
    return (
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: {origin}{SITEMAP_INDEX_ROUTE}\n"
    )


def render_sitemap_index(public_origin):
    origin = _validated_origin(public_origin)
    locations = (
        origin + STATIC_SITEMAP_ROUTE,
        origin + JOBS_SITEMAP_ROUTE,
        origin + COMPANIES_SITEMAP_ROUTE,
    )
    entries = "".join(
        f"<sitemap><loc>{xml_escape(location)}</loc></sitemap>"
        for location in locations
    )
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
        f"{entries}</sitemapindex>"
    )


def render_urlset(public_origin, paths):
    origin = _validated_origin(public_origin)
    try:
        normalized = tuple(sorted(set(paths)))
    except TypeError:
        raise ValueError("invalid_public_sitemap_paths") from None
    if len(normalized) > MAX_SITEMAP_URLS:
        raise ValueError("invalid_public_sitemap_paths")
    for path in normalized:
        if not _valid_sitemap_path(path):
            raise ValueError("invalid_public_sitemap_paths")
    entries = "".join(
        f"<url><loc>{xml_escape(origin + path)}</loc></url>"
        for path in normalized
    )
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
        f"{entries}</urlset>"
    )


def _valid_redirect_target(value):
    return bool(
        _valid_local_path(value)
        and (
            value == "/jobs"
            or _COMPANY_PATH.fullmatch(value)
            or _valid_job_path(value)
        )
    )


def _valid_sitemap_path(value):
    return bool(
        _valid_local_path(value)
        and (
            value == "/jobs"
            or _COMPANY_PATH.fullmatch(value)
            or _valid_job_path(value)
        )
    )


def _valid_job_path(value):
    match = _JOB_PATH.fullmatch(value) if type(value) is str else None
    return bool(
        match is not None
        and int(value.rsplit("-", 1)[-1]) <= 9_223_372_036_854_775_807
    )


def _valid_local_path(value):
    if type(value) is not str:
        return False
    try:
        if not value or len(value.encode("utf-8")) > MAX_PUBLIC_SEO_PATH_BYTES:
            return False
        parsed = urlsplit(value)
    except (UnicodeError, ValueError):
        return False
    segments = value.split("/")
    return bool(
        value.startswith("/")
        and "//" not in value
        and "%" not in value
        and "\\" not in value
        and _CONTROL_CHARACTERS.search(value) is None
        and not parsed.scheme
        and not parsed.netloc
        and not parsed.query
        and not parsed.fragment
        and parsed.path == value
        and not any(segment in {".", ".."} for segment in segments)
    )


def _validated_origin(value):
    if type(value) is not str:
        raise ValueError("invalid_public_seo_origin")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError("invalid_public_seo_origin") from None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc != parsed.netloc.lower()
    ):
        raise ValueError("invalid_public_seo_origin")
    return value.rstrip("/")


__all__ = [
    "COMPANIES_SITEMAP_ROUTE",
    "JOBS_SITEMAP_ROUTE",
    "PUBLIC_SEO_DOCUMENT_ROUTES",
    "PublicSeoRouteDecision",
    "PublicSeoRoutePolicy",
    "ROBOTS_ROUTE",
    "SITEMAP_INDEX_ROUTE",
    "STATIC_SITEMAP_ROUTE",
    "render_robots",
    "render_sitemap_index",
    "render_urlset",
]
