"""Display already-prepared source wording only on signed-in detail pages."""
from html import escape
import json

from wahojobs.crawler.provider_details import DETAIL_KEY, validate_detail_url
from wahojobs.opportunity_enrichment import source_body_paragraphs


def append_authenticated_source_detail(content, job, *, authenticated):
    if not authenticated:
        return content
    try:
        metadata = json.loads(job.get("rich_metadata_json") or "{}")
        if not isinstance(metadata, dict):
            return content
        if DETAIL_KEY in metadata:
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
            source_url = detail["url"]
        else:
            # A source may supply its complete body in the accepted listing
            # capture, without a separately recovered detail-page packet.
            # Render this exact variant's wording, never an enrichment summary.
            from wahojobs.public_job_page import first_human_facing_url
            source_url = job.get("rich_source_url")
            if (not job.get("external_id")
                    or job.get("rich_external_id") != job["external_id"]
                    or job.get("rich_provider") != job["company_slug"]
                    or source_url != job.get("official_url")
                    or not first_human_facing_url(source_url)):
                return content
            text = job.get("rich_body")
            if not isinstance(text, str) or not text.strip():
                return content
            if job.get("rich_body_format") == "text/html":
                text = "\n\n".join(source_body_paragraphs(text, "text/html"))
            if not text.strip():
                return content
            observed_at = job.get("last_captured_at") or "not recorded"
    except (KeyError, TypeError, ValueError):
        return content
    section = (
        "<section class='content-section' aria-labelledby='captured-source-detail'>"
        "<h2 id='captured-source-detail'>Original opportunity details</h2>"
        f"<p>Source captured {escape(observed_at)}. "
        "This does not verify that an application will be accepted.</p>"
        "<p>Original source conditions below are unassessed against your profile; "
        "required, preferred and alternative wording is retained.</p>"
        f"<p><a href='{escape(source_url, quote=True)}' rel='noopener noreferrer'>Original source page</a></p>"
        f"<div style='white-space:pre-wrap;overflow-wrap:anywhere'>{escape(text)}</div></section>"
    )
    marker = "<div class='job-description'>"
    return content.replace(marker, marker + section, 1)
