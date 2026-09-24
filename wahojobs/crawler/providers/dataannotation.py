from dataclasses import dataclass
from contextlib import nullcontext
import hashlib
import html
import re
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from wahojobs.crawler.local_inventory import open_catalog
from wahojobs.daily_source_policy import (current_source, observed_dataannotation_redirect,
    controlled_dataannotation_domains, controlled_validation_active)

from wahojobs.classification import (
    AVAILABILITY_BASIS_EVERGREEN_PAGE,
    OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
)
from wahojobs.crawler.types import JobCandidate, RecordPromotionAttestation, BODY_OBSERVATION_PRESENT


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "text/html,application/xhtml+xml",
}


@dataclass(frozen=True)
class DataAnnotationDomain:
    slug: str
    title: str
    category: str


DOMAIN_PAGES = (
    DataAnnotationDomain(
        "coding",
        "AI Coding Specialist / Coding Expert",
        "Coding / Software Evaluation",
    ),
    DataAnnotationDomain(
        "generalist",
        "Generalist AI Trainer",
        "Generalist",
    ),
    DataAnnotationDomain(
        "law",
        "Law Expert / Legal AI Trainer",
        "Legal Experts",
    ),
    DataAnnotationDomain(
        "math",
        "Math Expert / AI Math Trainer",
        "STEM Experts",
    ),
    DataAnnotationDomain(
        "medicine",
        "Medicine Expert / Medical AI Trainer",
        "Medical Experts",
    ),
    DataAnnotationDomain(
        "physics",
        "Physics Expert / AI Physics Trainer",
        "STEM Experts",
    ),
    DataAnnotationDomain(
        "finance",
        "Finance Expert / Finance AI Trainer",
        "Finance Experts",
    ),
    DataAnnotationDomain(
        "accounting",
        "Accounting Expert / Accounting AI Trainer",
        "Finance Experts",
    ),
    # Currently returns 404, but retained because prior public-page research
    # found it as a worker-facing domain application page.
    DataAnnotationDomain(
        "bilingual",
        "Bilingual AI Trainer",
        "Language / Linguistics",
    ),
    DataAnnotationDomain(
        "chemistry",
        "Chemistry Expert / AI Chemistry Trainer",
        "STEM Experts",
    ),
    DataAnnotationDomain(
        "biology",
        "Biology Expert / AI Biology Trainer",
        "STEM Experts",
    ),
)


def fetch_dataannotation_jobs(base_url):
    jobs = []
    skipped = []
    network_or_site_errors = []

    selected = controlled_dataannotation_domains()
    if selected is None and current_source() == "dataannotation" and not controlled_validation_active():
        selected = frozenset(("coding", *ROLE_IDENTITIES))
    for domain in DOMAIN_PAGES:
        if selected is not None and domain.slug not in selected:
            continue
        url = build_domain_url(base_url, domain.slug)
        try:
            page = fetch_page(url)
        except ValueError as exc:
            # A rejected route has no record authority, but independent fixed
            # pages can still be observed. The result remains partial.
            skipped.append(f"{domain.slug} (unsupported route: {exc})")
            continue
        if not page["ok"]:
            skipped.append(f"{domain.slug} ({page['reason']})")
            if page["outcome"] == "network_or_site_error":
                network_or_site_errors.append(domain.slug)
            continue

        if not has_application_surface(page["text"], domain.slug):
            skipped.append(f"{domain.slug} (generic or unsupported page)")
            continue

        try:
            jobs.append(parse_domain_page(domain, page["url"], page["text"]))
        except ValueError as exc:
            skipped.append(f"{domain.slug} (unsupported role evidence: {exc})")

    if network_or_site_errors and not jobs:
        raise RuntimeError(
            "DataAnnotation crawl failed: "
            f"{len(network_or_site_errors)} allowlisted page(s) had "
            "network/site errors."
        )

    return jobs, skipped


def build_domain_url(base_url, slug):
    return f"{base_url.rstrip('/')}/{slug}"


CANONICAL_REDIRECTS = {
    "/coding": "/job-board/software-engineer",
    "/generalist": "/job-board/generalist",
    "/law": "/job-board/legal-expert",
    "/math": "/job-board/mathematician",
    "/medicine": "/job-board/medical-expert",
    "/physics": "/job-board/physicist",
    "/finance": "/job-board/finance-expert",
    "/accounting": "/job-board/accountant",
    "/chemistry": "/job-board/chemist",
    "/biology": "/job-board/biologist",
}

