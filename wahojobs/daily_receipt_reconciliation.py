"""Explicit read-only evidence reconstruction and append-only reporting correction.

Never executes a provider, replays a lifecycle, sends mail, or changes inventory.
Legacy recovery requires the exact original capture, publication linkage and
terminal database transaction; a merely recent or successful source is not enough.
"""
from dataclasses import asdict
from pathlib import Path
from datetime import timedelta

from wahojobs import daily_inventory as d, evidence_maintenance as m
from wahojobs.crawler import staged_observation as staged
from wahojobs.crawler.types import TrackingSummary,evaluate_removal_authorization

VERSION='daily_receipt_reconciliation_v1'


def read_current(path):
    receipt=d.read_json(path)
    if receipt is None:return None
    pointer=d.read_json(Path(path).parent/'current-reconciliation.json')
    if pointer is None:return receipt
    if (type(pointer) is not dict or set(pointer)!={'report_id'} or type(pointer['report_id']) is not str
            or len(pointer['report_id'])!=64 or any(c not in '0123456789abcdef' for c in pointer['report_id'])):
        raise ValueError('invalid_receipt_reconciliation_reference')
    correction=d.read_json(Path(path).parent/'reconciliations'/(pointer['report_id']+'.json'))
    if m.digest(correction)!=pointer['report_id']:raise ValueError('invalid_receipt_reconciliation_binding')
    _validate_correction(correction,receipt)
    return dict(receipt,sources=correction['sources'],reporting_reconciliation=pointer['report_id'])


def _validate_correction(correction,receipt):
    if (type(correction) is not dict or correction.get('version')!=VERSION
            or correction.get('original_receipt_sha256')!=m.digest(receipt)
            or correction.get('run_id')!=receipt.get('run_id')
            or type(correction.get('sources')) is not dict or set(correction['sources'])!=set(d.SOURCES)
            or any(type(row) is not dict for row in correction['sources'].values())):
        raise ValueError('invalid_receipt_reconciliation_binding')


def source_state(config,provider):
    """Resolve one committed overlay; newer daily state always takes precedence."""
    root=Path(config['state_directory']);row=d.read_json(root/(provider+'-state.json'))
    run_id=(row or {}).get('run_id')
    if run_id and Path(run_id).name==run_id and run_id not in ('.','..'):
        target=root/'runs'/run_id
        if (target/'current-reconciliation.json').exists():
            return read_current(target/'run.json')['sources'][provider]
    return row


