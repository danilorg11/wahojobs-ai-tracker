"""Present accepted applicant-location clauses without a candidate profile.

Location and residence are separate dimensions. The country facet addresses
current location when that dimension is present; otherwise it addresses an
explicit residence requirement. It never certifies both for a person.
"""
from copy import deepcopy
import re
from urllib.parse import urlsplit

from wahojobs.matching.source_geography import apply_mercor_applicant_geography


def prepare_public_geography(job, text):
    """Recover retained role facts once; never alter matching or source records.

    Lever's workplaceType is distinct from its office/country metadata. A
    bounded applicant clause may fill missing geography, never a locale or
    office. Existing accepted dimensions, scoped facts and overrides win.
    """
    from wahojobs.public_job_page import candidate_job_eligibility, natural_join
    from wahojobs.catalog_source_presentation import bound_metadata
    from wahojobs.matching.source_geography import (_description_countries,
                                                   prepare_applicant_residence_clause)
    result = dict(candidate_job_eligibility(job))
    # Accepted Mercor clauses already resolve current location, residence and
    # exclusions. Older generic normalized facts cannot replace that packet.
    if result.get('applicant_geography_basis') == 'accepted_source':
        return result
    overrides = set(job.get('overridden_fields') or ())
    guarded = overrides | {
        fact.get('field_path') for fact in job['enrichment'].get('variant_facts', [])}
    prefix = 'attributes.work_arrangement.'
    arrangement = job['enrichment']['attributes']['work_arrangement']
    if prefix + 'workplace_mode' in guarded:
        result['mode'] = arrangement.get('workplace_mode') or 'unknown'
    if prefix + 'location_scope' in guarded:
        result['scope'] = arrangement.get('location_scope') or 'unknown'
    for field, key in (('eligible_countries','countries'), ('eligible_regions','regions')):
        if prefix + field in guarded:
            result[key] = tuple(arrangement.get(field) or ())
    metadata = bound_metadata(job)
    from wahojobs.catalog_source_links import oneforma_work_facts
    oneforma = oneforma_work_facts(job, text)
    if oneforma.get('mode') and result['mode'] == 'unknown' and prefix + 'workplace_mode' not in guarded:
        result['mode'] = oneforma['mode']
    geography_fields = {prefix + field for field in
        ('location_scope', 'eligible_countries', 'eligible_regions', 'eligible_locations')}
    # Automatic bootstrap copied project tags into every variant. Replace only
    # that exact unquoted fallback, never a manual or evidenced scoped decision.
    from wahojobs.matching.locations import countries_in_location
    project_countries = countries_in_location(job.get('source_location'))
    protected_geography = bool(overrides & geography_fields) or any(
        fact.get('field_path') in geography_fields and (fact.get('evidence') or
            fact.get('knowledge_state') != 'known_value')
        for fact in job['enrichment'].get('variant_facts', []))
    if (oneforma.get('country') and not protected_geography and not result['regions']
            and set(result['countries']) == set(project_countries)
            and arrangement.get('location_scope') in (None, '', 'unknown')):
        result.update(countries=(oneforma['country'],), regions=(), detail_countries=(),
            scope='remote_restricted' if result['mode'] == 'remote' else 'onsite_or_hybrid_restricted',
            applicant_geography_basis='retained_application_option', country_filter_dimension='location')
    lever = bool(metadata and urlsplit(job.get('listing_url') or '').hostname == 'jobs.lever.co')
    if (lever and result['mode'] == 'unknown' and prefix + 'workplace_mode' not in guarded
            and metadata.get('workplaceType') in ('remote', 'hybrid', 'on-site')):
        result['mode'] = {'on-site': 'onsite'}.get(metadata['workplaceType'], metadata['workplaceType'])
        if (result['mode'] == 'remote' and re.search(
                r'\b(?:engagement type\s*:\s*onsite project|full[- ]day onsite sessions)\b', text, re.I)):
            result.update(mode='unknown', workplace_conflict=True)

    geography_guarded = any(prefix + field in guarded for field in
        ('location_scope', 'eligible_countries', 'eligible_regions', 'eligible_locations'))
    # Recover only exact applicant clauses, not arbitrary country mentions.
    clauses, unresolved_restriction = [], False
    mandatory = re.compile(r'^(?:(?:Applicants|Candidates|Contributors|Workers|You) '
        r'(?:must|are required to)|Must)\b.{0,35}\b(?:based|located|resid\w*|liv\w*)\b', re.I)
    if (lever and not geography_guarded) or result['scope'] == 'remote_worldwide':
        for block in re.split(r'\n+|(?<=[.!?])\s+', text):
            block = block.strip(' *#-')
            # The shared parser expects a qualification, not an arbitrary body
            # sentence. Bare "Based in Malaysia" may describe an office.
            required = bool(mandatory.search(block))
            clause = (prepare_applicant_residence_clause(block, 'required', 'retained_role_description')
                      if required else None)
            if required and (not clause or clause.get('unresolved')):
                unresolved_restriction = True
            if not clause:
                match = re.fullmatch(r"We(?:'re|’re| are) looking for [\w ,()—–-]+? "
                    r'(?P<place>living|residing) in (?P<countries>.+?) to .+', block, re.I)
                if lever and match and not re.search(r'\b(?:not|except|unless|preferred|if)\b', block, re.I):
                    countries, unresolved = _description_countries(match['countries'])
                    clause = dict(countries=countries, unresolved=unresolved, dimension='residence')
            if clause and clause['countries'] and not clause['unresolved']:
                clauses.append(clause)
        if (unresolved_restriction or (clauses and geography_guarded)) and result['scope'] == 'remote_worldwide':
            # A global claim cannot overrule a contrary applicant condition.
            # Preserve manual/scoped source data; withhold that public claim.
            result.update(scope='unknown', countries=(), regions=(),
                          geography_unresolved=True,
                          summary='Location requirements need confirmation',
                          fact='Location requirements need confirmation')
            return result
        if clauses and not geography_guarded and not result.get('applicant_country_dimensions'):
            dimensions = {c['dimension'] for c in clauses}
            allowed = set.intersection(*(set(c['countries']) for c in clauses))
            existing = set(result['countries'])
            if existing:
                allowed &= existing
            if len(dimensions) == 1 and allowed and not result['regions']:
                result.update(countries=tuple(sorted(allowed)),
                    country_filter_dimension=next(iter(dimensions)),
                    applicant_geography_basis='retained_applicant_clause')
            elif not allowed:
                result.update(countries=(), regions=(), scope='unknown',
                              geography_unresolved=True,
                              summary='Location requirements need confirmation',
                              fact='Location requirements need confirmation')
                return result

    countries, regions = result['countries'], result['regions']
    if countries or regions:
        if result['scope'] == 'remote_worldwide' or (not geography_guarded and result['mode'] == 'remote'):
            result['scope'] = 'remote_restricted'
        elif not geography_guarded and result['scope'] == 'unknown':
            result['scope'] = 'onsite_or_hybrid_restricted'  # restriction scope, not a mode assertion
        labels = regions or countries
        summary = natural_join(labels) if len(labels) <= 3 else str(len(labels)) + ' countries'
        result.update(summary=summary, fact=natural_join(labels),
                      detail_countries=countries if len(countries) > 4 else ())
    elif result['scope'] == 'remote_worldwide':
        # A normalized empty restriction list is not proof of global availability.
        # Require the role's own listing location or an accepted scoped quote.
        worldwide = re.compile(r'\b(?:world\s*wide|anywhere (?:in the world|worldwide))\b', re.I)
        quotes = [job.get('source_location') or ''] + [str(e.get('evidence_text') or '')
            for f in job['enrichment'].get('variant_facts', [])
            if f.get('field_path') == prefix + 'location_scope'
            for e in f.get('evidence', [])]
        global_role_location = (job.get('source_location') or '').strip().casefold() == 'global'
        if global_role_location or any(worldwide.search(q) for q in quotes):
            result.update(summary='Worldwide', fact='Worldwide')
        else:
            result.update(scope='unknown', summary=None, fact=None)
    elif result.get('summary', '') and result['summary'].startswith(('Eligible in ', 'Eligible across ')):
        # A listing city alone can be an office, not an applicant restriction.
        result.update(summary=None, fact=None)
    elif result.get('summary') == 'Work from anywhere':
        result.update(summary=None, fact=None)  # scoped unknown suppressed the source fallback
    return result


