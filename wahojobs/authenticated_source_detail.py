"""Display already-prepared source wording only on signed-in detail pages."""
from html import escape
import json
from urllib.parse import urlencode

from wahojobs.crawler.provider_details import DETAIL_KEY, validate_detail_url
from wahojobs.opportunity_enrichment import source_body_paragraphs
from wahojobs.matching.opportunity_trust import INACTIVE, STALE_SOURCE, UNVERIFIED_SOURCE
from wahojobs.public_job_page import PUBLIC_JOB_STATE_LIVE


def _availability_message(job):
    """Explain prepared evidence, without evaluating freshness or enabling actions."""
    trust = job.get('availability_trust') or {}
    reason = trust.get('status')
    # Older retained detail packets may have flags but no prepared assessment.
    # An explicitly inactive record must never be described as merely stale.
    if (reason == INACTIVE or job.get('job_is_active') in (False, 0)
            or job.get('canonical_is_active') in (False, 0)):
        return ('Listing marked inactive',
                'This listing is marked inactive in our saved records. '
                'The saved description is shown for reference.')
    if reason == STALE_SOURCE:
        return ('Availability needs rechecking',
                'We haven’t recently verified whether this opportunity is still available. '
                'The saved description is shown for reference.')
    if reason == UNVERIFIED_SOURCE:
        return ('Availability not verified',
                'We don’t have a qualifying verification that this opportunity is available. '
                'The saved description is shown for reference.')
    return ('Availability not established',
            'The saved information does not establish whether this opportunity is available. '
            'The saved description is shown for reference.')


def prepare_detail_display(job, profile):
    """Use the same exact-source display packet as cards, with no new reads."""
    from wahojobs.authenticated_card_evidence import prepare_card_evidence
    source = {
        'job_id': job['job_id'], 'canonical_opportunity_id': job['canonical_opportunity_id'],
        'external_id': job['external_id'], 'url': job['official_url'],
        'source_slug': job['company_slug'], 'commitment': job.get('source_commitment'),
        'location': job.get('source_location'), 'content_external_id': job.get('rich_external_id'),
        'source_url': job.get('rich_source_url'), 'body': job.get('rich_body'),
        'body_format': job.get('rich_body_format'), 'metadata_json': job.get('rich_metadata_json'),
        'last_captured_at': job.get('last_captured_at'), 'material_content_sha256': job.get('material_content_sha256'),
    }
    if job.get('rich_provider') != job['company_slug']:
        return None
    local = job.get('_authenticated_local_checks') or {}
    match = dict(local.get('match') or job.get('_authenticated_recommendation') or {})
    match.update({k: source[k] for k in ('job_id', 'canonical_opportunity_id', 'url', 'source_slug')})
    conditional = ((job.get('_authenticated_recommendation') or {}).get('_detail_recommendation_section')
                   == 'conditional' and job['public_state'] == PUBLIC_JOB_STATE_LIVE)
    prepared = local.get('background_card_evidence')
    if prepared is not None:
        from copy import deepcopy
        from wahojobs.professional_background_semantics import digest
        from wahojobs.authenticated_card_evidence import _conditional_source_explanation
        if (local.get('background_profile_digest') == digest(profile)
                and all(prepared.get(k) == source.get(s) for k, s in (
                    ('job_id', 'job_id'), ('external_id', 'external_id'), ('url', 'url'),
                    ('source_hash', 'material_content_sha256')))):
            prepared = deepcopy(prepared)
            if conditional:
                prepared['placement_explanation'] = _conditional_source_explanation(match, prepared)
            return prepared
    return prepare_card_evidence(match, source, profile, include_item_experience=True,
                                 conditional_placement=conditional)


