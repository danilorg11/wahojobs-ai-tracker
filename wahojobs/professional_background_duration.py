"""Comparison-only duration bounds; no resume inference or profile mutation.

Confirmed domain-year records declare aggregate years in that exact scope.
Employment intervals and general career totals are deliberately not inputs.
Explicitly qualified bounds use existing confirmed constraint statements; they
never require new profile fields or a semantic model's numeric judgment.
"""
from decimal import Decimal
import math
import re
import unicodedata

VERSION = 'professional_background_components_v2'
# Parser ceilings fit the existing confirmed-label / scalar input envelope.
# Canonical profile validation remains at the established caller boundary.
_MAX_BOUND_TEXT = 128
_MAX_CLAUSE_TEXT = 4096
_NUMBER = r'\d+(?:\.\d+)?'
_RELEVANT = re.compile(
    rf'(?P<minimum>{_NUMBER})\+? (?P<unit>years?|months?) of relevant professional experience in (?P<scope>[\w /,-]{{2,160}})', re.I)
_HANDS_ON = re.compile(
    rf'(?P<minimum>{_NUMBER})\+? (?P<unit>years?|months?) of hands-on (?P<scope>[\w /,-]{{2,160}}) experience', re.I)
_BOUND = re.compile(
    rf'(?:I have )?(?P<bound>a total of|exactly|only|at most|at least) (?P<amount>{_NUMBER}) '
    r'(?P<unit>years?|months?) of (?:relevant )?professional experience in (?P<scope>[\w /,-]{2,160})\.?', re.I)


def scope_key(value):
    return ' '.join(unicodedata.normalize('NFC', value).split()).casefold()


def confirmed_fact(profile, path, value):
    from wahojobs.candidate_condition_comparisons import _fact
    fact = _fact(profile, path, value)
    refs = fact['sources']
    return fact if refs and all(r.get('explicit') is True and r.get('source_kind') in
                               ('user_confirmation', 'user_correction') for r in refs) else None


def requirement(quote):
    if not isinstance(quote, str) or len(quote) > _MAX_CLAUSE_TEXT:
        return None
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', quote).strip().rstrip('.')
    text = re.sub(r'\s+(?:required|preferred)$', '', text, flags=re.I)
    found = _RELEVANT.fullmatch(text) or _HANDS_ON.fullmatch(text)
    if not found:
        return None
    scope = found['scope']
    # A shared minimum for an explicit OR list is supported. Slash, AND and
    # unqualified comma lists retain their complete unresolved relationship.
    ambiguous = '/' in scope or bool(re.search(r'\band\b', scope, re.I)) or (',' in scope and not re.search(r'\bor\b', scope, re.I))
    scopes = [scope] if ambiguous else re.split(r',\s*(?:or\s+)?|\s+or\s+', scope, flags=re.I)
    return dict(minimum=found['minimum'], unit='months' if found['unit'].lower().startswith('month') else 'years',
                scope=scope, scopes=[s.strip() for s in scopes],
                operator='unresolved' if ambiguous else 'any_of' if len(scopes) > 1 else 'single')


def _months(value, unit):
    # Decimal construction and ordering are exact; Decimal arithmetic is not.
    # Scale the finite decimal's integer coefficient, then construct the result
    # directly. No operation here consults ambient decimal precision/rounding.
    parts = Decimal(str(value)).as_tuple()
    coefficient = int(''.join(map(str, parts.digits))) * (12 if unit == 'years' else 1)
    return Decimal((parts.sign, tuple(map(int, str(coefficient))), parts.exponent))


def compare_duration(quote, profile):
    req = requirement(quote)
    result = dict(status='unresolved', requirement=req, bounds=[], scope_results=[],
                  basis='deterministic_confirmed_duration', message='Relevant professional duration is not established.')
    if req is None or req['operator'] == 'unresolved':
        return result
    bounds = []
    # Explicit aggregate fields are not single jobs, timelines or lower bounds.
    for i, item in enumerate(profile.get('experience', {}).get('years_by_domain', [])):
        if not isinstance(item, dict) or type(item.get('years')) not in (int, float):
            continue
        domain, years = item.get('domain'), item['years']
        if (not isinstance(domain, str) or not 0 <= years <= 80 or not math.isfinite(years)
                or isinstance(years, float) and round(years, 2) != years):
            continue
        facts = [confirmed_fact(profile, f'experience.years_by_domain[{i}].{key}', item[key]) for key in ('domain', 'years')]
        if all(facts):
            bounds.append(dict(scope=domain, amount=years, unit='years', bound='total', basis='confirmed_domain_aggregate', profile_facts=facts))
    # Only full, explicit bound assertions. A single employment interval,
    # partial history, aspirations, and unqualified prose are not totals.
    for key in ('hard_constraints', 'negative_constraints'):
        for i, value in enumerate(profile.get('constraints', {}).get(key, [])):
            if not isinstance(value, str) or len(value) > _MAX_BOUND_TEXT:
                continue
            match = _BOUND.fullmatch(value.strip())
            fact = confirmed_fact(profile, f'constraints.{key}[{i}]', value)
            if match and fact:
                bounds.append(dict(scope=match['scope'].rstrip('.'), amount=match['amount'],
                    unit='months' if match['unit'].lower().startswith('month') else 'years',
                    bound={'at least': 'lower', 'at most': 'upper'}.get(match['bound'].lower(), 'total'),
                    basis='explicit_bound_statement', profile_facts=[fact]))
    minimum = _months(req['minimum'], req['unit'])
    for scope in req['scopes']:
        found = [b for b in bounds if scope_key(b['scope']) == scope_key(scope)]
        # A bound assertion qualifies a numeric aggregate of the same amount.
        # Never turn "at least two" into an exhaustive two-year upper bound.
        qualified = [b for b in found if b['bound'] in ('lower', 'upper')]
        if qualified:
            found = [b for b in found if b['basis'] != 'confirmed_domain_aggregate' or not any(
                _months(b['amount'], b['unit']) == _months(q['amount'], q['unit']) for q in qualified)]
        result['bounds'].extend(found)
        lowers = [_months(b['amount'], b['unit']) for b in found if b['bound'] in ('total', 'lower')]
        uppers = [_months(b['amount'], b['unit']) for b in found if b['bound'] in ('total', 'upper')]
        lower, upper = max(lowers, default=None), min(uppers, default=None)
        status = 'unresolved'
        if lower is not None and upper is not None and lower > upper:
            reason = 'Conflicting confirmed duration bounds.'
        elif upper is not None and upper < minimum:
            status, reason = 'contradicted', 'Confirmed relevant total or upper bound is below the source minimum.'
        elif lower is not None and lower >= minimum:
            status, reason = 'supported', 'Confirmed relevant duration meets the source minimum; depth remains separate.'
        else:
            reason = 'No exhaustive relevant shortfall or sufficient lower bound is established.'
        result['scope_results'].append(dict(scope=scope, status=status, lower_months=str(lower) if lower is not None else None,
            upper_months=str(upper) if upper is not None else None, reason=reason))
    states = [s['status'] for s in result['scope_results']]
    if 'supported' in states:
        result.update(status='supported', message='Your confirmed relevant duration meets a source duration option. Responsibilities and depth still need confirmation.')
    elif states and all(s == 'contradicted' for s in states):
        result.update(status='contradicted', message='Your confirmed relevant duration is below the source minimum for every stated option.')
    return result
