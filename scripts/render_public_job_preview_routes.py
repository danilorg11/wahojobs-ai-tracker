#!/usr/bin/env python3
"""Render the two exact Preview routing boundaries from an attested release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.public_job_release import load_preview_release_manifest


DEFAULT_MANIFEST = ROOT / "deploy/vercel-preview-gateway/release-manifest.json"
DEFAULT_VERCEL = ROOT / "deploy/vercel-preview-gateway/vercel.json"
DEFAULT_CADDY = ROOT / "deploy/digitalocean-preview/Caddyfile"


def render_vercel_configuration(release) -> str:
    owned_paths = ("/jobs",) + release.paths
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
            {"source": "/", "destination": "https://www.wahojobs.com/"},
            {
                "source": "/:path*",
                "destination": "https://www.wahojobs.com/:path*",
            },
        ],
        "headers": [
            {
                "source": path,
                "headers": [
                    {"key": "x-vercel-enable-rewrite-caching", "value": "0"}
                ],
            }
            for path in owned_paths
        ],
    }
    return json.dumps(configuration, indent=2, ensure_ascii=True) + "\n"


def render_caddy_configuration(release) -> str:
    path_arguments = " ".join(("/jobs",) + release.paths)
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
\t\t\theader_up Host {{$WAHOJOBS_PUBLIC_AUTHORITY}}
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
\t\t\theader_up Host {{$WAHOJOBS_PUBLIC_AUTHORITY}}
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


def _write_or_check(path: Path, expected: str, *, check: bool) -> None:
    if check:
        try:
            actual = path.read_text(encoding="utf-8")
        except OSError:
            raise SystemExit(f"missing generated route file: {path}") from None
        if actual != expected:
            raise SystemExit(f"stale generated route file: {path}")
        return
    path.write_text(expected, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--vercel", type=Path, default=DEFAULT_VERCEL)
    parser.add_argument("--caddy", type=Path, default=DEFAULT_CADDY)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    release = load_preview_release_manifest(arguments.manifest)
    _write_or_check(
        arguments.vercel,
        render_vercel_configuration(release),
        check=arguments.check,
    )
    _write_or_check(
        arguments.caddy,
        render_caddy_configuration(release),
        check=arguments.check,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
