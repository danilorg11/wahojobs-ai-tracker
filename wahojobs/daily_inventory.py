"""Bounded daily inventory policy and stored-state health; dormant on import.

The product database and maintenance journal remain the evidence authority.
These small operator receipts never change candidate eligibility or user data.
"""
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import uuid

from wahojobs import evidence_maintenance as maintenance

from wahojobs.daily_source_policy import (CORE_SOURCES, POLICY, READY_SOURCES, ALERT_RECIPIENT,
    default_sources, validate_sources, aggregate)
SOURCES=CORE_SOURCES
EXECUTION_SECONDS=aggregate(default_sources())['execution_seconds']
RECOVERY_SECONDS=120
CATCH_UP_SECONDS=3600
VERSION='daily_inventory_v1_all_sources'
SUCCESSFUL_RUN_OUTCOMES=('complete','partial_individual','complete_with_coverage_gaps')
DATABASE='/var/lib/wahojobs-beta/rehearsal-recovered-001/product.sqlite3'
JOURNAL='/var/lib/wahojobs-beta/rehearsal-recovered-001/journal'
RUNTIME='/etc/wahojobs-beta/config-002/runtime.json'
HOST='wahojobs-private-beta-rehearsal-20260917'
SERVICE='wahojobs-beta.service'
INVENTORY_TABLES={'companies','canonical_opportunities','jobs','crawl_runs','job_events',
    'job_source_contents','job_source_content_captures','job_source_content_acceptances',
    'opportunity_enrichments','sqlite_sequence'}


def now():return datetime.now(timezone.utc)


def stamp(value):return value.astimezone(timezone.utc).isoformat()


