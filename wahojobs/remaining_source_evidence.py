"""Requalify three pinned beta-host capture journals without network access.

This is a one-time, exact-evidence bridge. It does not discover more sources,
refresh availability, or authorize publication on its own.
"""
from hashlib import sha256
import json
from pathlib import Path

from wahojobs import evidence_maintenance as maintenance
from wahojobs.crawler.staged_observation import Observation
from wahojobs.crawler.types import CompanyCrawlResult, ProviderOutcome
from wahojobs.crawler.providers.dataannotation import (
    CANONICAL_REDIRECTS, DOMAIN_PAGES, parse_domain_page,
)
from wahojobs.crawler.providers.dataforce import (
    QUALIFIED_DETAIL_PATHS, parse_jobs_page, qualify_detail_record,
    validate_inventory_page, validate_pagination,
)

CAPTURE_ROOT = Path('/var/lib/wahojobs-beta/remaining-source-coverage-v1/task-ledger')
PINNED = (
    ('20260924T135201809767Z-dataannotation-validation', 'dataannotation',
     '045578a22c4187bc093fcc34065afe5157f58ddd',
     '3768480846180f6175d3a5b014601dadf74da563588d12208c3bd632b30d9e60',
     'a7c3788e034be30425b0232a6cd6f766cef5c14eb8a94529bdf177e9af3e1e99', 13),
    ('20260924T135224168385Z-dataforce-validation', 'dataforce',
     '045578a22c4187bc093fcc34065afe5157f58ddd',
     'ccff84160528b1e726170915822f83ea156307d49b59bcba0258fb6ff553594d',
     'b9233229c9c33701408553f705bab071db23e027f6e112089dbaee6890ffe821', 53),
    ('20260924T140304135807Z-dataannotation-validation', 'dataannotation',
     '7a87a6ea591fd126c62ed189b4ff8bddb6a9173a',
     '4f600a2c317224ac9463d3d72067cd5a64a2fe8e7999f7a26d886a4508b7f4fe',
     'fb7cb340879b7faa9710eb03e58bae2e74e8cdccfa631decfddde08f3db3a31a', 16),
)


def _responses(run, report):
    journal = run / 'journal' / report['plan_id']
    found = []
    for event in report['events']:
        row = event['data']
        if event['event'] != 'source_transport' or row.get('event') != 'response':
            continue
        raw = row.get('raw_response')
        if not raw:
            continue
        name = raw['file']
        if '/' in name or '\\' in name or not name.endswith('.raw'):
            raise ValueError('capture_raw_path_invalid')
        body = (journal / name).read_bytes()
        if len(body) != raw['bytes'] or sha256(body).hexdigest() != raw['sha256']:
            raise ValueError('capture_raw_hash_mismatch')
        found.append((row, body.decode('utf-8', errors='replace')))
    return found


def _dataannotation_result(rows, expected_domains):
    by_path = {value: key.lstrip('/') for key, value in CANONICAL_REDIRECTS.items()}
    domains = {page.slug: page for page in DOMAIN_PAGES}
    for domain in expected_domains:
        start = 'https://www.dataannotation.tech/' + domain
        redirects = [row for row, _ in rows if row['url'] == start and row['status'] == 301]
        if len(redirects) != 1 or redirects[0]['response_headers'].get('Location') != CANONICAL_REDIRECTS['/' + domain]:
            raise ValueError('dataannotation_redirect_lineage_missing')
    jobs = []
    seen = set()
    for row, body in rows:
        if row['status'] != 200 or row['url'] != row['final_url']:
            continue
        prefix = 'https://www.dataannotation.tech'
        if not row['url'].startswith(prefix):
            continue
        domain = by_path.get(row['url'][len(prefix):])
        if domain is None or domain not in expected_domains or domain in seen:
            raise ValueError('dataannotation_unexpected_canonical_response')
        seen.add(domain)
        jobs.append(parse_domain_page(domains[domain], row['url'], body))
    if seen != expected_domains or len(jobs) != len(expected_domains):
        raise ValueError('dataannotation_canonical_coverage_missing')
    return CompanyCrawlResult(jobs=jobs, used_sample_data=False,
        source_message='Pinned September 24 beta-host canonical role evidence',
        source_type='evergreen-application-pages', outcome=ProviderOutcome.PARTIAL,
        raw_record_count=len(jobs), normalized_record_count=len(jobs),
        payload_shape='dataannotation_evergreen_roles_v2',
        schema_fingerprint='dataannotation_evergreen_roles_v2')


