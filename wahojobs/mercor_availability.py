"""Dated anonymous Mercor exact-page observations; no catalog mutation.

This contract is deliberately separate from the partial explorer snapshot. Its
negative observation means public applications closed, not employer deletion.
The caller retains every audit event and must bind publication to that journal.
"""
from dataclasses import asdict, dataclass, field, fields as dataclass_fields, replace
from contextlib import contextmanager
from contextvars import ContextVar
from http.client import HTTPException
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

CONTRACT_ID = "mercor_public_page_availability_v1"
POSITIVE_CONTRACT_ID = "mercor_public_page_active_record_v1"
_KNOWN_JOBS = ContextVar("mercor_known_public_jobs", default=())
MAX_MISSING_JOBS = 100
JOB_FIELDS = ("external_id", "title", "location", "url", "department", "expertise", "commitment",
              "opportunity_kind", "availability_basis", "include_in_live_market_estimate")
PUBLIC_ORIGIN = "https://work.mercor.com"
PAGE_ROUTE = "/jobs/[listingId]/[[...slug]]"
CLOSED_MESSAGE = "This listing is no longer accepting applications."
MAX_BODY_BYTES = 2_000_000
MAX_REQUESTS_PER_ID = 2
REQUEST_TIMEOUT_SECONDS = 30
REDIRECT_STATUSES = frozenset({301, 302, 307, 308})
REQUIRED_ROLE_FIELDS = frozenset({"listingId", "status", "deletedAt", "isPrivate", "disableApplications"})


@dataclass(frozen=True)
class AvailabilityDecision:
    state: str
    reason: str
    canonical_url: str | None = None
    role_fields: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AvailabilityObservation:
    contract_id: str
    listing_id: str
    requested_url: str
    final_url: str | None
    started_at: str
    completed_at: str
    requests_used: int
    body_sha256: str | None
    decision: AvailabilityDecision


def _timestamp():
    from wahojobs.crawler.pipeline import utc_now
    return utc_now()


def _identity(listing_id):
    if type(listing_id) is not str or re.fullmatch(r"list_[A-Za-z0-9_-]+", listing_id) is None:
        raise ValueError("mercor_exact_listing_id_required")
    return listing_id


def public_job_url(listing_id, value=None):
    """Only the exact public role path; never arbitrary employer URLs or APIs."""
    _identity(listing_id)
    value = value if value is not None else PUBLIC_ORIGIN + "/jobs/" + listing_id
    if type(value) is not str:
        raise ValueError("mercor_public_job_url_invalid")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc != "work.mercor.com"
            or parsed.query or parsed.fragment
            or re.fullmatch(r"/jobs/" + re.escape(listing_id) + r"(?:/[a-z0-9][a-z0-9-]*)?", parsed.path) is None):
        raise ValueError("mercor_public_job_url_invalid")
    return value


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


