"""Display already-prepared source wording only on signed-in detail pages."""
from html import escape
import json

from wahojobs.crawler.provider_details import DETAIL_KEY, validate_detail_url


def append_authenticated_source_detail(content, job, *, authenticated):
    if not authenticated:
        return content
    try:
        metadata = json.loads(job.get("rich_metadata_json") or "{}")
        detail = metadata[DETAIL_KEY]
        provider = job["company_slug"]
        external_id = job["external_id"]
        validate_detail_url(provider, external_id, detail["url"])
        if (detail["external_id"] != external_id or detail["provider"] != provider
                or detail.get("version") != 1 or not isinstance(detail.get("display_text"), str)
                or not detail["display_text"].strip()):
            return content
        text = detail["display_text"]
        observed_at = detail["observed_at"]
    except (KeyError, TypeError, ValueError):
        return content
    section = (
        "<section class='content-section' aria-labelledby='captured-source-detail'>"
        "<h2 id='captured-source-detail'>Original opportunity details</h2>"
        f"<p>Source captured {escape(observed_at)}. "
        "This does not verify that an application will be accepted.</p>"
        f"<p><a href='{escape(detail['url'], quote=True)}' rel='noopener noreferrer'>Original source page</a></p>"
        f"<div style='white-space:pre-wrap;overflow-wrap:anywhere'>{escape(text)}</div></section>"
    )
    marker = "<div class='job-description'>"
    return content.replace(marker, marker + section, 1)