def _dataforce_result(rows):
    pages = [(row, body) for row, body in rows if row['status'] == 200
             and row['url'].split('?', 1)[0] == 'https://dataforcecommunity.transperfect.com/projects']
    if len(pages) != 3:
        raise ValueError('dataforce_index_coverage_changed')
    jobs = {}
    for page, (row, body) in enumerate(pages):
        expected = ('https://dataforcecommunity.transperfect.com/projects' if page == 0
                    else 'https://dataforcecommunity.transperfect.com/projects?project_type=All&page=' + str(page))
        if row['url'] != expected or row['final_url'] != expected:
            raise ValueError('dataforce_index_sequence_changed')
        validate_inventory_page(body)
        if validate_pagination(body, page) != (page < 2):
            raise ValueError('dataforce_terminal_pager_changed')
        for job in parse_jobs_page(body, expected):
            if job.url in jobs:
                raise ValueError('dataforce_duplicate_index_identity')
            jobs[job.url] = job
    if len(jobs) != 54:
        raise ValueError('dataforce_index_count_changed')
    details = {row['url']: body for row, body in rows if row['status'] == 200
               and row['url'] in jobs and row['final_url'] == row['url']}
    if len(details) != 50:
        raise ValueError('dataforce_detail_coverage_changed')
    qualified = []
    for url, job in jobs.items():
        if url.split('dataforcecommunity.transperfect.com', 1)[-1] in QUALIFIED_DETAIL_PATHS:
            if url not in details:
                raise ValueError('dataforce_qualified_detail_missing')
            qualified.append(qualify_detail_record(job, details[url]))
    if len(qualified) != 8:
        raise ValueError('dataforce_qualified_scope_changed')
    return CompanyCrawlResult(jobs=qualified, used_sample_data=False,
        source_message='Pinned September 24 beta-host index and role detail evidence',
        source_type='dataforce-community-html', outcome=ProviderOutcome.PARTIAL,
        raw_record_count=54, normalized_record_count=8, filtered_record_count=46,
        payload_shape='dataforce_index_detail_record_v1',
        schema_fingerprint='dataforce_index_detail_record_v1')


def pinned_observations(root=CAPTURE_ROOT):
    """Return the three original dated observations and verified journal reports."""
    root = Path(root)
    out = []
    for name, source, commit, plan_id, terminal_hash, attempts in PINNED:
        run = root / 'runs' / name
        receipt = json.loads((run / 'receipt.json').read_text(encoding='utf-8'))
        capture_plan = json.loads((run / 'plan.json').read_text(encoding='utf-8'))
        report = maintenance.report(run / 'journal', plan_id)
        terminal = report['events'][-1]
        if (receipt.get('status') != 'collected_unpublished' or receipt.get('no_publication') is not True
                or receipt.get('version') != 'remaining_source_delivery_ledger_v1'
                or receipt.get('source') != source or receipt.get('plan_id') != plan_id
                or receipt.get('journal_hash') != terminal_hash or receipt.get('http_attempts') != attempts
                or capture_plan.get('execution_host') != 'wahojobs-private-beta-rehearsal-20260917'
                or capture_plan.get('code_commit') != commit or capture_plan.get('source') != source
                or terminal['hash'] != terminal_hash or terminal['event'] != 'finished'
                or terminal['data'].get('status') != 'collected_unpublished'
                or terminal['data']['request_usage']['http_transactions'] != attempts):
            raise ValueError('pinned_capture_lineage_mismatch')
        rows = _responses(run, report)
        if source == 'dataforce':
            result = _dataforce_result(rows)
        else:
            domains = ({'coding', 'generalist'} if attempts == 13 else
                       {'law', 'math', 'medicine', 'physics', 'finance', 'accounting', 'chemistry', 'biology'})
            result = _dataannotation_result(rows, domains)
        completed = [e['data']['completed_at'] for e in report['events'] if e['event'] == 'collected_result']
        if len(completed) != 1:
            raise ValueError('capture_completion_missing')
        observation = Observation(source=source, careers_url=(
            'https://www.dataannotation.tech' if source == 'dataannotation' else
            'https://dataforcecommunity.transperfect.com/projects'),
            started_at=report['plan']['started_at'], completed_at=completed[0],
            result=result, collection_plan_id=plan_id, journal_hash=terminal_hash,
            request_usage=terminal['data']['request_usage'], controlled_validation=True)
        out.append((observation, report))
    return out
