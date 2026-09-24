"""Retain one bounded public observation before taking beta offline to publish.

This is a strict codec for existing adapter results, not HTTP replay or a second
crawler. Publication uses the original collection dates and current authority.
"""
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
import json
import os

from wahojobs.crawler.types import CompanyCrawlResult, JobCandidate, RecordPromotionAttestation
from wahojobs.crawler.local_inventory import refresh_request_budget
from wahojobs.daily_source_policy import POLICY, daily_source, controlled_validation_source

VERSION = 'daily_collected_observation_v1'


@dataclass(frozen=True)
class Observation:
    source: str
    careers_url: str
    started_at: str
    completed_at: str
    result: CompanyCrawlResult
    collection_plan_id: str
    journal_hash: str
    request_usage: dict
    controlled_validation: bool = False


def timestamp():
    from wahojobs.crawler.pipeline import utc_now
    return utc_now()


def _aware(value):
    at = datetime.fromisoformat(value)
    if at.tzinfo is None: raise ValueError('aware_observation_time_required')
    return at


def validate_observation(value, source):
    if type(value) is not Observation or value.source != source or source not in POLICY:
        raise ValueError('bound_source_observation_required')
    start, end, at = _aware(value.started_at), _aware(value.completed_at), _aware(timestamp())
    if type(value.controlled_validation) is not bool:
        raise ValueError('invalid_observation_scope')
    controlled = value.controlled_validation and source in ('dataannotation', 'dataforce')
    http_max = (32 if source == 'dataannotation' else 100) if controlled else POLICY[source]['http_max']
    seconds_max = 900 if controlled else POLICY[source]['seconds_max']
    if (value.controlled_validation and not controlled
            or not 0 <= (end-start).total_seconds() <= seconds_max
            or not 0 <= (at-end).total_seconds() <= 3600
            or value.result.used_sample_data is not False
            or not 0 < value.request_usage['http_transactions'] <= http_max
            or value.request_usage['detail_requests'] != 0):
        raise ValueError('staged_observation_not_admissible')
    return value


def _construct(cls, document):
    if type(document) is not dict or set(document) != {f.name for f in fields(cls)}:
        raise ValueError('collected_result_schema_changed')
    return cls(**document)


def decode_result(document):
    document = dict(document)
    if type(document.get('jobs')) is not list or len(document['jobs']) > 20_000:
        raise ValueError('invalid_collected_jobs')
    jobs = []
    for item in document['jobs']:
        item = dict(item)
        if item.get('record_promotion_attestation') is not None:
            item['record_promotion_attestation'] = _construct(RecordPromotionAttestation, item['record_promotion_attestation'])
        jobs.append(_construct(JobCandidate, item))
    document['jobs'] = jobs
    from wahojobs.crawler.providers.greenhouse import GreenhouseSourceRecord, GreenhouseDepartmentMetadata, GreenhouseOfficeMetadata
    records = []
    for item in document.get('source_records', []):
        item = dict(item)
        item['departments'] = tuple(_construct(GreenhouseDepartmentMetadata, dict(d, child_ids=tuple(d['child_ids']))) for d in item['departments'])
        item['offices'] = tuple(_construct(GreenhouseOfficeMetadata, dict(d, child_ids=tuple(d['child_ids']))) for d in item['offices'])
        item['additional_locations'] = tuple(item['additional_locations'])
        records.append(_construct(GreenhouseSourceRecord, item))
    document['source_records'] = tuple(records)
    result = _construct(CompanyCrawlResult, document)
    if type(result.used_sample_data) is not bool: raise ValueError('invalid_synthetic_flag')
    return result


