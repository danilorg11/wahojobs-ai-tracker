"""Discovery visibility is a projection of current, owner-authorized workflow.

Never infer identity from titles or employers. Exact linked postings and their
current accepted canonical membership are the only cross-variant association.
"""


def is_tracked(record):
    return (record.get('workflow_status') not in {None, 'recommended'}
            or bool(record.get('reminder_at')))


def suppresses_discovery(record):
    return record.get('visibility') == 'hidden' or is_tracked(record)


def excluded_identities(records):
    jobs, canonicals = set(), set()
    for record in records:
        if not suppresses_discovery(record):
            continue
        if record.get('_posting_job_id') is not None:
            jobs.add(record['_posting_job_id'])
        if record.get('_posting_canonical_id') is not None:
            canonicals.add(record['_posting_canonical_id'])
    return jobs, canonicals


def visible(match, records):
    jobs, canonicals = excluded_identities(records)
    return (match.get('job_id') not in jobs
            and match.get('canonical_opportunity_id') not in canonicals)
