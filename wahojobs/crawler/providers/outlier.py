"""Exact public Outlier board and role-detail evidence."""

import hashlib
from http.client import HTTPException
import json
import re
from collections import Counter
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from wahojobs.classification import (
    AVAILABILITY_BASIS_PUBLIC_FEED, OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY,
    OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
)
from wahojobs.crawler.local_inventory import (open_public, remaining_http_requests,
    record_pending_qualification_ids)
from wahojobs.crawler.types import BODY_OBSERVATION_PRESENT, JobCandidate, RecordPromotionAttestation
from wahojobs.daily_source_policy import OUTLIER_V1_IDS, OUTLIER_MAX_DETAILS, observed_outlier_details

INDEX_URL = "https://app.outlier.ai/internal/experts/job-board/jobs"
DETAIL_PREFIX = INDEX_URL + "/"
PUBLIC_PREFIX = "https://app.outlier.ai/opportunities/"
CONTRACT_ID = "outlier_index_detail_record_v1"
GENERAL_CONTRACT_ID = "outlier_index_detail_role_family_v2"
# Retained beta-host response-003.raw, the page-linked public role client.
# It renders allowedCountries before location.name and gates Apply on signupFlowId.
PUBLIC_PAGE_CHUNK_SHA256 = "4502c226c3025b82a7e04c99035da0a384f44a588a825482e16a3e303001e1f2"
# The eight IDs with matching public detail and role-bound signup in this
# capture. Additional board rows remain inspection-only until reviewed.
QUALIFIED_ROLE_PROOFS = {
    4729394005: ("Android AI Evaluator - French", "evaluate mobile AI experiences"),
    4729399005: ("Android AI Evaluator - Japanese", "evaluate mobile AI experiences"),
    4729398005: ("Android AI Evaluator - Korean", "evaluate mobile AI experiences"),
    4729395005: ("Android AI Evaluator - Spanish (Mexico)", "evaluate mobile AI experiences"),
    4705643005: ("General Inbound (Coding) – Join the Outlier Expert Community", "AI-training tasks"),
    4705636005: ("General Inbound – Join the Outlier Expert Community", "AI-training tasks"),
    4719499005: ("Qatar Educators", "AI teaching assistant"),
    4723202005: ("Voice Conversation Evaluator – English", "train generative AI voice models"),
}
assert frozenset(QUALIFIED_ROLE_PROOFS) == OUTLIER_V1_IDS
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "application/json", "Content-Type": "application/json",
    "Origin": "https://app.outlier.ai", "Referer": "https://app.outlier.ai/opportunities",
}


def _read_json(request):
    with open_public(request, timeout=30) as response:
        if response.status != 200 or response.geturl() != request.full_url:
            raise ValueError("Outlier response status or destination changed.")
        if "application/json" not in response.headers.get("Content-Type", "").lower():
            raise ValueError("Outlier returned a non-JSON page.")
        return response.read().decode("utf-8")


def parse_index(payload):
    data = json.loads(payload)
    if (type(data) is not dict or type(data.get("jobs")) is not list
            or type(data.get("totalCount")) is not int or type(data.get("page")) is not int
            or type(data.get("totalPages")) is not int or data["page"] != 1
            or data["totalPages"] < 1 or data["totalCount"] < len(data["jobs"])):
        raise ValueError("Outlier board envelope changed.")
    if not data["jobs"]:
        raise ValueError("Outlier empty board has no validated snapshot authority.")
    # Individual evidence does not depend on unrelated malformed cards. The
    # selector excludes every copy of ambiguous IDs; the attestation validator
    # independently requires exactly one matching index record for each job.
    return data["jobs"]


def valid_public_url(value, job_id):
    if type(value) is not str or not re.fullmatch(r"[1-9][0-9]*", str(job_id)):
        return False
    parsed = urlsplit(value)
    return (parsed.scheme == "https" and parsed.netloc == "app.outlier.ai"
            and parsed.path in (f"/en/expert/opportunities/{job_id}",
                                f"/opportunities/{job_id}")
            and not parsed.query and not parsed.fragment)