def legacy_transaction(config,receipt,provider,collection,publication):
    """Resolve pre-v1 post-commit reporting failure using exact retained evidence."""
    plan=publication['plan'];links=[e['data'] for e in publication['events'] if e['event']=='staged_observation']
    collected=[e['data'] for e in collection['events'] if e['event']=='collected_result']
    if (plan['config']['providers']!=[provider] or plan['database']!=m.database_identity(Path(config['database']))
            or len(collected)!=1 or len(links)!=1
            or links[0]['collection_plan_id']!=collection['plan_id']
            or links[0]['collection_journal_hash']!=collection['events'][-1]['hash']):
        raise ValueError('legacy_transaction_binding_invalid')
    capture=staged.decode_result(collected[0]['result']);finished=collected[0]['completed_at']
    with m.read_connection(config['database']) as db:
        db.execute('BEGIN')
        candidates=db.execute('''SELECT r.* FROM crawl_runs r JOIN companies c ON c.id=r.company_id
            WHERE c.slug=? AND r.started_at=? ORDER BY r.id''',(provider,collection['plan']['started_at'])).fetchall()
        if len(candidates)!=1:return None
        terminal=dict(candidates[0])
        if terminal['status'] not in ('success','partial') or terminal['used_sample_data'] or terminal['finished_at']!=finished:return None
        if terminal['jobs_found_count']!=capture.normalized_record_count:return None
        captured=db.execute('SELECT count(*) FROM job_source_content_captures WHERE crawl_run_id=?',(terminal['id'],)).fetchone()[0]
        if captured!=terminal['jobs_found_count']:raise ValueError('legacy_transaction_capture_count_mismatch')
        events={kind:db.execute('SELECT count(*) FROM job_events WHERE crawl_run_id=? AND event_type=?',(terminal['id'],kind)).fetchone()[0]
                for kind in ('removed','discovered','reactivated')}
        if events!=dict(removed=terminal['jobs_removed_count'],discovered=terminal['jobs_new_count'],reactivated=terminal['jobs_reactivated_count']):
            raise ValueError('legacy_transaction_event_count_mismatch')
        authority=evaluate_removal_authorization(capture)
        if terminal['jobs_removed_count'] and not authority.authorized:raise ValueError('legacy_closure_authority_missing')
        after=m.inspect_source(db,provider,d.parse(receipt['ended_at']),catalog_only=True)
        if after['latest_run']!=terminal:return None # newer source state must not be backdated
    summary=TrackingSummary(source_type=capture.source_type,jobs_found=terminal['jobs_found_count'],
        jobs_new=terminal['jobs_new_count'],jobs_reactivated=terminal['jobs_reactivated_count'],
        jobs_updated=terminal['jobs_updated_count'],jobs_removed=terminal['jobs_removed_count'],
        active_jobs_total=sum(j['verification']['status']!='inactive' for j in after['jobs']),
        used_sample_data=False,source_message=capture.source_message,provider_outcome=capture.outcome,
        snapshot_complete=capture.snapshot_complete,pagination_complete=capture.pagination_complete,
        removals_authorized=authority.authorized,removal_skip_reasons=authority.skip_reasons,
        raw_record_count=capture.raw_record_count,normalized_record_count=capture.normalized_record_count,
        rejected_record_count=capture.rejected_record_count,filtered_record_count=capture.filtered_record_count)
    events=[e for e in publication['events'] if e['event'] not in ('operation_result','finished')]
    events.extend([dict(event='operation_result',data=dict(operation='catalog:'+provider,status='completed',
        result=dict(summary=asdict(summary),after=after))),publication['events'][-1]])
    joined=staged.publication_report(collection,dict(publication,events=events))
    row=d.summarize_source(plan,joined,d.parse(collection['plan']['started_at']),d.parse(receipt['ended_at']))
    if not row['qualifying_observation']:return None
    row.update(publication_outcome='committed',accounting_status='reconciled_exact_transaction',
        reconciliation_proof=dict(legacy_transaction=True,crawl_run_id=terminal['id'],
            terminal_crawl_row_sha256=m.digest(terminal),collection_journal_hash=collection['events'][-1]['hash'],
            publication_journal_hash=publication['events'][-1]['hash'],source_fingerprint=after['fingerprint']))
    return row


