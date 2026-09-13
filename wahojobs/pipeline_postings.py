"""Exact posting links for private workflow; no writes or fuzzy association.

Jobs' persisted (company_id, source_hash) key and row ID identify a posting.
The owner-bound item namespace records that identity without a schema change.
Titles, URLs, canonical membership and accepted captures are not workflow keys.
"""
import hashlib
import json
import re
from collections import Counter

PREFIX = 'pipeline::posting-v1::'
_ITEM = re.compile(r'pipeline::posting-v1::([1-9][0-9]*)::[0-9a-f]{64}\Z')


def load_posting(connection, job_id):
    if type(job_id) is not int or not 0 < job_id <= 9223372036854775807:
        return None
    row = connection.execute(
        'SELECT j.*, c.name AS source, c.slug AS source_slug FROM jobs j '
        'JOIN companies c ON c.id=j.company_id WHERE j.id=?', (job_id,)).fetchone()
    return dict(row) if row is not None else None


def item_id(owner_profile_id, posting):
    identity = [owner_profile_id, posting['id'], posting['company_id'], posting['source_hash']]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
    return f"{PREFIX}{posting['id']}::{digest}"


def resolve_record(connection, record):
    """Return an exact row or a bounded reason. Never repair stored history."""
    key = record['pipeline_item_id']
    if key.startswith(PREFIX):
        parsed = _ITEM.fullmatch(key)
        posting = load_posting(connection, int(parsed[1])) if parsed else None
        if posting is not None and item_id(record['profile_id'], posting) == key:
            return posting, 'linked'
        return None, 'unavailable'
    # Legacy provider identity is exact and case-sensitive. A nonempty ID must
    # resolve on its own; a conflicting/absent ID is never replaced with a URL guess.
    external = record.get('external_id') or ''
    url = record.get('url') or ''
    if not external and not url:
        return None, 'unresolved_legacy'
    rows = connection.execute(
        'SELECT DISTINCT j.id FROM jobs j JOIN companies c ON c.id=j.company_id '
        'LEFT JOIN job_source_contents sc ON sc.job_id=j.id '
        'WHERE (c.name=? OR c.slug=?) AND ' +
        ('j.external_id=?' if external else '(j.url=? OR sc.source_url=?)'),
        (record['source'], record['source'], external) if external else
        (record['source'], record['source'], url, url)).fetchall()
    if len(rows) != 1:
        return None, 'ambiguous_legacy' if rows else 'unresolved_legacy'
    return load_posting(connection, rows[0][0]), 'linked_legacy'


def link_records(connection, records):
    result = []
    for record in records:
        posting, state = resolve_record(connection, record)
        result.append(dict(record, _posting_link_state=state,
            _posting_job_id=posting['id'] if posting else None,
            _posting_canonical_id=posting['canonical_opportunity_id'] if posting else None))
    counts = Counter(r['_posting_job_id'] for r in result if r['_posting_job_id'] is not None)
    for record in result:
        if counts[record['_posting_job_id']] > 1:
            record['_posting_link_state'] = 'ambiguous_history'
    return result


def hidden_job_ids(records):
    # Ambiguous duplicate histories remain distinct. Any authoritative hidden
    # association suppresses this posting until that history is explicitly restored.
    return frozenset(r['_posting_job_id'] for r in records
                     if r.get('_posting_job_id') is not None and r['visibility'] == 'hidden')


def source_binding(connection, job_id):
    """Capture-sensitive action binding, intentionally distinct from item_id."""
    posting = load_posting(connection, job_id)
    if posting is None:
        return None
    source = connection.execute('SELECT * FROM job_source_contents WHERE job_id=?', (job_id,)).fetchone()
    acceptance = connection.execute('SELECT * FROM job_source_content_acceptances WHERE job_id=?', (job_id,)).fetchone()
    # Content/identity/lifecycle changes invalidate controls. New availability
    # observations are checked separately at POST using the current scoped source.
    payload = [posting, dict(source) if source else None, dict(acceptance) if acceptance else None]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def bind_match(connection, match):
    posting = load_posting(connection, match.get('job_id'))
    if posting is None:
        return dict(match)
    return dict(match, _workflow_source_binding=source_binding(connection, posting['id']),
                _workflow_posting_id=posting['id'])


def current_action_posting(connection, match, *, now):
    """Revalidate exact source availability without trusting run membership."""
    from wahojobs.authenticated_variant_details import load_scoped_snapshot
    from wahojobs.public_job_page import prepare_public_job, PUBLIC_JOB_STATE_LIVE
    posting = load_posting(connection, match.get('_workflow_posting_id'))
    if (posting is None or match.get('job_id') != posting['id']
            or match.get('_workflow_source_binding') != source_binding(connection, posting['id'])):
        return None
    canonical = posting['canonical_opportunity_id']
    if not canonical or match.get('canonical_opportunity_id') != canonical:
        return None
    snapshot = load_scoped_snapshot(connection, canonical, posting['id'], now=now)
    job = prepare_public_job(snapshot['detail_evidence'], selected_job_id=posting['id'], now=now)
    if job is None or job['public_state'] != PUBLIC_JOB_STATE_LIVE:
        return None
    return posting