def collect(source, careers_url, directory, *, run_id, code_commit, http_max, journal_root, controlled_validation=False):
    from wahojobs import evidence_maintenance as maintenance
    from wahojobs.crawler.pipeline import CRAWLERS
    from wahojobs.crawler.source_registry import assert_production_dispatch_allowed
    assert_production_dispatch_allowed(source)
    permitted = (32 if source == 'dataannotation' else 100) if controlled_validation else POLICY[source]['http_max']
    if not 0 < http_max <= permitted: raise ValueError('invalid_collection_budget')
    if controlled_validation and (source, careers_url) not in {
        ('dataannotation', 'https://www.dataannotation.tech'),
        ('dataforce', 'https://dataforcecommunity.transperfect.com/projects'),
    }:
        raise ValueError('controlled_validation_endpoint_out_of_scope')
    plan = dict(version=maintenance.VERSION, kind=VERSION, run_id=run_id, source=source,
        careers_url=careers_url, code_commit=code_commit, contract_fingerprint=maintenance.contract_fingerprint(),
        started_at=timestamp(), http_max=http_max,
        controlled_validation=controlled_validation)
    plan['plan_id'] = maintenance.digest(plan)
    root = Path(directory); root.mkdir(parents=True, exist_ok=True)
    maintenance.save_json(root/(source+'-collection.json'), dict(plan_id=plan['plan_id']))
    # The established snapshot procedure already preserves this pinned root.
    # Collection writes evidence only; the product database remains online.
    journal = maintenance.Journal(journal_root, plan)
    source_context = controlled_validation_source(source) if controlled_validation else daily_source(source)
    with source_context, refresh_request_budget(http_limit=http_max, detail_limit=0,
            audit_sink=lambda event: journal.append('source_transport', event)) as budget:
        journal.append('started', dict(operation='collect:'+source))
        try:
            result = CRAWLERS[source](careers_url)
            if result.used_sample_data is not False: raise ValueError('daily_synthetic_evidence_forbidden')
            document = asdict(result)
            # Round-trip the exact closed result contract before sealing it.
            if asdict(decode_result(json.loads(maintenance.encoded(document)))) != document:
                raise ValueError('collected_result_round_trip_failed')
            journal.append('collected_result', dict(result=document, completed_at=timestamp()))
            journal.append('finished', dict(status='collected_unpublished', request_usage=budget.summary()))
        except Exception as error:
            journal.append('finished', dict(status='collection_failed', error_type=type(error).__name__, request_usage=budget.summary()))
            raise
    return plan['plan_id']


def load(directory, source, *, run_id, code_commit, journal_root, consume=False):
    from wahojobs import evidence_maintenance as maintenance
    root = Path(directory)
    plan_id = json.loads((root/(source+'-collection.json')).read_text())['plan_id']
    report = maintenance.report(journal_root, plan_id)
    plan = report['plan']
    if (plan.get('kind') != VERSION or plan.get('run_id') != run_id or plan.get('source') != source
            or plan.get('code_commit') != code_commit or plan.get('contract_fingerprint') != maintenance.contract_fingerprint()
            or report['status'] != 'collected_unpublished'):
        raise ValueError('completed_bound_collection_required')
    collected = [e['data'] for e in report['events'] if e['event'] == 'collected_result']
    finished = report['events'][-1]
    if len(collected) != 1 or finished['event'] != 'finished': raise ValueError('single_collection_result_required')
    value = Observation(source, plan['careers_url'], plan['started_at'], collected[0]['completed_at'],
        decode_result(collected[0]['result']), plan_id, finished['hash'], finished['data']['request_usage'],
        bool(plan.get('controlled_validation', False)))
    validate_observation(value, source)
    if consume:
        with (Path(journal_root)/plan_id/'publication.claim').open('x') as stream:
            stream.write(run_id); stream.flush(); os.fsync(stream.fileno())
    return value, report


def publication_report(collection, publication):
    """Join verified lineage for reporting only, never replay or mutate evidence."""
    if not publication.get('events') or publication['events'][-1]['event'] != 'finished':
        raise ValueError('finished_publication_required')
    terminal = publication['events'][-1]
    links = [e['data'] for e in publication['events'] if e['event'] == 'staged_observation']
    if (collection['status'] != 'collected_unpublished' or len(links) != 1
            or links[0]['collection_plan_id'] != collection['plan_id']
            or links[0]['collection_journal_hash'] != collection['events'][-1]['hash']
            or links[0]['source'] != collection['plan']['source']
            or terminal['data']['request_usage']['http_transactions'] != 0):
        raise ValueError('publication_collection_lineage_invalid')
    terminal = dict(terminal, data=dict(terminal['data'], request_usage=collection['events'][-1]['data']['request_usage']))
    return dict(publication, events=[*collection['events'][:-1], *publication['events'][:-1], terminal])
