"""Synthetic proof for exact preview routing and legacy isolation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
import json
import os
import sys
from urllib.parse import urljoin, urlsplit

import requests


DEFAULT_LEGACY_PATHS = (
    "/",
    "/job/wahojobs-preview-unmatched-job",
    "/company/wahojobs-preview-unmatched-company",
    "/online-jobs/wahojobs-preview-unmatched-online-job",
    "/_next/static/wahojobs-preview-unmatched-static.js",
    "/wahojobs-preview-random-unmatched-path",
)


class PreviewProbeFailure(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ProbeResult:
    checks: tuple[str, ...]
    origin_jobs_delta: int
    origin_rejected_delta: int


def probe(preview_origin, new_origin, token, *, legacy_paths=DEFAULT_LEGACY_PATHS):
    preview = _https_origin(preview_origin)
    origin = _https_origin(new_origin)
    if not isinstance(token, str) or len(token) != 43:
        raise PreviewProbeFailure("invalid_origin_token")
    session = requests.Session()
    session.headers.update({"User-Agent": "wahojobs-preview-probe/1"})
    checks = []

    direct = session.get(urljoin(origin, "/jobs"), timeout=10, allow_redirects=False)
    if direct.status_code != 403:
        raise PreviewProbeFailure("untrusted_origin_not_rejected")
    checks.append("direct_origin_rejected")

    wrong = session.get(
        urljoin(origin, "/jobs"),
        headers={"X-Wahojobs-Origin-Auth": "A" * 43},
        timeout=10,
        allow_redirects=False,
    )
    if wrong.status_code != 403:
        raise PreviewProbeFailure("wrong_origin_secret_not_rejected")
    checks.append("wrong_origin_secret_rejected")

    before = _origin_metrics(session, origin, token)
    catalog = session.get(urljoin(preview, "/jobs"), timeout=15, allow_redirects=False)
    if (
        catalog.status_code != 200
        or catalog.headers.get("x-wahojobs-preview-owner") != "new-origin"
        or catalog.headers.get("x-wahojobs-origin") != "public-catalog-preview"
        or b"<title>" not in catalog.content
    ):
        raise PreviewProbeFailure("preview_jobs_not_new_origin")
    checks.append("preview_jobs_new_origin")

    for path in legacy_paths:
        preview_response = session.get(
            urljoin(preview, path), timeout=15, allow_redirects=False
        )
        legacy_response = session.get(
            urljoin("https://www.wahojobs.com", path),
            timeout=15,
            allow_redirects=False,
        )
        if preview_response.headers.get("x-wahojobs-origin") is not None:
            raise PreviewProbeFailure("legacy_path_reached_new_origin")
        if (
            preview_response.status_code != legacy_response.status_code
            or preview_response.headers.get("location")
            != legacy_response.headers.get("location")
            or sha256(preview_response.content).digest()
            != sha256(legacy_response.content).digest()
        ):
            raise PreviewProbeFailure("legacy_response_changed")
    checks.append("legacy_paths_match_current_site")

    after = _origin_metrics(session, origin, token)
    jobs_delta = after["jobs"] - before["jobs"]
    rejected_delta = after["rejected"] - before["rejected"]
    if jobs_delta != 1 or rejected_delta != 0:
        raise PreviewProbeFailure("origin_received_unexpected_routes")
    checks.append("origin_received_only_jobs")
    return ProbeResult(tuple(checks), jobs_delta, rejected_delta)


def probe_rollback(preview_origin):
    preview = _https_origin(preview_origin)
    response = requests.get(
        urljoin(preview, "/jobs"),
        headers={"User-Agent": "wahojobs-preview-probe/1"},
        timeout=15,
        allow_redirects=False,
    )
    legacy = requests.get(
        "https://www.wahojobs.com/jobs",
        headers={"User-Agent": "wahojobs-preview-probe/1"},
        timeout=15,
        allow_redirects=False,
    )
    if (
        response.headers.get("x-wahojobs-preview-owner") != "legacy-fallback"
        or response.status_code != legacy.status_code
        or response.headers.get("location") != legacy.headers.get("location")
        or sha256(response.content).digest() != sha256(legacy.content).digest()
    ):
        raise PreviewProbeFailure("preview_rollback_not_legacy")
    return ProbeResult(("preview_jobs_rolled_back_to_legacy",), 0, 0)


def _origin_metrics(session, origin, token):
    response = session.get(
        urljoin(origin, "/__origin/metrics"),
        headers={"X-Wahojobs-Origin-Auth": token},
        timeout=10,
        allow_redirects=False,
    )
    if response.status_code != 200:
        raise PreviewProbeFailure("origin_metrics_unavailable")
    value = response.json()
    if type(value) is not dict or set(value) != {"health", "jobs", "rejected"}:
        raise PreviewProbeFailure("origin_metrics_invalid")
    return {key: int(value[key]) for key in value}


def _https_origin(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise PreviewProbeFailure("invalid_https_origin")
    return value + "/"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview-origin", required=True)
    parser.add_argument("--new-origin", required=False)
    parser.add_argument("--origin-token-env", default="WAHOJOBS_ORIGIN_AUTH_TOKEN")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--legacy-path", action="append", default=[])
    arguments = parser.parse_args(argv)
    try:
        if arguments.rollback:
            result = probe_rollback(arguments.preview_origin)
        else:
            result = probe(
                arguments.preview_origin,
                arguments.new_origin,
                os.environ.get(arguments.origin_token_env, ""),
                legacy_paths=tuple(arguments.legacy_path) or DEFAULT_LEGACY_PATHS,
            )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except Exception:
        print("HYBRID_PREVIEW_PROBE_FAILED", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "passed",
                "checks": result.checks,
                "origin_jobs_delta": result.origin_jobs_delta,
                "origin_rejected_delta": result.origin_rejected_delta,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
