from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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

        if not has_application_surface(page["text"]):
            skipped.append(f"{domain.slug} (missing apply surface)")
            continue

        jobs.append(parse_domain_page(domain, url, page["text"]))

    if network_or_site_errors:
        raise RuntimeError(
            "DataAnnotation crawl failed: "
            f"{len(network_or_site_errors)} allowlisted page(s) had "
            "network/site errors."
        )

    return jobs, skipped


def build_domain_url(base_url, slug):
    return f"{base_url.rstrip('/')}/{slug}"


def fetch_page(url):
    request = Request(url, headers=REQUEST_HEADERS)
    try:
        with urlopen(request, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            text = response.read().decode(charset, errors="replace")
            if response.status == 200:
                return {
                    "ok": True,
                    "text": text,
                    "reason": response.status,
                    "outcome": "success",
                }
            return {
                "ok": False,
                "text": text,
                "reason": f"HTTP {response.status}",
                "outcome": "network_or_site_error",
            }
    except HTTPError as exc:
        outcome = "not_found" if exc.code == 404 else "network_or_site_error"
        return {"ok": False, "text": "", "reason": f"HTTP {exc.code}", "outcome": outcome}
    except URLError as exc:
        return {
            "ok": False,
            "text": "",
            "reason": str(exc.reason),
            "outcome": "network_or_site_error",
        }
    except TimeoutError:
        return {
            "ok": False,
            "text": "",
            "reason": "timeout",
            "outcome": "network_or_site_error",
        }


def has_application_surface(text):
    normalized = text.lower()
    return (
        "app.dataannotation.tech" in normalized
        or ("apply" in normalized and "dataannotation" in normalized)
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
    )


def resolve_location(text):
    if "remote" in text.lower():
        return "Remote"
    return "Unknown"
