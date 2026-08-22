"""Attest an offline Production Exact-Route Release v1 artifact.

This script opens the generated projection read-only, validates its signed release
manifest, and exercises the production origin integration in-process. It does not
contact or mutate any external system.
"""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
from urllib.parse import urljoin, urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.public_catalog_origin import (
    PublicCatalogOriginConfiguration,
    PublicCatalogOriginIntegration,
    attest_public_projection,
)
from wahojobs.public_job_release import (
    HANDSHAKE_CANARY_PUBLIC_JOB_PATH,
    KARL_PUBLIC_JOB_PATH,
    load_production_release_manifest,
)


OFFLINE_ATTESTATION_TOKEN = "T" * 43
PUBLIC_ORIGIN = "https://www.wahojobs.com"
PRODUCTION_OWNED_ROUTES = frozenset(
    {"/jobs", HANDSHAKE_CANARY_PUBLIC_JOB_PATH}
)


class _HrefCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs = []

    def handle_starttag(self, _tag, attributes):
        for name, value in attributes:
            if name.casefold() == "href" and value is not None:
                self.hrefs.append(value)


def _internal_href_paths(document: str) -> tuple[str, ...]:
    collector = _HrefCollector()
    collector.feed(document)
    base = PUBLIC_ORIGIN + "/jobs"
    authority = urlsplit(base).netloc.casefold()
    paths = []
    for href in collector.hrefs:
        resolved = urlsplit(urljoin(base, href))
        if resolved.netloc.casefold() == authority:
            paths.append(resolved.path)
    return tuple(paths)


def _assert_status(response, expected: int, label: str) -> None:
    actual = response.status
    if actual != expected:
        raise RuntimeError(f"{label} returned {actual!r}; expected {expected}")


def attest_production_artifact(
    *,
    database_path: Path,
    release_manifest_path: Path,
) -> dict[str, object]:
    database_path = database_path.resolve()
    release_manifest_path = release_manifest_path.resolve()
    release = load_production_release_manifest(release_manifest_path)

    configuration = PublicCatalogOriginConfiguration(
        deployment_environment="production",
        bind_host="127.0.0.1",
        bind_port=8080,
        public_origin=PUBLIC_ORIGIN,
        public_authority="www.wahojobs.com",
        database_path=database_path,
        database_sha256=release.database_sha256,
        release=release,
    )
    projection = attest_public_projection(configuration)
    integration = PublicCatalogOriginIntegration(
        configuration,
        origin_auth_token=OFFLINE_ATTESTATION_TOKEN,
    )
    authenticated_headers = (
        ("X-Wahojobs-Origin-Auth", OFFLINE_ATTESTATION_TOKEN),
        ("X-Wahojobs-Release-Id", release.release_id),
    )

    try:
        ready = integration.handle(
            "GET", "/__origin/ready", authenticated_headers, loopback_peer=True
        )
        jobs = integration.handle(
            "GET", "/jobs", authenticated_headers, loopback_peer=True
        )
        canary_search = integration.handle(
            "GET",
            "/jobs?q=AI+Evaluation+Specialist",
            authenticated_headers,
            loopback_peer=True,
        )
        canary = integration.handle(
            "GET",
            HANDSHAKE_CANARY_PUBLIC_JOB_PATH,
            authenticated_headers,
            loopback_peer=True,
        )
        karl = integration.handle(
            "GET", KARL_PUBLIC_JOB_PATH, authenticated_headers, loopback_peer=True
        )
        unauthenticated = integration.handle(
            "GET",
            "/__origin/ready",
            (("X-Wahojobs-Release-Id", release.release_id),),
            loopback_peer=True,
        )
        wrong_release = integration.handle(
            "GET",
            "/jobs",
            (
                ("X-Wahojobs-Origin-Auth", OFFLINE_ATTESTATION_TOKEN),
                ("X-Wahojobs-Release-Id", "0" * 64),
            ),
            loopback_peer=True,
        )
    finally:
        integration.close()

    _assert_status(ready, 200, "authenticated readiness probe")
    _assert_status(jobs, 200, "/jobs probe")
    _assert_status(canary_search, 200, "Handshake catalog-search probe")
    _assert_status(canary, 200, "Handshake canary probe")
    _assert_status(karl, 404, "Karl preservation probe")
    _assert_status(unauthenticated, 403, "unauthenticated readiness probe")
    _assert_status(wrong_release, 409, "release-mismatch readiness probe")

    for label, response in (
        ("/jobs", jobs),
        ("Handshake catalog search", canary_search),
        ("Handshake canary", canary),
    ):
        response_headers = dict(response.headers)
        if response_headers.get("Cache-Control") != "private, no-store, max-age=0":
            raise RuntimeError(f"{label} did not return the production no-store policy")
        if response_headers.get("CDN-Cache-Control") != "no-store":
            raise RuntimeError(f"{label} did not disable shared-cache storage")

    jobs_body = jobs.body.decode("utf-8")
    search_body = canary_search.body.decode("utf-8")
    canary_body = canary.body.decode("utf-8")
    expected_href = f"href='{HANDSHAKE_CANARY_PUBLIC_JOB_PATH}'"
    canary_link_count = search_body.count(expected_href)
    if canary_link_count != 2:
        raise RuntimeError(
            "the Handshake card does not expose exactly two canary links "
            f"(found {canary_link_count})"
        )
    if KARL_PUBLIC_JOB_PATH in jobs_body or KARL_PUBLIC_JOB_PATH in search_body:
        raise RuntimeError("Karl's legacy path leaked into the production catalog")
    internal_href_paths = _internal_href_paths(jobs_body) + _internal_href_paths(
        search_body
    )
    unpublished_internal_paths = sorted(
        set(internal_href_paths).difference(PRODUCTION_OWNED_ROUTES)
    )
    if unpublished_internal_paths:
        raise RuntimeError(
            "the production catalog emitted unpublished internal href paths: "
            + ", ".join(unpublished_internal_paths)
        )
    if set(internal_href_paths) != PRODUCTION_OWNED_ROUTES:
        raise RuntimeError("the production catalog did not cover its exact route set")
    if f"rel='canonical' href='https://www.wahojobs.com{HANDSHAKE_CANARY_PUBLIC_JOB_PATH}'" not in canary_body:
        raise RuntimeError("the Handshake detail canonical URL is not production www")
    if "rel='canonical' href='https://www.wahojobs.com/jobs'" not in jobs_body:
        raise RuntimeError("the /jobs canonical URL is not production www")

    return {
        "artifact": str(database_path),
        "database_sha256": projection.database_sha256,
        "release_id": release.release_id,
        "registry_sha256": projection.registry_sha256,
        "catalog_job_count": projection.job_count,
        "company_count": projection.company_count,
        "published_detail_count": projection.path_count,
        "internal_href_count": len(internal_href_paths),
        "internal_href_paths": sorted(set(internal_href_paths)),
        "unpublished_internal_href_count": 0,
        "owned_routes": ["/jobs", HANDSHAKE_CANARY_PUBLIC_JOB_PATH],
        "cache_policy": "private, no-store, max-age=0",
        "ready": True,
        "karl_legacy_owned": True,
        "external_calls": 0,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Attest the offline production public-only projection artifact."
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--release-manifest", required=True, type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = attest_production_artifact(
        database_path=args.database,
        release_manifest_path=args.release_manifest,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
