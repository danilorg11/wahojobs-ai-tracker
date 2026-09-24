from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from wahojobs.crawler.local_inventory import open_catalog
from wahojobs.daily_source_policy import current_source

from wahojobs.classification import (
    AVAILABILITY_BASIS_EVERGREEN_PAGE,
    OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
)
from wahojobs.crawler.types import JobCandidate


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

    for domain in DOMAIN_PAGES:
        url = build_domain_url(base_url, domain.slug)
        page = fetch_page(url)
        if not page["ok"]:
            skipped.append(f"{domain.slug} ({page['reason']})")
            if page["outcome"] == "network_or_site_error":
                network_or_site_errors.append(domain.slug)
            continue

        if not has_application_surface(page["text"], domain.slug):
            skipped.append(f"{domain.slug} (missing apply surface)")
            continue

        jobs.append(parse_domain_page(domain, page["url"], page["text"]))

    if network_or_site_errors:
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
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _allowed_destination(start_url, destination):
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
    for hop in range(2):
        request = Request(current, headers=REQUEST_HEADERS)
        try:
            if current_source() is None:
                response = build_opener(_NoRedirect()).open(request, timeout=30)
            else:
                response = open_catalog(request, timeout=30)
            with response:
                final_url = response.geturl()
                if not _allowed_destination(url, final_url):
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
                if hop == 0 and _allowed_destination(url, destination) and destination != current:
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


def parse_domain_page(domain, url, text):
    return JobCandidate(
        external_id=f"dataannotation::{domain.slug}",
        title=domain.title,
        location=resolve_location(text),
        url=url,
        department=domain.category,
        expertise=domain.category,
        opportunity_kind=OPPORTUNITY_KIND_EVERGREEN_APPLICATION,
        availability_basis=AVAILABILITY_BASIS_EVERGREEN_PAGE,
        include_in_live_market_estimate=False,
        source_body=text if text.strip() else None,
        source_body_format="text/html" if text.strip() else None,
    )


def resolve_location(text):
    if "remote" in text.lower():
        return "Remote"
    return "Unknown"