def render_authenticated_job_page(job, *, profile, navigation, workflow_controls='',
                                  workflow_status='', catalog_return_to=None, return_run_id=None):
    """Normal signed-in job page. Availability and recommendation proof are inputs.

    Local eligibility is not list membership. Neither is inferred by this view;
    the resolver's diagnostic fields remain intact on ``job``.
    """
    from wahojobs import public_job_page as public
    from wahojobs.candidate_source_display import DISPLAY_CSS, markdown
    from wahojobs.authenticated_card_evidence import _blocks, _QUALIFICATION_HEADINGS, render_location_context, render_placement_explanation
    from wahojobs.candidate_condition_comparisons import render_comparisons
    from wahojobs.profile_opportunity_navigation import render_profile_update
    from wahojobs.authenticated_variant_details import variant_detail_url
    packet = prepare_detail_display(job, profile)
    current = job['public_state'] == public.PUBLIC_JOB_STATE_LIVE
    recommended = job.get('_authenticated_recommendation') is not None and current
    title = job.get('source_title') or job.get('canonical_title') or 'Opportunity'
    company = job.get('company_name') or ''
    url = public.first_human_facing_url(job.get('official_url'))
    facts = public.render_fact_grid(packet['facts']) if packet else ''
    kind = packet['kind'] if packet else ''
    if kind == 'Opportunity type not established':
        kind = ''
    if kind == 'Advertised role/project':
        kind = 'Advertised opportunity'
    kind_html = f"<p class='candidate-kind'>{escape(kind)}</p>" if kind else ''
    if kind == 'Talent network — future consideration':
        kind_html += "<p class='candidate-note'>Join for future projects; this is not a specific job posting.</p>"
    status = ''
    if not current:
        heading, message = _availability_message(job)
        status = (f"<aside class='candidate-status'><strong>{escape(heading)}</strong>"
                  f"<p>{escape(message)}</p></aside>")
    local = job.get('_authenticated_local_checks') or {}
    location = (local.get('match') or job.get('_authenticated_recommendation') or {}).get('location_eligibility_status')
    caveats = list(packet['caveats']) if packet else []
    if not packet:
        if location == 'incompatible':
            caveats.append('The applicant-location restriction conflicts with your profile.')
        elif location != 'eligible':
            country = profile.get('location', {}).get('country')
            caveats.append(f'Eligibility from {country} needs confirmation.' if country else 'Applicant-location eligibility isn’t specified.')
    caveat_html = ''.join('<li>' + escape(c) + '</li>' for c in caveats)
    blocks = _blocks(packet['text']) if packet else []
    qualification_block = next((i for i, block in enumerate(blocks)
                                if block['heading'].casefold().rstrip(':') in _QUALIFICATION_HEADINGS), None)
    qualification_link = ("<p class='candidate-note'><a href='#employer-qualifications'>Review qualifications and comparisons</a></p>"
                          if qualification_block is not None else '')
    checks = ("<section class='candidate-checks'><h2>Before you apply</h2>"
              + render_placement_explanation(packet)
              + (f"<ul class='candidate-caveats'>{caveat_html}</ul>" if caveats else '')
              + render_location_context(packet)
              + qualification_link + '</section>'
              if qualification_link or caveats or render_location_context(packet) else '')
    overview = (f"<p class='candidate-overview'>{escape(packet['summary'])}</p>"
                if packet and packet['summary'] else '')
    description = ''
    if packet:
        # Plain headings from accepted HTML captures receive the same formatting
        # as Markdown headings. No sections or requirements are manufactured.
        from wahojobs.authenticated_card_evidence import render_original_qualifications
        sections = []
        originals = []
        collapse_qualifications = bool(render_placement_explanation(packet))
        qualification_refs = {b['reference'] for b in packet['conditions']}
        for index, block in enumerate(blocks):
            heading, wording = block['heading'], block['text']
            if heading.casefold() == 'other published fields (read alongside the description)':
                # The accepted capture appends these fields for completeness.
                # Present their meaning without the diagnostic labels/JSON repr.
                heading = 'Additional information'
                wording = '\n\n'.join(line for line in wording.splitlines()
                                       if not line.startswith('Published hourly-rate fields:'))
                wording = wording.replace("Source page's structured applicant-location list:", 'Locations listed by the employer:')
            elif heading.casefold() == 'other published fields (not additional applicant requirements)':
                heading = 'Additional information'
                # These are formatter-added field labels, not qualification prose.
                # Hide duplicate raw bounds only when the shared pay view has a rate.
                wording = '\n\n'.join(line for line in wording.splitlines()
                                       if not (packet['pay']['wording'] and
                                               line.startswith(('Hourly range minimum:', 'Hourly range maximum:'))))
                wording = wording.replace('Engagement type:', 'Engagement:')
            anchor = " id='employer-qualifications'" if index == qualification_block else ''
            original = ((f"<h3{anchor}>{escape(heading)}</h3>" if heading != 'Source wording' else '')
                        + markdown(wording))
            comparisons = render_comparisons(packet, block_reference=block['reference'])
            if collapse_qualifications and block['reference'] in qualification_refs:
                # Comparisons, including conflicts, remain outside the collapsed
                # employer wording. The existing fragment target stays on its heading.
                sections.append(comparisons)
                originals.append(original)
            else:
                if originals:
                    sections.append(render_original_qualifications(''.join(originals)))
                    originals = []
                sections.append((f"<h3{anchor}>{escape(heading)}</h3>" if heading != 'Source wording' else '')
                                + comparisons + markdown(wording))
        if originals:
            sections.append(render_original_qualifications(''.join(originals)))
        description = ''.join(sections)
        description = "<section class='content-section source-description'><h2>Employer description</h2>" + description + '</section>'
    else:
        description = "<p>Full requirements aren’t available in the saved listing. Check the original source before applying.</p>"
    pay_wording = ''
    if packet and (len(packet['pay']['wording']) > 1 or packet['pay']['notes']):
        pay_wording = ("<details class='candidate-conditions'><summary>Pay wording from the source</summary><div class='source-description'><ul>"
                       + ''.join('<li>' + escape(p) + '</li>' for p in packet['pay']['wording'])
                       + '</ul></div></details>')
    action = ''
    if url and current and job['job_is_active'] and job['canonical_is_active']:
        action_label = 'Apply on company site' if recommended else 'View source listing'
        action = (f"<a class='button button-primary' href='{escape(url, quote=True)}' target='_blank' "
                  f"rel='noopener noreferrer nofollow'>{action_label}</a>")
    workflow = ''
    if workflow_controls:
        workflow = ("<aside class='workflow-card' data-action-card><h2>My Jobs</h2>"
                    f"<p class='pill js-card-status'>{escape(workflow_status)}</p>"
                    f"<div class='js-card-controls workflow-controls'>{workflow_controls}</div></aside>")
    source_link = (f"<a href='{escape(url, quote=True)}' target='_blank' rel='noopener noreferrer nofollow'>Original listing at {escape(company)}</a>"
                   if url else escape(company))
    back = public.safe_catalog_return_target(catalog_return_to)
    if not back:
        # An anchor restores the candidate's place, not recommendation membership.
        # Only the route may supply a proven-valid, owner-bound run reference.
        back = '/find-matches'
        if return_run_id:
            back += '?' + urlencode({'run': return_run_id})
        back += '#opportunity-' + str(job['job_id'])
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'><meta name='robots' content='noindex,follow'>
<title>{escape(title)} at {escape(company)} | Wahojobs</title>
<style>{public.PUBLIC_JOB_CSS}\n{DISPLAY_CSS}</style></head><body class='candidate-detail'>
<header class='site-header'><a class='brand' href='/jobs'>Wahojobs</a>{navigation}</header>
<main><p class='back-to-jobs'><a href='{escape(back, quote=True)}'>← Back to opportunities</a></p>
<article><header class='hero'><div class='hero-copy'><h1>{escape(title)}</h1>
<p class='company-line'>{escape(company)}</p>{kind_html}{status}{facts}{overview}
{render_comparisons(packet, highlights=True) if packet else ''}
{checks}{render_profile_update(packet, variant_detail_url(job, run_id=return_run_id))}<div class='hero-actions'>{action}</div></div>{workflow}</header>
<div id='action-feedback' aria-live='polite'></div><div class='job-description'>{pay_wording}{description}</div>
<footer class='verification-footer'>{source_link}<p>Based on saved source information. Confirm current terms and application availability with the employer.</p></footer>
</article></main></body></html>"""


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
