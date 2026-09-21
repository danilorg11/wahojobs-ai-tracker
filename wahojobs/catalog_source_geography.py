"""Present accepted applicant-location clauses without a candidate profile.

Location and residence are separate dimensions. The country facet addresses
current location when that dimension is present; otherwise it addresses an
explicit residence requirement. It never certifies both for a person.
"""
from copy import deepcopy

from wahojobs.matching.source_geography import apply_mercor_applicant_geography


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
