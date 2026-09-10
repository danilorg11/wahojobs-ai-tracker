"""Source-only semantic annotations for one narrow admission exception.

No behavioral vocabulary or candidate classification lives here. The existing
enricher classifies complete source clauses; this boundary authenticates their
identity and preserves unknown/mixed output without granting an exemption.
"""
from copy import deepcopy
import hashlib
import json
import sqlite3

VERSION = 'source_clause_materiality_v1'
FIELD = 'clause_materiality'
KINDS = ('generic_behavior_only', 'specific_or_mixed', 'ambiguous')
MAX_CLAUSES = 64


def clause_catalog(semantic_input):
    from wahojobs.authenticated_card_evidence import _source_text, _blocks, _QUALIFICATION_HEADINGS
    from wahojobs.candidate_condition_comparisons import _lines

    catalog = {}
    remaining = 8_000
    for item in semantic_input.get('rich_content') or []:
        authority = item.get('authority') or {}
        capture = authority.get('accepted_capture_ref')
        material = item.get('material_content_sha256')
        if not capture or not material:
            continue  # legacy/unaccepted content cannot authorize the exception
        source = dict(item, metadata_json=json.dumps(item.get('metadata') or {}),
                      source_slug=item.get('provider'), url=item.get('source_url'))
        try:
            blocks = _blocks(_source_text(source))
        except (TypeError, ValueError, KeyError):
            continue
        for block in blocks:
            if block['heading'].casefold().rstrip(':') not in _QUALIFICATION_HEADINGS:
                continue
            for line, quote in _lines(block):
                if len(quote) > remaining:
                    continue  # never classify a truncated clause as exclusively generic
                clause = dict(variant_ref=item['variant_ref'], source_url=item['source_url'],
                              external_id=item['external_id'], provider=item['provider'],
                              material_content_sha256=material, accepted_capture_ref=capture,
                              block_reference=block['reference'], heading=block['heading'],
                              line=line, quote=quote)
                alias = 'clause:' + hashlib.sha256(json.dumps(
                    clause, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                catalog[alias] = clause
                remaining -= len(quote)
                if len(catalog) >= MAX_CLAUSES:
                    return catalog  # incomplete coverage grants no default classification
    return catalog


def output_schema(clause_ids):
    # An empty source catalog has no permissible nonempty model response.
    return dict(type='array', maxItems=MAX_CLAUSES if clause_ids else 0, items=dict(
        type='object', additionalProperties=False,
        required=['clause_id', 'classification'], properties={
            'clause_id': {'type': 'string', 'enum': sorted(clause_ids) or ['no_clause_available']},
            'classification': {'type': 'string', 'enum': list(KINDS)}}))


def accept_annotations(items, catalog):
    from wahojobs.opportunity_enrichment import EnrichmentValidationError
    if type(items) is not list or len(items) > MAX_CLAUSES:
        raise EnrichmentValidationError('Invalid clause materiality list')
    seen, accepted = set(), []
    for item in items:
        if (type(item) is not dict or set(item) != {'clause_id', 'classification'}
                or type(item['clause_id']) is not str or item['clause_id'] not in catalog
                or item['clause_id'] in seen or item['classification'] not in KINDS):
            raise EnrichmentValidationError('Invalid or ambiguous clause materiality evidence')
        seen.add(item['clause_id'])
        accepted.append(dict(item, version=VERSION, basis='llm_source_evidence',
                             source=deepcopy(catalog[item['clause_id']])))
    return accepted


def validate_stored(items):
    from wahojobs.opportunity_enrichment import EnrichmentValidationError
    if type(items) is not list or len(items) > MAX_CLAUSES:
        raise EnrichmentValidationError('Invalid stored clause materiality')
    source_keys = {'variant_ref', 'source_url', 'external_id', 'provider',
                   'material_content_sha256', 'accepted_capture_ref',
                   'block_reference', 'heading', 'line', 'quote'}
    seen = set()
    for item in items:
        if (type(item) is not dict or set(item) != {'clause_id', 'classification', 'version', 'basis', 'source'}
                or item['version'] != VERSION or item['basis'] != 'llm_source_evidence'
                or item['classification'] not in KINDS or type(item['source']) is not dict
                or set(item['source']) != source_keys):
            raise EnrichmentValidationError('Invalid stored clause materiality')
        source = item['source']
        if (type(source['line']) is not int or source['line'] < 1
                or any(type(source[k]) is not str or not source[k] for k in source_keys - {'line', 'external_id'})
                or source['external_id'] is not None and type(source['external_id']) is not str):
            raise EnrichmentValidationError('Invalid stored clause source')
        alias = 'clause:' + hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if item['clause_id'] != alias or alias in seen:
            raise EnrichmentValidationError('Mismatched stored clause source')
        seen.add(alias)


def annotation_freshness(semantic_input, document):
    """Evidence binding is separate from semantic wording and recipe identity."""
    if type(document) is not dict:
        return 'invalid'
    items = document.get(FIELD)
    if items is None or items == []:
        return 'absent'  # no implicit backfill of legacy/unannotated documents
    try:
        validate_stored(items)
        catalog = clause_catalog(semantic_input)
        return ('current' if all(catalog.get(a['clause_id']) == a['source'] for a in items)
                else 'stale')
    except (ValueError, TypeError, KeyError):
        return 'invalid'


def binding_fingerprint(semantic_input):
    """Scope a repair attempt to accepted clauses, not observation timestamps."""
    return hashlib.sha256(json.dumps(clause_catalog(semantic_input),
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def attach_current_annotations(connection, sources):
    """Read existing enrichment only, on the already bounded source pool.

    Old documents cost no additional semantic preparation. Nonempty annotations
    require the existing source-integrity and recipe checks before consumption.
    No model, persistence, or enrichment refresh is reachable from this path.
    """
    from wahojobs import opportunity_enrichment as oe
    ids = sorted({s['canonical_opportunity_id'] for s in sources.values()
                  if s['canonical_opportunity_id'] is not None})
    if not ids:
        return sources
    try:
        rows = connection.execute('SELECT * FROM opportunity_enrichments WHERE canonical_opportunity_id IN ('
                                  + ','.join('?' for _ in ids) + ')', ids)
        names = [c[0] for c in rows.description]
        stored = [dict(zip(names, row)) for row in rows]
    except sqlite3.OperationalError:
        return sources  # optional enrichment storage absent
    for row in stored:
        try:
            document = json.loads(row['automatic_document_json'])
            if type(document) is not dict:
                continue
            items = document.get(FIELD)
            if not items:
                continue
            oe.validate_enrichment_document(document)
            semantic_input = oe.load_semantic_input(connection, row['canonical_opportunity_id'])
            freshness = oe.classify_enrichment_freshness(semantic_input, row)
            if (freshness['source_input_status'] != 'current' or freshness['derivation_status'] != 'current'
                    or freshness['clause_binding_status'] != 'current'
                    or not all(row.get(k) for k in ('model_provider', 'model_name', 'prompt_version'))):
                continue
            provenance = {k: row[k] for k in ('input_sha256', 'derivation_fingerprint',
                          'model_provider', 'model_name', 'prompt_version', 'generated_at')}
            for source in sources.values():
                if source['canonical_opportunity_id'] != row['canonical_opportunity_id']:
                    continue
                bound = [dict(deepcopy(a), provenance=provenance) for a in items
                         if (a['source']['variant_ref'] == 'source_hash:' + source['source_hash']
                             and a['source']['source_url'] == source['url']
                             and a['source']['external_id'] == source['external_id']
                             and a['source']['provider'] == source['source_slug']
                             and a['source']['material_content_sha256'] == source['material_content_sha256'])]
                if bound:
                    source[FIELD] = bound
        except (ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
            continue  # untrusted/stale enrichment retains conservative admission
    return sources


def generic_annotation(row, source):
    """Only a wholly unassessed row may be non-decisive; never a contradiction."""
    if (row['kind'] != 'unassessed' or row['status'] != 'unresolved'
            or row['modality'] == 'conflicting' or row['message']):
        return None
    try:
        annotations = source.get(FIELD, [])
        validate_stored([{k: v for k, v in a.items() if k != 'provenance'} for a in annotations])
        for annotation in annotations:
            ref = annotation['source']
            provenance = annotation.get('provenance') or {}
            if (annotation['classification'] == 'generic_behavior_only'
                    and all(type(provenance.get(k)) is str and provenance[k] for k in
                            ('input_sha256', 'derivation_fingerprint', 'model_provider',
                             'model_name', 'prompt_version', 'generated_at'))
                    and ref['variant_ref'] == 'source_hash:' + source['source_hash']
                    and ref['provider'] == source['source_slug']
                    and ref['external_id'] == source['external_id'] == row['source']['external_id']
                    and ref['source_url'] == source['url'] == row['source']['url']
                    and ref['material_content_sha256'] == source['material_content_sha256'] == row['source']['source_hash']
                    and all(ref[k] == row['source'][k]
                            for k in ('quote', 'heading', 'line', 'block_reference'))):
                return deepcopy(annotation)
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    return None
