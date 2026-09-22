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

SOURCES={'alignerr':100,'mercor':1}
EXECUTION_SECONDS=900
RECOVERY_SECONDS=120
CATCH_UP_SECONDS=3600
VERSION='daily_inventory_v1'
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
            or config.get('sources')!=SOURCES or config.get('schedule')!='06:00 UTC'
            or config.get('execution_seconds')!=EXECUTION_SECONDS
            or config.get('recovery_seconds')!=RECOVERY_SECONDS
            or config.get('catch_up_seconds')!=CATCH_UP_SECONDS
            or config.get('configuration_revision')!='config-002'
            or config.get('details')!=0 or config.get('retries')!=0 or config.get('model_calls')!=0
            or config.get('state_directory')!='/var/lib/wahojobs-beta/daily-inventory-v1'):
        raise ValueError('incompatible_daily_policy')
    commit=config.get('code_commit','')
    import re
    if not re.fullmatch('[a-f0-9]{40}',commit):raise ValueError('exact_release_required')
    first=parse(config['first_run_at'])
    if slot_at(first)!=first:raise ValueError('first_run_must_be_0600_utc')
    if activation:
        delivery=config.get('alert_delivery') or {}
        if (not config['enabled'] or delivery.get('approved') is not True
                or not delivery.get('recipient') or not delivery.get('command')
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
    before={j['job_id']:j for j in plan['sources'][0]['jobs']}
    if not results or 'summary' not in results[-1].get('result',{}):return row
    result=results[-1]['result'];summary=result['summary'];state=result['after'];run=state.get('latest_run') or {}
    run_id=run.get('id')
    good=[j for j in state['jobs'] if j['verification'].get('source_run_id')==run_id
          and j['verification'].get('latest_successful_source_run_at')]
    valid=(row['requests_used']>0 and not summary['used_sample_data'] and run.get('status') in ('success','partial')
        and summary['normalized_record_count']==summary['jobs_found']
        and summary['raw_record_count']==summary['normalized_record_count']+summary['rejected_record_count'])
    qualifies=valid and (bool(good) if provider=='mercor' else
        summary['snapshot_complete'] and summary['pagination_complete'] and run.get('status')=='success')
    row.update(qualifying_observation=bool(qualifies),outcome=('partial_individual' if provider=='mercor' else 'complete') if qualifies else 'partial_or_failed',
        observed=summary['jobs_found'],new=summary['jobs_new'],confirmed_closed=summary['jobs_removed'])
    # jobs_updated in the old tracker includes identical reconfirmations. Compare
    # actual accepted semantic hashes instead of labelling every sighting changed.
    observed_ids={j['job_id'] for j in good}
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


def collect(config,run_id):
    """One child process, one lifetime lease, verified backup, two bounded plans."""
    from wahojobs.database_lifetime_ownership import acquire_database_lifetime_ownership,release_database_lifetime_ownership,ROLE_OFFLINE_OPERATOR
    from wahojobs.beta_recovery import create_snapshot,verify_snapshot
    database=Path(config['database']);directory=Path(config['state_directory']);target=directory/'runs'/run_id
    lease=acquire_database_lifetime_ownership(database,role=ROLE_OFFLINE_OPERATOR)
    try:
        binding=maintenance.journal_binding(database)
        if not binding or Path(binding['journal_root'])!=Path(config['journal']):raise ValueError('authoritative_journal_mismatch')
        before=protected_domains(database)
        snapshot=directory/'backups'/run_id
        snapshot.parent.mkdir(parents=True,exist_ok=True)
        create_snapshot(database,snapshot,code_commit=config['code_commit'],configuration_revision='config-002',ownership=lease)
        manifest=verify_snapshot(snapshot)
        write_json(target/'backup.json',dict(verified=True,files=len(manifest['files'])))
        for provider,cap in SOURCES.items():
            from wahojobs.crawler.local_inventory import remaining_request_seconds
            remaining_request_seconds()
            started=now()
            plan=maintenance.build_plan(database,[provider],http_limit=cap,detail_limit=0,
                details=None,phase='source',daily_discovery=True)
            # Fail closed if adapter/configuration policy changes unexpectedly.
            operations=[o for o in plan['operations'] if o['kind']=='catalog_observation']
            if len(operations)!=1 or operations[0]['blocked'] or operations[0]['details'] is not None:
                raise ValueError('daily_catalog_contract_incompatible')
            maintenance.save_json(target/(provider+'-plan.json'),plan)
            result=maintenance.execute_plan(plan,config['journal'],authorized=True,authorize_sources=True,ownership=lease)
            summary=summarize_source(plan,result,started,now())
            if summary['requests_used']>cap:raise ValueError('daily_request_limit_violated')
            previous=read_json(directory/(provider+'-state.json'),{})
            summary=merge_source_history(summary,previous)
            summary.update(run_id=run_id,trigger=read_json(target/'run.json',{}).get('trigger','isolated_worker'))
            write_json(target/(provider+'.json'),summary)
            # Persist all cohorts even on partial qualification, including expired absent records.
            write_json(directory/(provider+'-state.json'),summary)
        if before!=protected_domains(database):raise ValueError('protected_domain_changed')
        with maintenance.read_connection(database) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('post_collection_integrity_failed')
        write_json(target/'worker.json',dict(completed=True,protected_domains_unchanged=True))
    finally:release_database_lifetime_ownership(lease,role=ROLE_OFFLINE_OPERATOR,database_path=database)


def health_issues(config,at):
    directory=Path(config['state_directory']);issues={}
    slot=slot_at(at);first=parse(config['first_run_at'])
    try:
        latest=read_json(directory/'runs'/slot.strftime('%Y%m%dT060000Z')/'run.json')
    except (OSError,ValueError):
        latest=None
        issues['run:unreadable']=dict(severity='error',reason='expected_run_receipt_unreadable')
    if slot>=first and at-slot<=timedelta(minutes=20) and (not latest or latest.get('outcome') not in ('complete','partial_individual')):
        # Starting a new calendar day is not recovery from a missed/failed run.
        previous=read_json(directory/'health.json',{'active':{}})
        issues.update({k:v for k,v in previous['active'].items() if k in ('run:missing','run:failed')})
    if slot>=first and at-slot>timedelta(minutes=20):
        if latest is None:issues['run:missing']=dict(severity='error',reason='expected_daily_run_missing',scheduled_at=stamp(slot))
        elif latest['outcome'] not in ('complete','partial_individual'):
            issues['run:failed']=dict(severity='error',reason=latest['outcome'],run_id=latest['run_id'])
    for provider in SOURCES:
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
                if not plan:row['outcome']='not_started'
                elif report['status']=='interrupted':row.update(outcome='interrupted',qualifying_observation=False)
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
