"""Bounded recommendation materiality, separate from qualification satisfaction.

Only complete, source-bound ordinary behavioral clauses are recognized here.
Unrecognized/mixed wording remains material or unresolved. This never supplies
candidate evidence, changes a comparison result, or waives a modeled condition.
"""
from copy import deepcopy
import re


VERSION = 1
# These complete forms describe ordinary work habits, not credentials, language
# proficiency, professional history, tools, availability or task specialization.
# Full matching is intentional: a credential or restrictive suffix cannot be
# discarded merely because a sentence starts with an ordinary quality.
_QUALITY = (
    r'(?:(?:strong|good|excellent) )?attention to detail'
    r'(?: with a (?:systematic|thorough|careful|methodical)'
    r'(?:(?:,| and) (?:systematic|thorough|careful|methodical))* approach to (?:tasks|work))?'
    r'|(?:self[- ]motivated|reliable|motivated|patient|curious|independent)'
    r'|(?:clear|good|strong|excellent) (?:written )?communication(?: skills)?'
    r'|(?:a )?(?:willingness|motivation|commitment) to learn'
)
_QUALITY_LIST = rf'(?:{_QUALITY})(?:(?:,\s*(?:and )?| and )(?:{_QUALITY}))*'
_BEHAVIOR = re.compile(
    rf'(?:{_QUALITY_LIST})(?: when working independently)?'
    r'|(?:able to |ability to )?follow (?:written |structured |detailed )?'
    r'(?:project )?(?:guidelines|instructions)'
    r'(?: and apply them consistently| consistently)?'
    r'|comfortable evaluating (?:a broad (?:variety|range) of|varied|different) '
    r'topics and content formats', re.I)


def _bound_clause(row, source):
    """Require this exact accepted clause, not a matching text fragment."""
    ref = row.get('source') or {}
    url = source.get('url', source.get('source_url'))
    material = source.get('material_content_sha256', source.get('source_hash'))
    if (type(ref.get('job_id')) is not int or ref['job_id'] <= 0
            or ref.get('job_id') != source.get('job_id')
            or ref.get('external_id') != source.get('external_id')
            or not isinstance(url, str) or not url or ref.get('url') != url
            or not isinstance(material, str) or not material or ref.get('source_hash') != material
            or type(ref.get('line')) is not int or ref['line'] < 1
            or not all(isinstance(ref.get(k), str) and ref[k]
                       for k in ('quote', 'heading', 'block_reference'))):
        return False
    # A loaded raw source additionally carries independent content/row binding.
    if ('body' in source and (source.get('source_url') != url
            or source.get('content_external_id') != source.get('external_id'))):
        return False
    from wahojobs.authenticated_card_evidence import _blocks, _source_text
    from wahojobs.candidate_condition_comparisons import _condition_lines
    try:
        blocks = _blocks(_source_text(source)) if 'body' in source else source.get('blocks', [])
        return any(block.get('reference') == ref['block_reference']
                   and block.get('heading') == ref['heading']
                   and any(line == ref['line'] and quote == ref['quote']
                           for line, quote in _condition_lines(block))
                   for block in blocks)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def condition_materiality(row, source):
    """Classify a current comparison without changing status or authority.

``source`` is a loaded accepted source or its exact presentation packet with
full blocks. Existing current prepared annotations retain their authority;
the deterministic fallback records its own distinct, versioned basis.
"""
    result = dict(classification='material_or_unresolved',
                  basis='conservative_unresolved', admission_decisive=True,
                  source=deepcopy(row.get('source') or {}))
    if (row.get('kind') != 'unassessed' or row.get('status') != 'unresolved'
            or row.get('modality') in ('conflicting', 'not_required')
            or row.get('message') or not _bound_clause(row, source)):
        return result
    from wahojobs.source_clause_materiality import generic_annotation
    annotation = generic_annotation(row, source)
    if annotation is not None:
        return dict(annotation, admission_decisive=False,
                    source=deepcopy(row['source']), prepared_source=annotation['source'])
    quote = re.sub(r'\*\*([^*]+)\*\*', r'\1', row['source']['quote']).strip().rstrip('.')
    # Explicit behavioral requirements still describe employer-assessed habits;
    # this changes recommendation materiality, never their required modality.
    quote = re.sub(r'^(?:(?:you|candidates|applicants) must (?:be |have )?|must (?:be |have )?)', '', quote, flags=re.I)
    quote = re.sub(r' (?:is |are )?required$', '', quote, flags=re.I)
    if not _BEHAVIOR.fullmatch(quote):
        return result
    return dict(result, classification='generic_behavior_only',
                basis='bounded_behavior_clause', version=VERSION,
                admission_decisive=False)