def attach_catalog_geography(connection, rows):
    copied = [dict(row, source_slug=row.get('company_slug', row.get('source_slug'))) for row in rows]
    return apply_mercor_applicant_geography(connection, copied)


def mercor_candidate_eligibility(job, base):
    if job.get('company_slug') != 'mercor':
        return None
    raw = str(job.get('source_location') or '').strip()
    result = dict(base, scope='unknown', countries=(), regions=(), detail_countries=(),
                  summary='Applicant location unconfirmed', fact='Applicant location unconfirmed',
                  applicant_geography_basis='accepted_source',
                  country_filter_dimension=None, dimension_details=[],
                  source_wording=raw if raw.casefold() != 'remote' else None)
    requirements = job.get('applicant_country_requirements') or []
    proof = job.get('applicant_geography_evidence') or {}
    if not requirements or (proof.get('job_id') != job.get('job_id')
            or proof.get('source_url') != job.get('listing_url')):
        # Mercor's free-text location includes preferences and alternatives.
        # Only its accepted applicant fields establish a country requirement.
        return result
    dimensions = {}
    uncertain = False
    for dimension in ('location', 'residence'):
        clauses = [r for r in requirements if r.get('dimension') == dimension]
        if not clauses:
            continue
        allows = [set(r.get('countries') or []) for r in clauses if r.get('mode') == 'allow']
        excluded = set().union(*(set(r.get('countries') or []) for r in clauses if r.get('mode') == 'exclude'))
        allowed = set.intersection(*allows) - excluded if allows else set()
        unresolved = any(r.get('unresolved') or r.get('ambiguous_statement') or
                         r.get('source_conflict') for r in clauses)
        conflict = bool(allows and not allowed)
        uncertain |= unresolved or conflict
        dimensions[dimension] = dict(allowed=tuple(sorted(allowed)), excluded=tuple(sorted(excluded)),
            unresolved=unresolved, conflict=conflict, clauses=deepcopy(clauses))
        label = 'Current location' if dimension == 'location' else 'Residence'
        if allows:
            result['dimension_details'].append((label, ', '.join(sorted(set.intersection(*allows))) or 'Conflicting requirements'))
        if excluded:
            result['dimension_details'].append((label + ' exclusions', ', '.join(sorted(excluded))))
        if unresolved:
            result['dimension_details'].append((label, 'Some source requirements are unclear; confirm with the employer.'))
    # A country selection refers to one dimension; do not union/intersect
    # current location with residence, which can differ for cross-border work.
    selected_dimension = 'location' if 'location' in dimensions else 'residence'
    selected = dimensions.get(selected_dimension, {})
    countries = () if uncertain else selected.get('allowed', ())
    result.update(scope='remote_restricted' if base.get('mode') == 'remote' else 'onsite_or_hybrid_restricted',
                  countries=countries, country_filter_dimension=selected_dimension,
                  applicant_country_dimensions=dimensions)
    if uncertain:
        result.update(summary='Location requirements need confirmation', fact='Location requirements need confirmation')
    elif countries:
        label = 'Current location' if selected_dimension == 'location' else 'Required residence'
        summary = label + ': ' + (', '.join(countries) if len(countries) <= 3 else str(len(countries)) + ' countries')
        if selected_dimension == 'location' and 'residence' in dimensions:
            summary += ' · residence requirements also apply'
        result.update(summary=summary, fact=summary)
    else:
        result.update(summary='Location restrictions apply', fact='Location restrictions apply')
    return result