# These exact identities were observed in the two September 24 beta-host
# capture journals. A new path/title is a new qualification decision.
ROLE_IDENTITIES = {
    "generalist": ("generalist", "Generalist"),
    "law": ("legal-expert", "Legal Expert"),
    "math": ("mathematician", "Mathematician"),
    "medicine": ("medical-expert", "Health/Medical Expert"),
    "physics": ("physicist", "Physicist"),
    "finance": ("finance-expert", "Finance Expert"),
    "accounting": ("accountant", "Accountant"),
    "chemistry": ("chemist", "Chemist"),
    "biology": ("biologist", "Biologist"),
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _allowed_destination(start_url, destination, *, observed=False):
    start = urlsplit(start_url)
    target = urlsplit(destination)
    return (
        target.scheme == "https"
        and target.netloc == "www.dataannotation.tech"
        and not target.query and not target.fragment
        and (target.path == start.path or
             target.path == CANONICAL_REDIRECTS.get(start.path))
    )


def fetch_page(url):
    current = url
    observed_redirect = None
    for hop in range(2):
        request = Request(current, headers=REQUEST_HEADERS)
        try:
            scope = (observed_dataannotation_redirect(url, observed_redirect)
                     if observed_redirect is not None else nullcontext())
            with scope:
                if current_source() is None:
                    response = build_opener(_NoRedirect()).open(request, timeout=30)
                else:
                    response = open_catalog(request, timeout=30)
            with response:
                final_url = response.geturl()
                if not _allowed_destination(url, final_url, observed=observed_redirect == final_url):
                    raise ValueError("DataAnnotation response left canonical scope.")
                charset = response.headers.get_content_charset() or "utf-8"
                text = response.read().decode(charset, errors="replace")
                if response.status != 200:
                    raise RuntimeError(f"DataAnnotation returned HTTP {response.status}.")
                return {"ok": True, "text": text, "url": final_url,
                        "reason": "HTTP 200", "outcome": "success"}
        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                destination = urljoin(current, exc.headers.get("Location", ""))
                if hop == 0 and _allowed_destination(url, destination, observed=True) and destination != current:
                    # The context manager checks exact fixed-origin and destination
                    # conditions before the audited second request is dispatched.
                    with observed_dataannotation_redirect(url, destination):
                        pass
                    observed_redirect = destination
                    current = destination
                    continue
                raise ValueError("DataAnnotation redirect left canonical scope or exceeded hop limit.") from exc
            outcome = "not_found" if exc.code == 404 else "network_or_site_error"
            return {"ok": False, "text": "", "reason": f"HTTP {exc.code}", "outcome": outcome}
        except (URLError, TimeoutError) as exc:
            return {"ok": False, "text": "", "reason": str(exc), "outcome": "network_or_site_error"}
    raise ValueError("DataAnnotation redirect exceeded hop limit.")


def has_application_surface(text, slug):
    normalized = text.lower()
    if any(token in normalized for token in ("403 forbidden", "access denied", "captcha", "page not found")):
        return False
    role_terms = {
        "coding": ("coding", "software engineer"),
        "generalist": ("generalist",), "law": ("law", "legal"),
        "math": ("math",), "medicine": ("medicine", "medical"),
        "physics": ("physics",), "finance": ("finance",),
        "accounting": ("accounting",), "bilingual": ("bilingual",),
        "chemistry": ("chemistry",), "biology": ("biology",),
    }
    return (
        any(term in normalized for term in role_terms[slug])
        and "dataannotation" in normalized
        and "app.dataannotation.tech" in normalized
        and ("apply" in normalized or "sign up" in normalized)
    )


def coding_role_evidence(text, url):
    """Require the role body and role-bound signup in the observed Webflow page."""
    if url != "https://www.dataannotation.tech/job-board/software-engineer":
        raise ValueError("DataAnnotation coding destination is not canonical.")
    if not re.search(r'data-wf-item-slug=["\']software-engineer["\']', text):
        raise ValueError("DataAnnotation coding role slug is missing.")
    decoded = html.unescape(text)
    title = re.search(r'<div class="rd-title">\s*<h1>([^<]+)</h1>', decoded)
    if not title or title.group(1).strip() != "Software Engineer":
        raise ValueError("DataAnnotation coding role title is missing.")
    for href in re.findall(r'href="([^"]+)"', decoded):
        application = html.unescape(href)
        parsed = urlsplit(application)
        query = parse_qs(parsed.query)
        if (parsed.scheme == "https" and parsed.netloc == "app.dataannotation.tech"
                and parsed.path == "/worker_signup" and not parsed.fragment
                and query.get("utm_role") == ["software-engineer"]
                and any(value.startswith("role_") and value.endswith("software-engineer")
                        for value in query.get("utm_content", []))):
            return title.group(1).strip(), application
    raise ValueError("DataAnnotation role-bound application link is missing.")


def evergreen_role_evidence(domain_slug, text, url):
    """Verify one exact observed role, its remote condition and signup action."""
    identity = ROLE_IDENTITIES.get(domain_slug)
    if identity is None:
        raise ValueError("DataAnnotation role lacks a reviewed identity.")
    role_slug, expected_title = identity
    if url != f"https://www.dataannotation.tech/job-board/{role_slug}":
        raise ValueError("DataAnnotation role destination is not canonical.")
    if not re.search(r'data-wf-item-slug=["\']' + re.escape(role_slug) + r'["\']', text):
        raise ValueError("DataAnnotation role slug is missing.")
    decoded = html.unescape(text)
    heading = re.search(r'<div class="rd-title">\s*<h1>([^<]+)</h1>', decoded)
    if not heading or heading.group(1).strip() != expected_title:
        raise ValueError("DataAnnotation role heading disagrees with the observed identity.")
    if (not re.search(r'<div class="rail-row"><span>Location</span><span>Remote</span></div>', decoded)
            or not re.search(r'<section class="rd-section"><h2>Overview</h2>', decoded)
            or not re.search(r'\b(?:AI|models)\b', decoded, re.I)):
        raise ValueError("DataAnnotation remote AI role evidence is missing.")
    for href in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>\s*Apply now\s*</a>', decoded):
        application = html.unescape(href)
        parsed = urlsplit(application)
        query = parse_qs(parsed.query)
        if (parsed.scheme == "https" and parsed.netloc == "app.dataannotation.tech"
                and parsed.path == "/worker_signup" and not parsed.fragment
                and query.get("utm_role") == [role_slug]
                and any(value.startswith("role_") and value.endswith(role_slug)
                        for value in query.get("utm_content", []))):
            return expected_title, application
    raise ValueError("DataAnnotation role-bound application link is missing.")


def parse_domain_page(domain, url, text):
    if domain.slug == "coding":
        title, application_url = coding_role_evidence(text, url)
        return JobCandidate(
            external_id="dataannotation::coding", title=title,
            location=resolve_location(text), url=url,
            department=domain.category, expertise=domain.category,
            opportunity_kind=OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
            availability_basis=AVAILABILITY_BASIS_EVERGREEN_PAGE,
            include_in_live_market_estimate=False,
            source_body=text, source_body_format="text/html",
            source_metadata={"application_url": application_url},
            record_promotion_attestation=RecordPromotionAttestation(
                contract_id="dataannotation_coding_evergreen_record_v1",
                body_observation=BODY_OBSERVATION_PRESENT,
                authority_evidence={
                    "requested_url": "https://www.dataannotation.tech/coding",
                    "final_url": url,
                    "role_slug": "software-engineer",
                    "title": title,
                    "application_url": application_url,
                    "body_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                },
            ),
        )
    title, application_url = evergreen_role_evidence(domain.slug, text, url)
    return JobCandidate(
        external_id=f"dataannotation::{domain.slug}",
        title=title,
        location="Remote",
        url=url,
        department=domain.category,
        expertise=domain.category,
        opportunity_kind=OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
        availability_basis=AVAILABILITY_BASIS_EVERGREEN_PAGE,
        include_in_live_market_estimate=False,
        source_body=text if text.strip() else None,
        source_body_format="text/html" if text.strip() else None,
        source_metadata={"application_url": application_url},
        record_promotion_attestation=RecordPromotionAttestation(
            contract_id="dataannotation_evergreen_role_record_v2",
            body_observation=BODY_OBSERVATION_PRESENT,
            authority_evidence={
                "requested_url": build_domain_url("https://www.dataannotation.tech", domain.slug),
                "final_url": url,
                "role_slug": ROLE_IDENTITIES[domain.slug][0],
                "title": title,
                "application_url": application_url,
                "body_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            },
        ),
    )


def resolve_location(text):
    if "remote" in text.lower():
        return "Remote"
    return "Unknown"
