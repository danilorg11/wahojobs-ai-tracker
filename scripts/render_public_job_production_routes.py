#!/usr/bin/env python3
"""Render offline Production Exact-Route Release v1 boundaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.public_job_release import load_production_release_manifest


PUBLIC_ORIGIN = "https://www.wahojobs.com"
PUBLICATION_FORMAT = "wahojobs-production-route-publication-v1"
GATEWAY_TARGET = "production-gateway-deployment"
DEFAULT_MANIFEST = ROOT / "deploy/vercel-production-gateway/release-manifest.json"
DEFAULT_VERCEL = ROOT / "deploy/vercel-production-gateway/vercel.json"
DEFAULT_CADDY = ROOT / "deploy/public-catalog-production-origin/Caddyfile"
DEFAULT_ACTIVATION = (
    ROOT / "deploy/production-exact-route-v1/route-publication-activation.json"
)
DEFAULT_ROLLBACK = (
    ROOT / "deploy/production-exact-route-v1/route-publication-rollback.json"
)
PRESERVATION_PATHS = (
    "/",
    "/job/oneforma-karl-llm-1",
    "/job/freecash-multi-task-contributor-1",
    "/job/oneforma-atlas-creator-1",
    "/remote-companies",
    "/company/oneforma",
    "/online-jobs/ai-training",
    "/blog",
    "/blog/ai-training",
    "/editorial-guidelines",
    "/robots.txt",
    "/sitemap.xml",
    "/sitemaps/static.xml",
    "/sitemaps/jobs.xml",
    "/sitemaps/companies.xml",
)


def owned_routes(release) -> tuple[str, ...]:
    return ("/jobs",) + release.paths


def render_vercel_configuration(release) -> str:
    routes = owned_routes(release)
    configuration = {
        "$schema": "https://openapi.vercel.sh/vercel.json",
        "functions": {
            "api/job.mjs": {"maxDuration": 10},
            "api/jobs.mjs": {"maxDuration": 10},
        },
        "rewrites": [
            {"source": "/jobs", "destination": "/api/jobs"},
            *(
                {"source": path, "destination": "/api/job"}
                for path in release.paths
            ),
        ],
        "headers": [
            {
                "source": path,
                "headers": [
                    {"key": "x-vercel-enable-rewrite-caching", "value": "0"},
                    {
                        "key": "Cache-Control",
                        "value": "private, no-store, max-age=0",
                    },
                    {"key": "CDN-Cache-Control", "value": "no-store"},
                    {"key": "Vercel-CDN-Cache-Control", "value": "no-store"},
                ],
            }
            for path in routes
        ],
    }
    return json.dumps(configuration, indent=2, ensure_ascii=True) + "\n"


def render_caddy_configuration(release) -> str:
    path_arguments = " ".join(owned_routes(release))
    return f"""{{
\tadmin 127.0.0.1:2019
}}

{{$WAHOJOBS_ORIGIN_HOST}} {{
\tencode gzip

\t@trusted_public {{
\t\tpath {path_arguments}
\t\tmethod GET HEAD
\t\theader X-Wahojobs-Origin-Auth {{$WAHOJOBS_ORIGIN_AUTH_TOKEN}}
\t}}
\thandle @trusted_public {{
\t\treverse_proxy 127.0.0.1:{{$WAHOJOBS_ORIGIN_PORT}} {{
\t\t\theader_up Host www.wahojobs.com
\t\t\theader_up -Cookie
\t\t\theader_up -Authorization
\t\t\theader_up -Origin
\t\t\theader_up X-Wahojobs-Origin-Auth {{$WAHOJOBS_ORIGIN_AUTH_TOKEN}}
\t\t}}
\t}}

\t@trusted_operator {{
\t\tpath /__origin/live /__origin/ready /__origin/metrics
\t\tmethod GET HEAD
\t\theader X-Wahojobs-Origin-Auth {{$WAHOJOBS_ORIGIN_AUTH_TOKEN}}
\t}}
\thandle @trusted_operator {{
\t\treverse_proxy 127.0.0.1:{{$WAHOJOBS_ORIGIN_PORT}} {{
\t\t\theader_up Host www.wahojobs.com
\t\t\theader_up -Cookie
\t\t\theader_up -Authorization
\t\t\theader_up -Origin
\t\t\theader_up X-Wahojobs-Origin-Auth {{$WAHOJOBS_ORIGIN_AUTH_TOKEN}}
\t\t}}
\t}}

\t@protected path {path_arguments} /__origin/live /__origin/ready /__origin/metrics
\trespond @protected 403
\trespond 404
}}
"""


def route_publication_document(release, *, enabled: bool) -> dict:
    routes = (
        [
            {
                "source": path,
                "destination": GATEWAY_TARGET,
                "destination_path": path,
                "methods": ["GET", "HEAD"],
            }
            for path in owned_routes(release)
        ]
        if enabled
        else []
    )
    return {
        "format": PUBLICATION_FORMAT,
        "release_id": release.release_id,
        "state": "enabled" if enabled else "disabled",
        "public_origin": PUBLIC_ORIGIN,
        "cache_policy": "no-store",
        "routes": routes,
        "preservation_paths": list(PRESERVATION_PATHS),
    }


def render_route_publication(release, *, enabled: bool) -> str:
    return (
        json.dumps(
            route_publication_document(release, enabled=enabled),
            indent=2,
            ensure_ascii=True,
        )
        + "\n"
    )


def _write_or_check(path: Path, expected: str, *, check: bool) -> None:
    if check:
        try:
            actual = path.read_text(encoding="utf-8")
        except OSError:
            raise SystemExit(f"missing generated route file: {path}") from None
        if actual != expected:
            raise SystemExit(f"stale generated route file: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(expected, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--vercel", type=Path, default=DEFAULT_VERCEL)
    parser.add_argument("--caddy", type=Path, default=DEFAULT_CADDY)
    parser.add_argument("--activation", type=Path, default=DEFAULT_ACTIVATION)
    parser.add_argument("--rollback", type=Path, default=DEFAULT_ROLLBACK)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    release = load_production_release_manifest(arguments.manifest)
    generated = (
        (arguments.vercel, render_vercel_configuration(release)),
        (arguments.caddy, render_caddy_configuration(release)),
        (arguments.activation, render_route_publication(release, enabled=True)),
        (arguments.rollback, render_route_publication(release, enabled=False)),
    )
    for path, content in generated:
        _write_or_check(path, content, check=arguments.check)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