def build(config,run_id):
    target=Path(config['state_directory'])/'runs'/run_id;receipt=d.read_json(target/'run.json')
    if not receipt or not receipt.get('ended_at') or receipt.get('normal_service_resumed') is not True:
        raise ValueError('finished_restored_run_required')
    rows={};baseline=d._baseline_cohorts(config,list(d.SOURCES))
    if baseline is None:raise ValueError('authoritative_cohorts_required')
    for provider in d.SOURCES:
        original=receipt.get('sources',{}).get(provider,{})
        row=d.reconstruct_source_receipt(config,receipt,provider)
        row=d.merge_source_history(row,original)
        ref=d.read_json(target/(provider+'-collection.json'))
        if ref:
            collection=m.report(config['journal'],ref['plan_id'])
            if collection['plan'].get('run_id')!=run_id or collection['plan'].get('source')!=provider:
                raise ValueError('collection_run_binding_invalid')
            plan=d.read_json(target/(provider+'-plan.json'))
            if plan:
                m._validate_plan(plan)
                if plan['config']['providers']!=[provider]:raise ValueError('publication_source_mismatch')
                try:publication=m.report(config['journal'],plan['plan_id'])
                except FileNotFoundError:
                    with m.read_connection(config['database']) as db:
                        count=db.execute('''SELECT count(*) FROM crawl_runs r JOIN companies c ON c.id=r.company_id
                            WHERE c.slug=? AND r.started_at=?''',(provider,collection['plan']['started_at'])).fetchone()[0]
                    if count==0:
                        row.update(outcome='publication_failed',publication_outcome='not_started',
                            accounting_status='reconciled_pre_lifecycle_failure',failure_stage='publication_preparation',
                            reconciliation_proof=dict(no_matching_crawl_transaction=True,publication_plan_id=plan['plan_id']))
                else:
                    if plan!=publication['plan']:raise ValueError('publication_plan_binding_invalid')
                    if not row['qualifying_observation']:
                        recovered=legacy_transaction(config,receipt,provider,collection,publication)
                        if recovered:row.update(recovered)
                    if not row['qualifying_observation']:
                        with m.read_connection(config['database']) as db:
                            failed=db.execute('''SELECT r.* FROM crawl_runs r JOIN companies c ON c.id=r.company_id
                                WHERE c.slug=? AND r.started_at=?''',(provider,collection['plan']['started_at'])).fetchall()
                        if len(failed)==1 and failed[0]['status']=='failed':
                            diagnostic=failed[0]['error_message'] or ''
                            if provider=='mindrift' and diagnostic.startswith('Suspicious Mindrift partial crawl:'):
                                row.update(outcome='qualification_failed',qualification_outcome='rejected',failure_stage='existing_count_drop_guard')
                            elif diagnostic=='worker_execution_deadline_expired':
                                row.update(failure_stage='publication_transaction_deadline')
            row['collection_plan_id']=ref['plan_id']
        if baseline.get(provider):
            row['cohorts']=[dict(c,expires_at=d.stamp(d.parse(c['verified_at'])+timedelta(hours=72)) if c['verified_at'] else None) for c in baseline[provider]]
            row['last_qualifying_verification']=max((c['verified_at'] for c in row['cohorts'] if c['verified_at']),default=None)
            row['next_verification_deadline']=min((c['expires_at'] for c in row['cohorts'] if c['expires_at']),default=None)
        row.update(run_id=run_id,trigger=receipt['trigger'],ended_at=receipt['ended_at'])
        rows[provider]=row
    return dict(version=VERSION,run_id=run_id,original_receipt_sha256=m.digest(receipt),
        reconstructed_at=d.stamp(d.now()),code_commit=config['code_commit'],sources=rows,
        inventory_writes=0,provider_requests=0,original_evidence_rewritten=False)


def apply(config,run_id,*,authorized=False):
    if not authorized:raise ValueError('explicit_receipt_reconciliation_authorization_required')
    from wahojobs.maintenance_gate import operation_gate
    root=Path(config['state_directory']);target=root/'runs'/run_id
    with operation_gate(config['database']),operation_gate(root/'health'):
        # One auditable correction for a finished run. Repeated application is
        # a read of its bound result, not another event or a freshness renewal.
        if (target/'current-reconciliation.json').exists():return read_current(target/'run.json')
        prepared=target/'prepared-reconciliation.json'
        correction=d.read_json(prepared)
        if correction is None:
            correction=build(config,run_id)
            m.save_json(prepared,correction)
        _validate_correction(correction,d.read_json(target/'run.json'))
        identifier=m.digest(correction)
        folder=target/'reconciliations';folder.mkdir(mode=0o700,exist_ok=True)
        audit=folder/(identifier+'.json')
        if audit.exists():
            if d.read_json(audit)!=correction:raise ValueError('immutable_reconciliation_mismatch')
        else:m.save_json(audit,correction)
        # This single atomic pointer is the only visibility boundary. Individual
        # source state, the original run and its delivery history stay immutable.
        m.save_json(target/'current-reconciliation.json',dict(report_id=identifier))
        return read_current(target/'run.json')