def parse(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('aware_operational_time_required')
    return result.astimezone(timezone.utc)


def slot_at(at):
    candidate=at.astimezone(timezone.utc).replace(hour=6,minute=0,second=0,microsecond=0)
    return candidate if candidate<=at else candidate-timedelta(days=1)


def next_trigger(at):return slot_at(at)+timedelta(days=1)


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x',encoding='utf8') as stream:
            json.dump(value,stream,sort_keys=True,ensure_ascii=False,allow_nan=False)
            stream.flush();os.fsync(stream.fileno())
        if os.name=='posix' and os.geteuid()==0:
            parent=path.parent.stat();os.chown(temporary,parent.st_uid,parent.st_gid)
        os.replace(temporary,path)
        if os.name=='posix':
            fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
    finally:
        if temporary.exists():temporary.unlink()


def read_json(path,default=None):
    try:return json.loads(Path(path).read_text(encoding='utf8'))
    except FileNotFoundError:return default


def validate_policy(config,*,activation=False):
    if (config.get('version')!=VERSION or type(config.get('enabled')) is not bool
            or config.get('database')!=DATABASE or config.get('journal')!=JOURNAL
            or config.get('runtime_config')!=RUNTIME or config.get('host')!=HOST
            or config.get('schedule')!='06:00 UTC'
            or config.get('execution_seconds')!=aggregate(validate_sources(config.get('sources')))['execution_seconds']
            or config.get('recovery_seconds')!=RECOVERY_SECONDS
            or config.get('catch_up_seconds')!=CATCH_UP_SECONDS
            or config.get('configuration_revision')!='config-002'
            or config.get('details')!=0 or config.get('retries')!=0 or config.get('model_calls')!=0
            or config.get('state_directory')!='/var/lib/wahojobs-beta/daily-inventory-v1'):
        raise ValueError('incompatible_daily_policy')
    if (config.get('alert_delivery') or {}).get('recipient') != ALERT_RECIPIENT:
        raise ValueError('approved_operational_recipient_required')
    commit=config.get('code_commit','')
    import re
    if not re.fullmatch('[a-f0-9]{40}',commit):raise ValueError('exact_release_required')
    first=parse(config['first_run_at'])
    if slot_at(first)!=first:raise ValueError('first_run_must_be_0600_utc')
    if activation:
        delivery=config.get('alert_delivery') or {}
        if (not config['enabled'] or delivery.get('approved') is not True
                or delivery.get('recipient')!=ALERT_RECIPIENT or not delivery.get('command')
                or not isinstance(delivery['command'],list)
                or not Path(delivery['command'][0]).is_absolute()):
            raise ValueError('activation_and_approved_alert_delivery_required')
    return config


def reserve_run(directory,at,first_run,trigger):
    if trigger not in ('timer','manual','restart','timer_catch_up','unknown'):raise ValueError('invalid_trigger')
    slot=slot_at(at)
    if slot<first_run:return None
    run_id=slot.strftime('%Y%m%dT060000Z')
    target=Path(directory)/'runs'/run_id
    try:target.mkdir(parents=True,exist_ok=False)
    except FileExistsError:return None  # Consumed even if interrupted before dispatch.
    if os.name=='posix' and os.geteuid()==0:
        parent=target.parent.stat()
        os.chown(target,parent.st_uid,parent.st_gid)
    receipt=dict(version=VERSION,run_id=run_id,trigger=trigger,scheduled_at=stamp(slot),
        started_at=stamp(at),ended_at=None,outcome='reserved',sources={},
        next_scheduled_execution=stamp(next_trigger(at)),maintenance_seconds=0)
    if (at-slot).total_seconds()>CATCH_UP_SECONDS:
        receipt.update(outcome='missed_window',ended_at=stamp(at))
    write_json(target/'run.json',receipt)
    return receipt


def protected_domains(database):
    """Hashes only; no profiles, identities or history enter operational logs."""
    result={}
    paths={'product':Path(database),'drafts':Path(str(database)+'.correction-drafts.sqlite3')}
    for scope,path in paths.items():
        if not path.exists():
            if scope=='product':raise ValueError('product_missing')
            continue
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
            db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            schema=db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
            tables={}
            for kind,name,_,_ in schema:
                if kind!='table' or scope=='product' and name in INVENTORY_TABLES:continue
                quoted='"'+name.replace('"','""')+'"'
                values=sorted(sha256(repr(tuple(r)).encode()).hexdigest() for r in db.execute('SELECT * FROM '+quoted))
                tables[name]=dict(count=len(values),sha256=sha256(''.join(values).encode()).hexdigest())
            result[scope]=dict(schema_sha256=sha256(repr(schema).encode()).hexdigest(),tables=tables)
    return result


def summarize_source(plan,report,started,ended):
    provider=plan['config']['providers'][0]
    events=report.get('events',[])
    results=[e['data'] for e in events if e['event']=='operation_result' and e['data']['operation']=='catalog:'+provider]
    finished=[e['data'] for e in events if e['event']=='finished']
    requests=[e for e in events if e['event']=='source_transport' and e['data'].get('event')=='request']
    row=dict(provider=provider,plan_id=plan['plan_id'],started_at=stamp(started),ended_at=stamp(ended),
        outcome='failed',qualifying_observation=False,requests_used=len(requests),request_unit='HTTP attempts',
        count_unit='exact posting records (variants)', observed=None,new=None,changed=None,reconfirmed=None,
        confirmed_closed=None,missing=None,uncertain=None,stale=None,cohorts=[],last_qualifying_verification=None,
        next_verification_deadline=None,next_scheduled_execution=stamp(next_trigger(ended)))
    if finished:row['requests_used']=finished[-1]['request_usage']['http_transactions']
    responses=[e['data'] for e in events if e['event']=='source_transport' and e['data'].get('event')=='response']
    row.update(http_responses_received=len({e['ordinal'] for e in responses}),
        pages_fetched=sum(1 for e in responses if 200<=e.get('status',0)<300),
        page_unit='successful HTTP response pages (API pages and public probes, not records)',
        surface_coverage=POLICY[provider]['verification_rule'], upstream_records=None,
        upstream_record_unit=None, observed_canonical_opportunities=None,new_canonical_opportunities=None,
        filtered_records=None,held_catalog_content=None,content_hold_reasons={},
        request_cap_reached=row['requests_used']>=plan['config'].get('http_limit',POLICY[provider]['http_max']))
    envelopes=[e['data'] for e in events if e['event']=='source_transport' and e['data'].get('event')=='envelope_shape']
    if envelopes:row['listing_envelope_shape']=envelopes[-1]
    before={j['job_id']:j for j in plan['sources'][0]['jobs']}
    if not results or 'summary' not in results[-1].get('result',{}):return row
    result=results[-1]['result'];summary=result['summary'];state=result['after'];run=state.get('latest_run') or {}
    run_id=run.get('id')
    good=[j for j in state['jobs'] if j['verification'].get('source_run_id')==run_id
          and j['verification'].get('latest_successful_source_run_at')]
    valid=(row['requests_used']>0 and not summary['used_sample_data'] and run.get('status') in ('success','partial')
        and summary['normalized_record_count']==summary['jobs_found']
        and summary['raw_record_count']==summary['normalized_record_count']+summary['rejected_record_count']+summary.get('filtered_record_count',0))
    qualifies=valid and (bool(good) if provider=='mercor' else
        summary['snapshot_complete'] and summary['pagination_complete'] and run.get('status')=='success')
    row.update(qualifying_observation=bool(qualifies),outcome=('partial_individual' if provider=='mercor' else 'complete') if qualifies else 'partial_or_failed',
        observed=summary['jobs_found'],new=summary['jobs_new'],confirmed_closed=summary['jobs_removed'])
    surfaces=[e['data'] for e in events if e['event']=='source_transport' and e['data'].get('event')=='surface_counts']
    surface=surfaces[-1] if surfaces else {}
    row.update(upstream_records=surface.get('upstream_records',summary['raw_record_count']),
        upstream_record_unit=surface.get('upstream_unit','source listing rows'),
        filtered_records=surface.get('filtered_records',summary.get('filtered_record_count',0)),
        normalized_variants=summary['normalized_record_count'],rejected_records=summary['rejected_record_count'])
    if provider in ('oneforma','mindrift') and not surfaces:
        # Legacy wrapper raw_count is normalized variants. Never mislabel it as
        # the upstream posts/rows when the actual collector counter is absent.
        row.update(upstream_records=None,upstream_record_unit='not retained')
    # jobs_updated in the old tracker includes identical reconfirmations. Compare
    # actual accepted semantic hashes instead of labelling every sighting changed.
    observed_ids={j['job_id'] for j in good}
    canonicals={j.get('canonical_id') for j in good if j.get('canonical_id') is not None}
    prior_canonicals={j.get('canonical_id') for j in before.values() if j.get('canonical_id') is not None}
    row.update(observed_canonical_opportunities=len(canonicals),
        new_canonical_opportunities=len(canonicals-prior_canonicals))
    held=[j for j in state['jobs'] if j['evidence'].get('latest_capture_id') is not None
        and j['evidence'].get('latest_capture_id')!=j['evidence'].get('accepted_capture_id')]
    row.update(held_catalog_content=len(held),
        content_hold_reasons=dict(Counter(reason for j in held for reason in j["evidence"].get("latest_decision_reasons",[]))),
        content_hold_note='Availability reconfirmation does not mean held catalog content replaced dated detail. Inspect the existing maintenance evidence report; no daily detail requests.')
    row['changed']=sum(j['job_id'] in before and j['job_id'] in observed_ids and
        j['evidence']['accepted_semantic_material_sha256']!=before[j['job_id']]['evidence']['accepted_semantic_material_sha256'] for j in state['jobs'])
    row['reconfirmed']=max(0,len(good)-row['new']-row['changed'])
    active=[j for j in state['jobs'] if j['verification']['status']!='inactive']
    row['missing']=sum(j['job_id'] not in observed_ids for j in active)
    row['uncertain']=summary['rejected_record_count']+max(0,summary['jobs_found']-len(good))
    dates=Counter(j['verification'].get('latest_successful_source_run_at') for j in active)
    row['cohorts']=[dict(verified_at=date,records=count,expires_at=stamp(parse(date)+timedelta(hours=72)) if date else None)
        for date,count in sorted(dates.items(),key=lambda p:p[0] or '')]
    row['stale']=sum(c['records'] for c in row['cohorts'] if not c['verified_at'] or ended-parse(c['verified_at'])>timedelta(hours=72))
    qualifying_dates=[j['verification']['latest_successful_source_run_at'] for j in good]
    row['last_qualifying_verification']=max(qualifying_dates,default=None) if qualifies else None
    deadlines=[c['expires_at'] for c in row['cohorts'] if c['expires_at']]
    row['next_verification_deadline']=min(deadlines,default=None)
    return row


def source_settings(config):
    return config.get('sources', default_sources())


def execution_seconds(config):
    return aggregate(source_settings(config))['execution_seconds']


def coverage_plan(config, database, at):
    """Account for every core source; daily due is separate from TTL eligibility."""
    result={}
    with maintenance.read_connection(database) as db:
        for source, settings in source_settings(config).items():
            policy=POLICY[source]
            row=dict(source=source, **settings, readiness=policy['readiness'],
                state='due', reason=None, next_eligible_at=None,
                verification_rule=policy['verification_rule'])
            if policy['readiness']=='blocked':row.update(state='blocked',reason=policy['blocker'])
            elif not settings['enabled']:row.update(state='disabled',reason='owner_configuration_disabled')
            else:
                company=db.execute('SELECT id FROM companies WHERE slug=?',(source,)).fetchone()
                if not company:row.update(state='blocked',reason='source_not_configured_in_authoritative_database')
                elif policy['cooldown_hours']:
                    last=db.execute("SELECT started_at FROM crawl_runs WHERE company_id=? AND status='success' AND used_sample_data=0 AND error_message IS NULL ORDER BY started_at DESC,id DESC LIMIT 1",(company['id'],)).fetchone()
                    if last:
                        due=parse(last[0])+timedelta(hours=policy['cooldown_hours'])
                        if at<due:row.update(state='cooldown',reason='source_success_cooldown',next_eligible_at=stamp(due))
            result[source]=row
    return result


def empty_source(source, at, *, outcome, reason=None):
    row=summarize_source(dict(plan_id=None,config=dict(providers=[source]),sources=[dict(jobs=[])]),{},at,at)
    row.update(outcome=outcome,reason=reason)
    return row


def save_source(config, run_id, summary):
    directory=Path(config['state_directory']);source=summary['provider'];target=directory/'runs'/run_id
    previous=read_json(directory/(source+'-state.json'),{})
    summary=merge_source_history(summary,previous)
    summary.update(run_id=run_id,trigger=read_json(target/'run.json',{}).get('trigger','isolated_worker'))
    write_json(target/(source+'.json'),summary)
    write_json(directory/(source+'-state.json'),summary)


def collect_phase(config, run_id, phase):
    """One backup, independently bounded source processes, one integrity finish.

    Native parent holds the common operation gate throughout. Each phase obtains
    the existing lifetime lease; a killed source releases it before the next one.
    No provider can spend a sibling's request or time allowance.
    """
    from wahojobs.database_lifetime_ownership import acquire_database_lifetime_ownership,release_database_lifetime_ownership,ROLE_OFFLINE_OPERATOR
    from wahojobs.beta_recovery import create_snapshot,verify_snapshot
    database=Path(config['database']);directory=Path(config['state_directory']);target=directory/'runs'/run_id
    if phase not in ('backup','finish',*SOURCES):raise ValueError('invalid_worker_phase')
    lease=acquire_database_lifetime_ownership(database,role=ROLE_OFFLINE_OPERATOR)
    try:
        binding=maintenance.journal_binding(database)
        if not binding or Path(binding['journal_root'])!=Path(config['journal']):raise ValueError('authoritative_journal_mismatch')
        if phase=='backup':
            before=protected_domains(database)
            snapshot=directory/'backups'/run_id
            snapshot.parent.mkdir(parents=True,exist_ok=True)
            create_snapshot(database,snapshot,code_commit=config['code_commit'],configuration_revision='config-002',ownership=lease)
            manifest=verify_snapshot(snapshot)
            write_json(target/'backup.json',dict(verified=True,files=len(manifest['files']),protected_domains=before))
            plan=coverage_plan(config,database,now())
            write_json(target/'coverage-plan.json',plan)
            for source,row in plan.items():
                if row['state']!='due':
                    summary=empty_source(source,now(),outcome=row['state'],reason=row['reason'])
                    summary['next_eligible_at']=row['next_eligible_at']
                    save_source(config,run_id,summary)
        elif phase=='finish':
            before=read_json(target/'backup.json')
            if not before or before.get('verified') is not True:raise ValueError('verified_backup_required')
            if before['protected_domains']!=protected_domains(database):raise ValueError('protected_domain_changed')
            with maintenance.read_connection(database) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('post_collection_integrity_failed')
            write_json(target/'worker.json',dict(completed=True,protected_domains_unchanged=True))
        else:
            source=phase
            schedule=read_json(target/'coverage-plan.json')
            if not schedule or schedule[source]['state']!='due' or not source_settings(config)[source]['enabled']:
                raise ValueError('source_not_due_in_reserved_plan')
            cap=source_settings(config)[source]['http_max'];started=now()
            plan=maintenance.build_plan(database,[source],http_limit=cap,detail_limit=0,
                details=None,phase='source',daily_discovery=True)
            operations=[o for o in plan['operations'] if o['kind']=='catalog_observation']
            if len(operations)!=1 or operations[0]['blocked'] or operations[0]['details'] is not None:
                raise ValueError('daily_catalog_contract_incompatible')
            maintenance.save_json(target/(source+'-plan.json'),plan)
            result=maintenance.execute_plan(plan,config['journal'],authorized=True,authorize_sources=True,ownership=lease)
            summary=summarize_source(plan,result,started,now())
            if summary['requests_used']>cap:raise ValueError('daily_request_limit_violated')
            save_source(config,run_id,summary)
    finally:release_database_lifetime_ownership(lease,role=ROLE_OFFLINE_OPERATOR,database_path=database)


def collect(config,run_id):
    """Isolated callable path; native execution adds a process deadline per phase."""
    from wahojobs.crawler.local_inventory import request_deadline
    import time
    collect_phase(config,run_id,'backup')
    schedule=read_json(Path(config['state_directory'])/'runs'/run_id/'coverage-plan.json')
    for source,row in schedule.items():
        if row['state']!='due':continue
        try:
            with request_deadline(time.monotonic()+row['seconds_max']):collect_phase(config,run_id,source)
        except Exception as error:
            write_json(Path(config['state_directory'])/'runs'/run_id/(source+'-failure.json'),dict(error_type=type(error).__name__))
    collect_phase(config,run_id,'finish')


def health_issues(config,at):
    directory=Path(config['state_directory']);issues={}
    slot=slot_at(at);first=parse(config['first_run_at'])
    try:
        latest=read_json(directory/'runs'/slot.strftime('%Y%m%dT060000Z')/'run.json')
    except (OSError,ValueError):
        latest=None
        issues['run:unreadable']=dict(severity='error',reason='expected_run_receipt_unreadable')
    grace=timedelta(seconds=execution_seconds(config)+RECOVERY_SECONDS+60)
    if slot>=first and at-slot<=grace and (not latest or latest.get('outcome') not in SUCCESSFUL_RUN_OUTCOMES):
        # Starting a new calendar day is not recovery from a missed/failed run.
        previous=read_json(directory/'health.json',{'active':{}})
        issues.update({k:v for k,v in previous['active'].items() if k in ('run:missing','run:failed')})
    if slot>=first and at-slot>grace:
        if latest is None:issues['run:missing']=dict(severity='error',reason='expected_daily_run_missing',scheduled_at=stamp(slot))
        elif latest['outcome'] not in SUCCESSFUL_RUN_OUTCOMES:
            issues['run:failed']=dict(severity='error',reason=latest['outcome'],run_id=latest['run_id'])
    for provider in SOURCES:
        policy=POLICY[provider]
        if policy['readiness']=='blocked' or not source_settings(config)[provider]['enabled']:
            issues[provider+':coverage']=dict(severity='warning',reason=policy['blocker'] or 'owner_configuration_disabled',
                corrective_action=policy['corrective_action'], readiness=policy['readiness'])
        try:
            source=read_json(directory/(provider+'-state.json'))
        except (OSError,ValueError):
            source=None
        if source is None:
            issues[provider+':unverified']=dict(severity='error',reason='no_stored_qualifying_state');continue
        if not source.get('qualifying_observation'):
            issues[provider+':collection']=dict(severity='error',reason=source['outcome'])
        elif source['outcome']=='partial_individual':
            issues[provider+':partial']=dict(severity='info',reason='partial_catalog_individual_verification_only')
        if source.get('abnormal_count_drop'):
            issues[provider+':count_drop']=dict(severity='error',reason='observed_record_count_dropped',observed=source['observed'])
        cohorts=source.get('cohorts',[])
        for label,hours,severity in (('age36',36,'warning'),('age48',48,'error')):
            aged=[c for c in cohorts if not c['verified_at'] or at-parse(c['verified_at'])>=timedelta(hours=hours)]
            if aged:
                issues[provider+':'+label]=dict(severity=severity,reason='verification_cohort_age',threshold_hours=hours,
                    records=sum(c['records'] for c in aged),unit='exact posting records',
                    oldest_verification=min((c['verified_at'] for c in aged if c['verified_at']),default=None))
    return issues


def health(config,at=None):
    """Durable deduplicated outbox. No employer or delivery calls here."""
    at=at or now();directory=Path(config['state_directory']);issues=health_issues(config,at)
    previous=read_json(directory/'health.json',{'active':{},'events':[]})
    # Stable issue keys deduplicate repeated hourly checks and changing age/counts.
    events=list(previous['events'])
    for key in sorted(set(issues)-set(previous['active'])):
        events.append(dict(id=uuid.uuid4().hex,kind='opened',key=key,at=stamp(at),issue=issues[key],delivery='pending'))
    for key in sorted(set(previous['active'])-set(issues)):
        events.append(dict(id=uuid.uuid4().hex,kind='recovered',key=key,at=stamp(at),delivery='pending'))
    result=dict(checked_at=stamp(at),next_scheduled_execution=stamp(max(next_trigger(at),parse(config['first_run_at']))),active=issues,events=events)
    write_json(directory/'health.json',result);return result


def merge_source_history(summary,previous):
    baseline=previous.get('count_baseline',previous.get('observed')) or 0
    observed=summary['observed']
    summary['abnormal_count_drop']=(previous.get('abnormal_count_drop',False) if observed is None else
        bool(baseline>=10 and observed<baseline*.5))
    summary['count_baseline']=baseline if observed is None or summary['abnormal_count_drop'] else observed
    if not summary['cohorts']:
        summary['cohorts']=previous.get('cohorts',[])
    if not summary['last_qualifying_verification']:
        summary['last_qualifying_verification']=previous.get('last_qualifying_verification')
    if not summary['next_verification_deadline']:
        summary['next_verification_deadline']=previous.get('next_verification_deadline')
    if summary.get('ended_at') and summary['cohorts']:
        at=parse(summary['ended_at'])
        summary['stale']=sum(c['records'] for c in summary['cohorts'] if not c['verified_at'] or at-parse(c['verified_at'])>timedelta(hours=72))
    return summary


def finish_run_sources(config,receipt):
    """Complete timeout/undispatched summaries using retained records after restore.

    No database or employer access. An interrupted journal's reservations count
    as consumed requests; missing/corrupt accounting is unknown, never zero.
    """
    directory=Path(config['state_directory']);target=directory/'runs'/receipt['run_id']
    rows={}
    for provider in SOURCES:
        plan=None
        previous=read_json(directory/(provider+'-state.json'),{})
        newer_state=previous.get('run_id','')>receipt['run_id']
        history={} if newer_state else previous
        try:
            row=read_json(target/(provider+'.json'))
            if row is None:
                plan=read_json(target/(provider+'-plan.json'))
                scaffold=plan or dict(plan_id=None,config=dict(providers=[provider]),sources=[dict(jobs=[])])
                report=maintenance.report(config['journal'],plan['plan_id']) if plan else {}
                row=summarize_source(scaffold,report,parse(receipt['started_at']),now())
                if not plan:
                    schedule=read_json(target/'coverage-plan.json',{})
                    policy=POLICY[provider];scheduled=schedule.get(provider,{})
                    row.update(outcome=scheduled.get('state','blocked' if policy['readiness']=='blocked' else 'not_started'),reason=scheduled.get('reason',policy['blocker']))
                    if row['outcome']=='due':row['outcome']='not_started'
                failure=read_json(target/(provider+'-failure.json'))
                if failure:row.update(outcome='interrupted_or_failed',error_type=failure['error_type'])
                elif report.get('status')=='interrupted':row.update(outcome='interrupted',qualifying_observation=False)
                row=merge_source_history(row,history)
        except (OSError,ValueError,KeyError,TypeError):
            row=summarize_source(dict(plan_id=None,config=dict(providers=[provider]),sources=[dict(jobs=[])]),{},parse(receipt['started_at']),now())
            row.update(outcome='accounting_unavailable',requests_used=None)
            try:row=merge_source_history(row,history)
            except (OSError,ValueError,KeyError,TypeError):pass
        row.update(run_id=receipt['run_id'],trigger=receipt['trigger'],maintenance_seconds=receipt.get('maintenance_seconds'))
        rows[provider]=row
        write_json(target/(provider+'.json'),row)
        if not newer_state:write_json(directory/(provider+'-state.json'),row)
    receipt['sources']=rows
