import json
from urllib.request import Request, urlopen

from wahojobs.crawler.types import JobCandidate


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "application/json",
    "rid": "w-wahojobs",
    "X-Client-IP": "true",
}


def fetch_mercor_listings(api_url):
    request = Request(api_url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="replace")

    data = json.loads(payload)
    listings = data.get("listings") if isinstance(data, dict) else None
    if not isinstance(listings, list):
        raise ValueError("Mercor response did not include a listings list.")

    return [
        parse_mercor_listing(listing)
        for listing in listings
        if should_include_listing(listing)
    ]


def should_include_listing(listing):
    return (
        listing.get("status") == "active"
        and listing.get("deletedAt") is None
        and listing.get("isPrivate") is False
        and bool(clean_value(listing.get("listingId")))
        and bool(clean_value(listing.get("title")))
    )


def parse_mercor_listing(listing):
    listing_id = clean_value(listing.get("listingId"))
    listing_domain = clean_value(listing.get("listingDomain")) or "Unknown"
    return JobCandidate(
        external_id=listing_id,
        title=clean_value(listing.get("title")),
        location=clean_value(listing.get("location")) or "Remote",
        url=f"https://work.mercor.com/jobs/{listing_id}",
        department=listing_domain,
        expertise=listing_domain,
        commitment=clean_value(listing.get("commitment")),
    )


def clean_value(value):
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None