def should_include_job(row):
    """Index identity alone never qualifies a record for publication."""
    return (type(row) is dict and type(row.get("id")) is int and row["id"] > 0
            and type(row.get("title")) is str and bool(row["title"].strip())
            and valid_public_url(row.get("absolute_url"), row["id"]))


def _countries(value):
    return (type(value) is list and bool(value)
            and all(type(c) is str and c.strip() == c and 2 <= len(c) <= 80 for c in value)
            and len(value) == len(set(value)))


def _supported_role_family(title, content):
    """Families evidenced by the eight real index/detail pairs, not arbitrary AI text."""
    body = content.casefold()
    if title.startswith("Android AI Evaluator - "):
        return "evaluate mobile ai experiences" in body
    if title.startswith("Voice Conversation Evaluator"):
        return "train generative ai voice models" in body
    if title.endswith(" Educators"):
        return "ai teaching assistant" in body
    if title.startswith("General Inbound"):
        return "express your interest" in body and "ai-training tasks" in body
    return False


def qualify_index_detail(row, detail, index_payload, *, general=False):
    """Match an indexed role to a public detail with a role-bound signup flow."""
    if not should_include_job(row):
        raise ValueError("Outlier exact public index identity is missing.")
    if type(detail) is not dict:
        raise ValueError("Outlier detail is not a record.")
    identity = row["id"]
    role_proof = QUALIFIED_ROLE_PROOFS.get(identity)
    if general:
        if role_proof is not None or not _supported_role_family(
                row["title"].strip(), str(detail.get("content", ""))):
            raise ValueError("Outlier role is outside the evidenced role families.")
    elif (role_proof is None or row["title"].strip() != role_proof[0]
            or role_proof[1].casefold() not in str(detail.get("content", "")).casefold()):
        raise ValueError("Outlier role is outside the captured qualified scope.")
    if (type(detail) is not dict or detail.get("id") != identity
            or detail.get("title") != row["title"]
            or detail.get("internal_job_id") != row.get("internal_job_id")
            or not _countries(row.get("allowedCountries"))
            or detail.get("allowedCountries") != row["allowedCountries"]
            or detail.get("location") != row.get("location")
            or type(detail.get("content")) is not str or not detail["content"].strip()
            or detail.get("content") != row.get("content")
            or type(detail.get("signupFlowId")) is not str
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", detail["signupFlowId"])):
        raise ValueError("Outlier role, conditions, or application flow disagree.")
    location = row.get("location")
    if (type(location) is not dict or type(location.get("name")) is not str
            or not location["name"].startswith("Remote - ")):
        raise ValueError("Outlier remote work is not established.")
    if not re.search(r"\b(?:AI|artificial intelligence|generative)\b", detail["content"], re.I):
        raise ValueError("Outlier role is outside the AI work scope.")
    title = row["title"].strip()
    evergreen = title.startswith("General Inbound") and "Express Your Interest" in detail["content"]
    if title.startswith("General Inbound") and not evergreen:
        raise ValueError("Outlier general application type is unsupported.")
    kind = (OPPORTUNITY_KIND_EVERGREEN_APPLICATION if evergreen
            else OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY)
    # The index URL is the exact employer-issued record link. Prior retained
    # observations show its redirect, but this capture only visited one final
    # page; do not claim every constructed destination was observed today.
    public_url = row["absolute_url"]
    # The retained public page client renders nonempty allowedCountries and
    # only falls back to location.name when that list is empty. The latter is
    # inconsistent for several captured roles, so retain both fields and use
    # the actual displayed restriction; never infer worldwide eligibility.
    metadata = {
        "index_payload": index_payload, "index_row": row, "detail_record": detail,
        "application_action": "public role page Apply starts source signup flow",
        "signup_flow_id": detail["signupFlowId"],
        "allowed_countries": row["allowedCountries"],
        "source_location_label": location["name"],
        "location_display_contract": "outlier-public-page-allowedCountries-first-20260924",
        "public_client_sha256": PUBLIC_PAGE_CHUNK_SHA256,
    }
    return JobCandidate(
        external_id=str(identity), title=title,
        location="Remote - " + ", ".join(row["allowedCountries"]), url=public_url,
        department=row.get("pod_group"),
        expertise=", ".join(row.get("skillNames") or []),
        opportunity_kind=kind, availability_basis=AVAILABILITY_BASIS_PUBLIC_FEED,
        include_in_live_market_estimate=False,
        source_body=detail["content"], source_body_format="text/html",
        source_metadata=metadata,
        record_promotion_attestation=RecordPromotionAttestation(
            contract_id=GENERAL_CONTRACT_ID if general else CONTRACT_ID,
            body_observation=BODY_OBSERVATION_PRESENT,
            authority_evidence={
                "id": identity, "index_sha256": hashlib.sha256(index_payload.encode()).hexdigest(),
                "detail_sha256": hashlib.sha256(json.dumps(detail, sort_keys=True,
                    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
                "signup_flow_id": detail["signupFlowId"], "public_url": public_url,
                "public_client_sha256": PUBLIC_PAGE_CHUNK_SHA256,
            }))


def select_daily_rows(rows, slots, day_ordinal):
    """Sweep all evidenced families before spending remaining slots on discovery."""
    if type(slots) is not int or not 0 <= slots <= OUTLIER_MAX_DETAILS or type(day_ordinal) is not int:
        raise ValueError("Outlier detail rotation bounds are invalid.")
    counts = Counter(str(row.get('id')) for row in rows if type(row) is dict)
    eligible = sorted((row for row in rows if should_include_job(row)
        and counts[str(row['id'])] == 1), key=lambda row: row['id'])
    supported = [row for row in eligible if row['id'] in OUTLIER_V1_IDS
        or _supported_role_family(row['title'].strip(), str(row.get('content', '')))]
    other = [row for row in eligible if row not in supported]
    selected = []
    for pool in (supported, other):
        count = min(len(pool), slots-len(selected))
        start = day_ordinal*count % len(pool) if pool and count < len(pool) else 0
        selected.extend(pool[(start+offset) % len(pool)] for offset in range(count))
    return selected


def fetch_outlier_jobs(api_url):
    if api_url != INDEX_URL:
        raise ValueError("Outlier endpoint differs from the reviewed public board.")
    index_payload = _read_json(Request(api_url, data=b"{}", headers=REQUEST_HEADERS, method="POST"))
    rows = parse_index(index_payload)
    remaining = remaining_http_requests()
    slots = min(OUTLIER_MAX_DETAILS, len(rows), remaining if remaining is not None else 8)
    selected = select_daily_rows(rows, slots, datetime.now(timezone.utc).date().toordinal())
    candidates = []
    with observed_outlier_details(tuple(DETAIL_PREFIX + str(row["id"]) for row in selected),
                                  index_ids={row['id'] for row in rows if type(row) is dict
                                      and type(row.get('id')) is int and row['id'] > 0}):
        for row in selected:
            try:
                detail = json.loads(_read_json(Request(
                    DETAIL_PREFIX + str(row["id"]), headers=REQUEST_HEADERS)))
                candidates.append(qualify_index_detail(row, detail, index_payload,
                    general=row['id'] not in OUTLIER_V1_IDS))
            except (ValueError, TypeError, KeyError, HTTPError, URLError, TimeoutError,
                    OSError, HTTPException):
                continue
    accepted = {int(job.external_id) for job in candidates}
    record_pending_qualification_ids(source='outlier',
        identities={row['id'] for row in rows if type(row) is dict and type(row.get('id')) is int
                    and row['id'] > 0
                    and row['id'] not in accepted},
        index_sha256=hashlib.sha256(index_payload.encode()).hexdigest())
    return candidates, len(rows), len(selected)