class _PublicPage(HTMLParser):
    """Read Next state and visible role actions; script text is never visible."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.next_data = []
        self.canonicals = []
        self.controls = []
        self.stack = []
        self.capture = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        parent_hidden = self.stack[-1][1] if self.stack else False
        style = re.sub(r"\s+", "", values.get("style", "").casefold())
        hidden = (parent_hidden or tag in ("script", "style", "noscript", "template")
                  or "hidden" in values or values.get("aria-hidden") == "true"
                  or "display:none" in style or "visibility:hidden" in style)
        if tag == "link" and "canonical" in values.get("rel", "").lower().split():
            self.canonicals.append(values.get("href"))
        if tag == "script" and values.get("id") == "__NEXT_DATA__":
            self.capture = []
            self.next_data.append(self.capture)
        control = None
        if tag in ("a", "button") and not hidden:
            control = [tag, values, []]
            self.controls.append(control)
        if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
            self.stack.append((tag, hidden, control))

    def handle_endtag(self, tag):
        if tag == "script":
            self.capture = None
        for index in range(len(self.stack)-1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.capture is not None:
            self.capture.append(data)
        if self.stack and self.stack[-1][1]:
            return
        for _, _, control in self.stack:
            if control is not None:
                control[2].append(data)


def inspect_public_page(raw, *, listing_id, final_url):
    """Qualify only the two directly observed public application states."""
    public_job_url(listing_id, final_url)
    if type(raw) is not bytes or len(raw) > MAX_BODY_BYTES:
        return AvailabilityDecision("indeterminate", "invalid_or_oversized_body")
    try:
        page = _PublicPage()
        page.feed(raw.decode("utf-8-sig"))
        if len(page.next_data) != 1 or len(page.canonicals) != 1:
            raise ValueError("missing_or_duplicate_page_identity")
        data = json.loads("".join(page.next_data[0]), object_pairs_hook=_strict_object)
        if (type(data) is not dict or data.get("page") != PAGE_ROUTE
                or type(data.get("query")) is not dict or data["query"].get("listingId") != listing_id):
            raise ValueError("page_identity_mismatch")
        props = data["props"]["pageProps"]
        role = props["role"]
        canonical = public_job_url(listing_id, props["nextSeoProps"]["canonical"])
        if canonical != final_url or page.canonicals != [canonical]:
            raise ValueError("canonical_identity_mismatch")
        slug = urlsplit(final_url).path.split("/")[3:]
        if data["query"].get("slug", []) != slug:
            raise ValueError("slug_identity_mismatch")
        if (type(role) is not dict or not REQUIRED_ROLE_FIELDS <= set(role)
                or role["listingId"] != listing_id or role["status"] != "active"
                or role["deletedAt"] is not None
                or type(role["isPrivate"]) is not bool or type(role["disableApplications"]) is not bool
                or "closedListing" not in props or props["closedListing"] is not None
                or "candidateStatus" not in props or props["candidateStatus"] is not None):
            raise ValueError("unsupported_role_state")
        fields = {name: role[name] for name in sorted(REQUIRED_ROLE_FIELDS)}
        actions = [(tag, attrs, " ".join(" ".join(parts).split())) for tag, attrs, parts in page.controls]
        closed = any(tag == "a" and urljoin(PUBLIC_ORIGIN, attrs.get("href", "")) == PUBLIC_ORIGIN + "/explore"
                     and text.startswith(CLOSED_MESSAGE) for tag, attrs, text in actions)
        apply_now = any(tag == "button" and text == "Apply now" for tag, _, text in actions)
        if role["disableApplications"] is True and closed and not apply_now:
            return AvailabilityDecision("closed", "public_applications_closed", canonical, fields)
        if role["isPrivate"] is False and role["disableApplications"] is False and apply_now and not closed:
            return AvailabilityDecision("open", "public_applications_open", canonical, fields)
        return AvailabilityDecision("indeterminate", "application_state_not_established", canonical, fields)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return AvailabilityDecision("indeterminate", "page_contract_not_established")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def observe_public_job(listing_id, *, audit_sink, requested_url=None, http_limit=2,
                       deadline=None, clock=None, monotonic=time.monotonic, shared_budget=False):
    """At most two anonymous attempts; audit before dispatch and before parsing.

    Standalone use requires a durable sink, including raw response bytes. Daily
    collection uses the existing durable budget sink and also supplies a local
    proof collector. Audit errors propagate so unretained evidence cannot publish.
    No generic daily policy, source settings or production state is modified.
    """
    start_url = public_job_url(listing_id, requested_url)
    if type(http_limit) is not int or not 1 <= http_limit <= MAX_REQUESTS_PER_ID:
        raise ValueError("mercor_exact_page_budget_invalid")
    if not callable(audit_sink):
        raise ValueError("mercor_availability_audit_required")
    clock = clock or _timestamp
    started_at = clock()
    requests_used = 0
    url = start_url
    final_url = None
    body_hash = None
    decision = AvailabilityDecision("indeterminate", "request_cap_reached")
    opener = build_opener(_NoRedirect())
    for ordinal in range(1, http_limit+1):
        remaining = REQUEST_TIMEOUT_SECONDS if deadline is None else min(REQUEST_TIMEOUT_SECONDS, deadline-monotonic())
        if remaining <= 0:
            decision = AvailabilityDecision("indeterminate", "execution_deadline_expired")
            break
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
                                       "Accept": "text/html"}, method="GET")
        entry = None
        if shared_budget:
            from wahojobs.crawler.local_inventory import (
                reserve_http_request, remaining_request_seconds, audit_http_response,
                RequestBudgetExceeded)
            from wahojobs.daily_source_policy import validate_request
            validate_request(request)
            try:
                entry = reserve_http_request(request)
                if entry is None:
                    raise ValueError("mercor_audited_budget_required")
                remaining_shared = remaining_request_seconds()
                if remaining_shared is not None: remaining = min(remaining, remaining_shared)
            except (RequestBudgetExceeded, TimeoutError):
                decision = AvailabilityDecision("indeterminate", "shared_budget_exhausted")
                break
        observed_at = entry["observed_at"] if entry is not None else clock()
        audit_sink(dict(event="request", contract_id=CONTRACT_ID, listing_id=listing_id,
                        ordinal=ordinal, method="GET", url=url, observed_at=observed_at))
        requests_used += 1
        try:
            try:
                response = opener.open(request, timeout=remaining)
            except HTTPError as error:
                response = error
            with response:
                status = response.status
                response_url = response.geturl()
                headers = {name: response.headers[name] for name in ("Content-Type", "Location")
                           if response.headers.get(name) is not None}
                locations = response.headers.get_all("Location", [])
                raw = response.read(MAX_BODY_BYTES+1)
        except (URLError, TimeoutError, OSError, HTTPException) as error:
            if shared_budget:
                from wahojobs.crawler.local_inventory import audit_http_error
                audit_http_error(entry, error)
            audit_sink(dict(event="transport_error", contract_id=CONTRACT_ID, listing_id=listing_id,
                            ordinal=ordinal, url=url, error_type=type(error).__name__, observed_at=observed_at))
            decision = AvailabilityDecision("indeterminate", "transport_failed")
            break
        complete = len(raw) <= MAX_BODY_BYTES
        if shared_budget:
            entry.update(status=status, final_url=response_url, response_headers=headers)
            audit_http_response(entry, body=raw, capture_complete=complete)
        audit_sink(dict(event="response", contract_id=CONTRACT_ID, listing_id=listing_id,
                        ordinal=ordinal, url=url, final_url=response_url, status=status,
                        response_headers=headers, observed_at=observed_at,
                        raw_response=raw, capture_complete=complete))
        final_url = response_url
        body_hash = sha256(raw).hexdigest()
        if response_url != url:
            decision = AvailabilityDecision("indeterminate", "unreviewed_redirect")
            break
        if not complete:
            decision = AvailabilityDecision("indeterminate", "invalid_or_oversized_body")
            break
        if status in REDIRECT_STATUSES:
            if ordinal == MAX_REQUESTS_PER_ID:
                decision = AvailabilityDecision("indeterminate", "redirect_limit_reached")
                break
            try:
                if len(locations) != 1:
                    raise ValueError("single_redirect_required")
                url = public_job_url(listing_id, urljoin(url, locations[0]))
            except ValueError:
                decision = AvailabilityDecision("indeterminate", "redirect_identity_mismatch")
                break
            continue
        if status != 200:
            decision = AvailabilityDecision("indeterminate", "http_status_not_authoritative")
            break
        if headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "text/html":
            decision = AvailabilityDecision("indeterminate", "html_response_required")
            break
        decision = inspect_public_page(raw, listing_id=listing_id, final_url=final_url)
        break
    result = AvailabilityObservation(CONTRACT_ID, listing_id, start_url, final_url,
                                     started_at, clock(), requests_used, body_hash, decision)
    audit_sink(dict(event="availability_decision", contract_id=CONTRACT_ID, listing_id=listing_id,
                    state=decision.state, reason=decision.reason, body_sha256=body_hash,
                    requests_used=requests_used, started_at=result.started_at,
                    completed_at=result.completed_at, final_url=final_url))
    return result


@dataclass(frozen=True)
class MercorPageRecord:
    contract_id: str
    listing_id: str
    known_job_id: int
    known_source_hash: str
    known_fields: dict
    started_at: str
    observed_at: str
    requested_url: str
    final_url: str
    raw_html: str
    body_sha256: str
    state: str
    requests: tuple


def load_known_jobs(conn, company_id):
    """Read existing active identities only; no online inventory writes."""
    return tuple(dict(row) for row in conn.execute("""
        SELECT j.*, EXISTS(SELECT 1 FROM job_source_contents sc WHERE sc.job_id=j.id) AS has_accepted_content
        FROM jobs j JOIN companies c ON c.id=j.company_id
        WHERE j.company_id=? AND c.slug='mercor' AND j.is_active=1
        ORDER BY j.last_seen_at, j.id
    """, (company_id,)))


@contextmanager
def known_public_jobs(rows):
    rows = tuple(dict(row) for row in (rows or ()))
    if len(rows) > 20_000 or len({row["external_id"] for row in rows}) != len(rows):
        raise ValueError("mercor_known_identity_scope_invalid")
    token = _KNOWN_JOBS.set(rows)
    try: yield
    finally: _KNOWN_JOBS.reset(token)


def _fields(row):
    values = {name: row[name] for name in JOB_FIELDS}
    values["include_in_live_market_estimate"] = bool(values["include_in_live_market_estimate"])
    return values


def augment_result(result):
    """Spend only the configured shared budget on missing known exact IDs."""
    from wahojobs.crawler.local_inventory import remaining_http_requests
    from wahojobs.daily_source_policy import observed_mercor_public_jobs
    from wahojobs.crawler.types import JobCandidate, RecordPromotionAttestation, BODY_OBSERVATION_PRESENT
    candidates = list(result.jobs)
    present = {job.external_id for job in candidates}
    missing = [row for row in _KNOWN_JOBS.get() if row["external_id"] not in present]
    # A historical invalid URL/identity remains unverified and is reported.
    eligible = []
    for row in missing:
        try: public_job_url(row["external_id"], row["url"])
        except (ValueError, TypeError): continue
        eligible.append(row)
    selected = eligible
    if len(eligible) > MAX_MISSING_JOBS:
        # Failed checks cannot advance last_seen_at. Rotate only an oversized
        # cohort so a permanently blocked oldest group cannot starve later IDs.
        day = _aware(_timestamp()).astimezone(timezone.utc).date().toordinal()
        offset = (day * MAX_MISSING_JOBS) % len(eligible)
        selected = eligible[offset:] + eligible[:offset]
    selected = selected[:MAX_MISSING_JOBS]
    records, attempted = [], 0
    with observed_mercor_public_jobs([row["external_id"] for row in selected]):
        for row in selected:
            remaining = remaining_http_requests()
            if remaining is None or remaining <= 0: break
            events = []
            observation = observe_public_job(row["external_id"], requested_url=row["url"],
                http_limit=min(2, remaining), audit_sink=events.append, shared_budget=True)
            if observation.requests_used == 0: break
            attempted += 1
            if observation.decision.state not in ("open", "closed"): continue
            if observation.decision.state == "open" and not row.get("has_accepted_content", False):
                continue
            responses = [event for event in events if event["event"] == "response"]
            raw = responses[-1]["raw_response"]
            record = MercorPageRecord(CONTRACT_ID, row["external_id"], row["id"], row["source_hash"],
                _fields(row), observation.started_at, _second(responses[-1]["observed_at"]),
                observation.requested_url, observation.final_url, raw.decode("utf-8"),
                observation.body_sha256, observation.decision.state,
                tuple({key: event[key] for key in ("ordinal", "url", "final_url", "status",
                       "response_headers", "observed_at", "capture_complete")} |
                      {"body_sha256": sha256(event["raw_response"]).hexdigest()} for event in responses))
            validate_record(record)
            records.append(record)
            if record.state == "open":
                proof = asdict(record)
                proof.pop("raw_html")
                proof["normalized_body_sha256"] = sha256(normalize_html(record.raw_html).encode()).hexdigest()
                candidates.append(JobCandidate(**record.known_fields,
                    source_body=record.raw_html, source_body_format="text/html",
                    record_promotion_attestation=RecordPromotionAttestation(
                        POSITIVE_CONTRACT_ID, BODY_OBSERVATION_PRESENT, proof)))
    from wahojobs.crawler.local_inventory import record_pending_qualification_ids, record_surface_counts
    record_surface_counts(upstream_records=result.raw_record_count,
        upstream_unit="explorer listing rows; known missing pages checked separately", variants=len(candidates))
    qualified = {record.listing_id for record in records}
    record_pending_qualification_ids(source="mercor",
        identities=[str(row["external_id"]) for row in missing if row["external_id"] not in qualified],
        index_sha256=sha256(json.dumps(sorted(present)).encode()).hexdigest())
    unresolved = len(missing)-len(records)
    warning = (f"Mercor exact public pages: {len(records)} verified of {len(missing)} missing known records; "
               f"{attempted} attempted; {unresolved} remain unconfirmed.")
    return replace(result, jobs=candidates, source_records=tuple(records),
        raw_record_count=result.raw_record_count+attempted,
        normalized_record_count=len(candidates),
        rejected_record_count=result.rejected_record_count+attempted-(len(candidates)-len(result.jobs)),
        warnings=(*result.warnings, warning) if missing else result.warnings)


def normalize_html(value):
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _second(value):
    # Match the established source-run clock without moving evidence forward.
    # The exact transport timestamp remains retained in requests and the journal.
    return _aware(value).replace(microsecond=0).isoformat()


def _aware(value):
    at = datetime.fromisoformat(value)
    if at.tzinfo is None: raise ValueError("mercor_aware_observation_required")
    return at


def validate_record(record, *, body_is_normalized=False):
    """Recompute identity, redirect path and the visible typed availability state."""
    if type(record) is not MercorPageRecord:
        raise ValueError("mercor_exact_record_type_invalid")
    _identity(record.listing_id)
    if (record.contract_id != CONTRACT_ID
            or type(record.known_job_id) is not int or record.known_job_id <= 0
            or record.known_source_hash != sha256(record.listing_id.encode()).hexdigest()
            or type(record.known_fields) is not dict or set(record.known_fields) != set(JOB_FIELDS)
            or record.known_fields["external_id"] != record.listing_id
            or record.known_fields["url"] != record.requested_url
            or record.state not in ("open", "closed")
            or type(record.raw_html) is not str):
        raise ValueError("mercor_exact_record_invalid")
    raw = record.raw_html.encode("utf-8")
    if len(raw) > MAX_BODY_BYTES: raise ValueError("mercor_exact_body_oversized")
    public_job_url(record.listing_id, record.requested_url)
    public_job_url(record.listing_id, record.final_url)
    if (not body_is_normalized and sha256(raw).hexdigest() != record.body_sha256
            or inspect_public_page(raw, listing_id=record.listing_id, final_url=record.final_url).state != record.state
            or not 1 <= len(record.requests) <= 2):
        raise ValueError("mercor_exact_record_evidence_invalid")
    expected_url = record.requested_url
    previous_at = _aware(record.started_at)
    for index, request in enumerate(record.requests, 1):
        if (type(request) is not dict or set(request) != {"ordinal", "url", "final_url",
                "status", "response_headers", "observed_at", "capture_complete", "body_sha256"}
                or type(request["ordinal"]) is not int or request["ordinal"] != index
                or type(request["status"]) is not int or request["url"] != expected_url
                or request["final_url"] != expected_url or request["capture_complete"] is not True
                or type(request["response_headers"]) is not dict
                or set(request["response_headers"])-{"Content-Type", "Location"}
                or any(type(value) is not str for value in request["response_headers"].values())
                or type(request["body_sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", request["body_sha256"]) is None
                or _aware(request["observed_at"]) < previous_at):
            raise ValueError("mercor_exact_transport_invalid")
        previous_at = _aware(request["observed_at"])
        if index < len(record.requests):
            if request["status"] not in REDIRECT_STATUSES or not request["response_headers"].get("Location"):
                raise ValueError("mercor_exact_redirect_invalid")
            expected_url = public_job_url(record.listing_id,
                urljoin(expected_url, request["response_headers"].get("Location", "")))
        elif (request["status"] != 200 or expected_url != record.final_url
                or request["body_sha256"] != record.body_sha256
                or _second(request["observed_at"]) != record.observed_at
                or request["response_headers"].get("Content-Type", "").split(";", 1)[0].strip().lower() != "text/html"):
            raise ValueError("mercor_exact_response_invalid")
    return record


def validate_publication(conn, company_id, crawl_run_id, result, now):
    """Validate every exact transition before any catalog write in the transaction."""
    records = result.source_records
    positives = [job for job in result.jobs if job.record_promotion_attestation is not None
                 and job.record_promotion_attestation.contract_id == POSITIVE_CONTRACT_ID]
    if not records and not positives: return {}
    run = conn.execute("SELECT * FROM crawl_runs WHERE id=?", (crawl_run_id,)).fetchone()
    if (result.source_type != "mercor-marketplace" or result.used_sample_data
            or result.snapshot_complete or result.pagination_complete or result.outcome != "partial"
            or result.normalized_record_count != len(result.jobs)
            or result.raw_record_count != result.normalized_record_count+result.rejected_record_count
            or run is None or run["company_id"] != company_id):
        raise ValueError("mercor_exact_publication_scope_invalid")
    by_id = {}
    candidates = {job.external_id: job for job in result.jobs}
    if len(candidates) != len(result.jobs): raise ValueError("mercor_exact_duplicate_candidate")
    for record in records:
        validate_record(record)
        if (record.listing_id in by_id or not _aware(run["started_at"]) <=
                _aware(record.started_at) <= _aware(record.observed_at) <= _aware(now)):
            raise ValueError("mercor_exact_observation_time_or_identity_invalid")
        existing = conn.execute("""SELECT j.* FROM jobs j JOIN companies c ON c.id=j.company_id
            WHERE j.id=? AND j.company_id=? AND c.slug='mercor'""",
            (record.known_job_id, company_id)).fetchone()
        if (existing is None or existing["is_active"] != 1
                or existing["source_hash"] != record.known_source_hash
                or _fields(existing) != record.known_fields
                or _aware(existing["last_seen_at"]) > _aware(record.observed_at)):
            raise ValueError("mercor_exact_known_record_changed")
        if record.state == "closed":
            if record.listing_id in candidates: raise ValueError("mercor_exact_state_conflict")
        else:
            candidate = candidates.get(record.listing_id)
            if (candidate is None or candidate.record_promotion_attestation is None
                    or candidate.record_promotion_attestation.contract_id != POSITIVE_CONTRACT_ID
                    or {name: getattr(candidate, name) for name in JOB_FIELDS} != record.known_fields
                    or candidate.source_body != record.raw_html):
                raise ValueError("mercor_exact_positive_missing")
            expected = asdict(record); expected.pop("raw_html")
            expected["normalized_body_sha256"] = sha256(normalize_html(record.raw_html).encode()).hexdigest()
            # JSON forms intentionally allow tuple/list differences from the sealed codec.
            if json.dumps(candidate.record_promotion_attestation.authority_evidence, sort_keys=True) != json.dumps(expected, sort_keys=True):
                raise ValueError("mercor_exact_positive_binding_invalid")
        by_id[record.listing_id] = record
    if {job.external_id for job in positives} != {identity for identity, record in by_id.items() if record.state == "open"}:
        raise ValueError("mercor_exact_unbound_positive")
    return by_id


def validate_positive_capture(attestation, candidate, prepared, context, *, provider, source_type):
    """Availability-only evidence is replayable but can never replace accepted content."""
    proof = json.loads(attestation.authority_evidence_json)
    normalized_hash = proof.pop("normalized_body_sha256", None)
    # Original raw bytes live in the sealed collection. Content capture normalizes
    # whitespace, so verify that normalization separately before reconstructing.
    if (provider != "mercor" or source_type != "mercor-marketplace"
            or context.used_sample_data or context.provider_outcome != "partial"
            or context.snapshot_complete or context.pagination_complete
            or context.crawl_run_id is None or context.normalized_record_count != context.candidate_count
            or context.raw_record_count != context.normalized_record_count+context.rejected_record_count
            or prepared.body_format != "text/html" or not prepared.body
            or sha256(prepared.body.encode()).hexdigest() != normalized_hash
            or proof.get("state") != "open"
            or candidate.external_id != proof.get("listing_id")
            or {name: getattr(candidate, name) for name in JOB_FIELDS} != proof.get("known_fields")
            or inspect_public_page(prepared.body.encode(), listing_id=candidate.external_id,
                final_url=proof.get("final_url")).state != "open"):
        raise ValueError("mercor_public_page_positive_invalid")
    expected_keys = {field.name for field in dataclass_fields(MercorPageRecord)}-{"raw_html"}
    if set(proof) != expected_keys or proof.get("contract_id") != CONTRACT_ID:
        raise ValueError("mercor_public_page_positive_shape_invalid")
    validate_record(MercorPageRecord(**proof, raw_html=prepared.body), body_is_normalized=True)


def validate_journal_records(result, report):
    """Bind exact records to raw HTTP evidence already checked by the journal."""
    if result.source_type != "mercor-marketplace" or not result.source_records: return
    events = [event["data"] for event in report["events"] if event["event"] == "source_transport"]
    responses = [event for event in events if event.get("event") == "response"]
    requests = [event for event in events if event.get("event") == "request"]
    consumed = set()
    for record in result.source_records:
        validate_record(record)
        previous = 0
        for proof in record.requests:
            matches = [event for event in responses if
                event.get("url") == proof["url"] and event.get("final_url") == proof["final_url"]
                and event.get("status") == proof["status"]
                and event.get("observed_at") == proof["observed_at"]
                and event.get("response_headers") == proof["response_headers"]
                and event.get("capture_complete") is True
                and event.get("raw_response", {}).get("sha256") == proof["body_sha256"]]
            if len(matches) != 1:
                raise ValueError("mercor_exact_journal_response_unbound")
            ordinal = matches[0]["ordinal"]
            if (ordinal in consumed or ordinal <= previous or not any(event.get("ordinal") == ordinal
                    and event.get("url") == proof["url"] and event.get("method") == "GET"
                    and event.get("observed_at") == proof["observed_at"] for event in requests)):
                raise ValueError("mercor_exact_journal_request_unbound")
            consumed.add(ordinal)
            previous = ordinal
