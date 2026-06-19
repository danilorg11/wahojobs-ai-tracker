import hashlib
import re

from wahojobs.crawler.types import JobCandidate


def normalize_text(value):
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def with_source_hash(company_slug, candidate):
    identity = candidate.external_id or "|".join(
        [
            company_slug,
            normalize_text(candidate.title),
            normalize_text(candidate.location),
            normalize_text(candidate.url),
        ]
    )
    source_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return JobCandidate(
        title=candidate.title.strip(),
        location=(candidate.location or "").strip(),
        url=candidate.url.strip(),
        external_id=candidate.external_id,
        department=candidate.department,
        expertise=candidate.expertise,
        commitment=candidate.commitment,
        opportunity_kind=candidate.opportunity_kind,
        availability_basis=candidate.availability_basis,
        include_in_live_market_estimate=candidate.include_in_live_market_estimate,
        source_hash=source_hash,
    )
