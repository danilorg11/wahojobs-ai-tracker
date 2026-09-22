import json
import re
from collections import Counter
from urllib.request import Request
from wahojobs.crawler.local_inventory import open_catalog as urlopen

from wahojobs.crawler.types import (
    BODY_OBSERVATION_NOT_OBSERVED, BODY_OBSERVATION_PRESENT,
    MERCOR_RECORD_CONTRACT_ID, CompanyCrawlResult, JobCandidate,
    ProviderOutcome, RecordPromotionAttestation,
)
from wahojobs.crawler.source_content import first_text, nonempty_metadata, selected_metadata
from wahojobs.matching.source_geography import (
    DESCRIPTION_GEOGRAPHY_KEY, prepare_mercor_description_geography,
)


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "application/json",
    "rid": "w-wahojobs",
    "X-Client-IP": "true",
}
MERCOR_ENDPOINT = "https://aws.api.mercor.com/work/listings-explore-page"
MERCOR_PAYLOAD_SHAPE = "mercor-marketplace:listings:v1"
MERCOR_SCHEMA_FINGERPRINT = "mercor-public-active-record:v1"


def fetch_mercor_listings(api_url):
    return fetch_mercor_observations(api_url).jobs


def fetch_mercor_observations(api_url):
    if api_url != MERCOR_ENDPOINT:
        raise ValueError("Mercor record observations require the configured public endpoint.")
    request = Request(api_url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=30) as response:
        if response.geturl() != MERCOR_ENDPOINT:
            raise ValueError("Mercor response did not come from the public endpoint.")
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="replace")
    data = json.loads(payload)
    from wahojobs.crawler.local_inventory import record_envelope_shape
    record_envelope_shape(data)
    return parse_mercor_observations(data)


def parse_mercor_observations(data):
    """A returned active record is evidence; listing completeness is unknown."""
    listings = data.get("listings") if isinstance(data, dict) else None
    if not isinstance(listings, list):
        raise ValueError("Mercor response did not include a listings list.")
    identities = Counter(
        listing["listingId"] for listing in listings
        if isinstance(listing, dict) and isinstance(listing.get("listingId"), str)
    )
    # Withhold all copies of duplicate identities, including conflicting status.
    duplicates = {identity for identity, count in identities.items() if count > 1}
    jobs = [parse_mercor_listing(listing, attest=True) for listing in listings
            if should_include_listing(listing) and listing["listingId"] not in duplicates]
    return CompanyCrawlResult(
        jobs=jobs, used_sample_data=False, source_type="mercor-marketplace",
        source_message="Observed public active Mercor records; complete inventory is not established.",
        outcome=ProviderOutcome.PARTIAL, snapshot_complete=False,
        pagination_complete=False, empty_snapshot_validated=False,
        payload_shape=MERCOR_PAYLOAD_SHAPE, schema_fingerprint=MERCOR_SCHEMA_FINGERPRINT,
        raw_record_count=len(listings), normalized_record_count=len(jobs),
        rejected_record_count=len(listings) - len(jobs),
        warnings=("Partial listing response cannot authorize removal of absent records.",),
    )


def should_include_listing(listing):
    return (
        isinstance(listing, dict)
        and listing.get("status") == "active"
        and "deletedAt" in listing
        and listing.get("deletedAt") is None
        and listing.get("isPrivate") is False
        and isinstance(listing.get("listingId"), str)
        and re.fullmatch(r"[A-Za-z0-9_-]+", listing["listingId"]) is not None
        and isinstance(listing.get("title"), str)
        and bool(listing["title"].strip())
    )


def parse_mercor_listing(listing, *, attest=False):
    listing_id = clean_value(listing.get("listingId"))
    listing_domain = clean_value(listing.get("listingDomain")) or "Unknown"
    source_body = first_text(
        listing,
        ("description", "jobDescription", "descriptionText", "details"),
    )
    body_field = next((field for field in ("description", "jobDescription", "descriptionText", "details")
                       if source_body is not None and listing.get(field) == source_body), None)
    description_geography = prepare_mercor_description_geography(source_body, body_field, listing)
    return JobCandidate(
        external_id=listing_id,
        title=clean_value(listing.get("title")),
        location=clean_value(listing.get("location")) or "Remote",
        url=f"https://work.mercor.com/jobs/{listing_id}",
        department=listing_domain,
        expertise=listing_domain,
        commitment=clean_value(listing.get("commitment")),
        source_body=source_body,
        source_body_format="text/plain" if source_body else None,
        source_metadata=nonempty_metadata(
            {**selected_metadata(
                listing,
                (
                    "listingDomain",
                    "skills",
                    "jobTypes",
                    "responsibilities",
                    "requirements",
                    "qualifications",
                    "payRate",
                    "payRateFrequency",
                    "eligibleLocation",
                    "eligibleResidenceLocation",
                    "ineligibleLocation",
                    "ineligibleResidenceLocation",
                ),
            ), **({DESCRIPTION_GEOGRAPHY_KEY: description_geography} if description_geography else {})}
        ),
        source_updated_at=clean_value(listing.get("updatedAt")),
        record_promotion_attestation=(RecordPromotionAttestation(
            contract_id=MERCOR_RECORD_CONTRACT_ID,
            body_observation=BODY_OBSERVATION_PRESENT if source_body else BODY_OBSERVATION_NOT_OBSERVED,
            authority_evidence={
                "authoritative_endpoint": MERCOR_ENDPOINT,
                "listing_id": listing_id,
                "status": listing.get("status"),
                "deleted_at": listing.get("deletedAt"),
                "is_private": listing.get("isPrivate"),
                "required_record_shape_validated": should_include_listing(listing),
            },
        ) if attest else None),
    )


def clean_value(value):
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None
