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
# The outage is bounded independently from the online network cycle. Stop,
# cold backup, publication and integrity share this smaller execution allowance.
PUBLICATION_SECONDS=240
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
    pending=[e['data'] for e in events if e['event']=='source_transport'
             and e['data'].get('event')=='pending_qualification'
             and e['data'].get('source')==provider]
    if provider in ('outlier','dataforce'):
        row['pending_qualification_ids']=pending[-1]['identities'] if pending else []
        row['pending_qualification_count']=len(row['pending_qualification_ids'])
        row['pending_qualification_index_sha256']=pending[-1]['index_sha256'] if pending else None
        row['pending_qualification_scope']='identities without a qualifying detail in this run; prior qualification is not inferred'
        row['discovery_scope']=('observed public board; only versioned, individually attested IDs can publish'
            if provider=='outlier' else
            'observed public project index; only exact remote Thyme AI-writing family records with attested details can publish')
    before={j['job_id']:j for j in plan['sources'][0]['jobs']}
    if not results or 'summary' not in results[-1].get('result',{}):return row
    result=results[-1]['result'];summary=result['summary'];state=result['after'];run=state.get('latest_run') or {}
    run_id=run.get('id')
    good=[j for j in state['jobs'] if j['verification'].get('source_run_id')==run_id
          and j['verification'].get('latest_successful_source_run_at')]
    valid=(row['requests_used']>0 and not summary['used_sample_data'] and run.get('status') in ('success','partial')
        and summary['normalized_record_count']==summary['jobs_found']
        and summary['raw_record_count']==summary['normalized_record_count']+summary['rejected_record_count']+summary.get('filtered_record_count',0))
    qualifies=valid and (bool(good) if provider in ('mercor','dataannotation','dataforce','handshake','surge','outlier') else
        summary['snapshot_complete'] and summary['pagination_complete'] and run.get('status')=='success')
    row.update(qualifying_observation=bool(qualifies),outcome=('partial_individual' if provider in ('mercor','dataannotation','dataforce','handshake','surge','outlier') else 'complete') if qualifies else 'partial_or_failed',
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
                else:
                    row['stored_records']=db.execute('SELECT count(*) FROM jobs WHERE company_id=?',(company['id'],)).fetchone()[0]
                if company and policy['cooldown_hours']:
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
    previous=_retain_old_failure(config,source,read_json(directory/(source+'-state.json'),{}))
    summary=merge_source_history(summary,previous)
    summary.update(run_id=run_id,trigger=read_json(target/'run.json',{}).get('trigger','isolated_worker'))
    write_json(target/(source+'.json'),summary)
    write_json(directory/(source+'-state.json'),summary)


def collect_phase(config, run_id, phase):
    """One backup, independently bounded source processes, one integrity finish.

    Native parent holds the common operation gate throughout. Publication obtains
    the existing lifetime lease; online collection changes no product records.
    No provider can spend a sibling's request or time allowance.
    """
    from wahojobs.database_lifetime_ownership import acquire_database_lifetime_ownership,release_database_lifetime_ownership,ROLE_OFFLINE_OPERATOR
    from wahojobs.beta_recovery import create_snapshot,verify_snapshot
    database=Path(config['database']);directory=Path(config['state_directory']);target=directory/'runs'/run_id
    from wahojobs.crawler import staged_observation as staged
    if phase=='prepare':
        # Read-only source/configuration inspection is safe while beta owns the
        # database. No database copy or offline lifetime lease is taken here.
        plan=coverage_plan(config,database,now())
        write_json(target/'coverage-plan.json',plan)
        for source,row in plan.items():
            if row['state']!='due':
                summary=empty_source(source,now(),outcome=row['state'],reason=row['reason'])
                summary['next_eligible_at']=row['next_eligible_at'];save_source(config,run_id,summary)
        return
    if phase.startswith('collect-'):
        source=phase.removeprefix('collect-')
        schedule=read_json(target/'coverage-plan.json')
        if source not in SOURCES or not schedule or schedule[source]['state']!='due' or not source_settings(config)[source]['enabled']:
            raise ValueError('source_not_due_in_reserved_plan')
        with maintenance.read_connection(database) as connection:
            company=connection.execute('SELECT careers_url FROM companies WHERE slug=?',(source,)).fetchone()
        if not company:raise ValueError('configured_source_required')
        binding=maintenance.journal_binding(database)
        if not binding or Path(binding['journal_root'])!=Path(config['journal']):raise ValueError('authoritative_journal_mismatch')
        staged.collect(source,company['careers_url'],target,run_id=run_id,code_commit=config['code_commit'],
            http_max=source_settings(config)[source]['http_max'],journal_root=config['journal'])
        return
    publishing=phase.startswith('publish-')
    if publishing:phase=phase.removeprefix('publish-')
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
            if not read_json(target/'coverage-plan.json'):
                collect_phase(config,run_id,'prepare')
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
            observation=None;collection_report=None
            if publishing:
                observation,collection_report=staged.load(target,source,run_id=run_id,code_commit=config['code_commit'],journal_root=config['journal'],consume=True)
                started=parse(observation.started_at)
            plan=maintenance.build_plan(database,[source],http_limit=cap,detail_limit=0,
                details=None,phase='source',daily_discovery=True)
            operations=[o for o in plan['operations'] if o['kind']=='catalog_observation']
            if len(operations)!=1 or operations[0]['blocked'] or operations[0]['details'] is not None:
                raise ValueError('daily_catalog_contract_incompatible')
            maintenance.save_json(target/(source+'-plan.json'),plan)
            result=maintenance.execute_plan(plan,config['journal'],authorized=True,authorize_sources=True,ownership=lease,
                **({'observation':observation} if observation is not None else {}))
            if observation is not None:
                # Collection accounting stays in its original retained journal.
                # Combine evidence for the report without inventing HTTP attempts
                # in the authoritative publication journal (which must use zero).
                result=staged.publication_report(collection_report,result)
            summary=summarize_source(plan,result,started,now())
            if observation is not None:
                summary.update(collection_plan_id=observation.collection_plan_id,collection_completed_at=observation.completed_at,
                    publication_completed_at=stamp(now()),publication_requests_used=0)
            if summary['requests_used']>cap:raise ValueError('daily_request_limit_violated')
            save_source(config,run_id,summary)
    finally:release_database_lifetime_ownership(lease,role=ROLE_OFFLINE_OPERATOR,database_path=database)


def collect(config,run_id):
    """Isolated callable path; native execution adds a process deadline per phase."""
    from wahojobs.crawler.local_inventory import request_deadline
    import time
    started=now()
    collect_phase(config,run_id,'prepare')
    schedule=read_json(Path(config['state_directory'])/'runs'/run_id/'coverage-plan.json')
    for source,row in schedule.items():
        if row['state']!='due':continue
        try:
            with request_deadline(time.monotonic()+row['seconds_max']):collect_phase(config,run_id,'collect-'+source)
        except Exception as error:
            write_json(Path(config['state_directory'])/'runs'/run_id/(source+'-failure.json'),dict(error_type=type(error).__name__))
    collect_phase(config,run_id,'backup')
    for source,row in schedule.items():
        if row['state']!='due':continue
        try:collect_phase(config,run_id,'publish-'+source)
        except Exception as error:
            write_json(Path(config['state_directory'])/'runs'/run_id/(source+'-failure.json'),dict(error_type=type(error).__name__))
    collect_phase(config,run_id,'finish')
    receipt=read_json(Path(config['state_directory'])/'runs'/run_id/'run.json',
        dict(run_id=run_id,trigger='isolated_worker',started_at=stamp(started),maintenance_seconds=None))
    finish_run_sources(config,receipt)


def _published_partial_cycle(config,receipt):
    if not (type(receipt) is dict and receipt.get('outcome')=='partial_or_failed'
            and receipt.get('normal_service_resumed') is True):
        return False
    try:worker=read_json(Path(config['state_directory'])/'runs'/receipt['run_id']/'worker.json',{})
    except (OSError,ValueError,KeyError,TypeError):return False
    sources=receipt.get('sources') or {}
    if type(worker) is not dict or not _valid_source_rows(receipt):return False
    return (worker.get('completed') is True and worker.get('protected_domains_unchanged') is True
            and any(row.get('qualifying_observation') is True for row in sources.values()))


def _valid_source_rows(receipt):
    rows=receipt.get('sources')
    return (type(rows) is dict and set(rows)==set(SOURCES)
            and all(type(row) is dict for row in rows.values()))


def _cohort_key(provider,verified_at):
    identity=verified_at or 'unknown'
    return provider+':cohort_'+sha256(identity.encode()).hexdigest()[:16]


def _cohort_issue(cohort,at):
    verified=cohort.get('verified_at');records=cohort.get('records')
    if type(records) is not int or records<=0:return None
    if verified:
        age=at-parse(verified)
        if age<timedelta(hours=36):return None
        state=('expired' if age>=timedelta(hours=72) else
               'escalated' if age>=timedelta(hours=48) else 'approaching_expiry')
        expires=stamp(parse(verified)+timedelta(hours=72))
    else:state='verification_unknown';expires=None
    return dict(severity='warning' if state=='approaching_expiry' else 'error',
        reason='verification_cohort_age',state=state,records=records,unit='exact posting records',
        verified_at=verified,expires_at=expires,closure_confirmed=False)


def _baseline_cohorts(config,providers):
    """Read exact-record verification when daily state has no usable cohorts."""
    if not providers:return {}
    from wahojobs.source_verification import SOURCE_VERIFICATION_FIELDS,SOURCE_VERIFICATION_JOINS
    result={source:[] for source in providers}
    try:
        with maintenance.read_connection(config['database']) as db:
            rows=db.execute(f'''SELECT c.slug,{SOURCE_VERIFICATION_FIELDS}
                FROM jobs j JOIN companies c ON c.id=j.company_id
                {SOURCE_VERIFICATION_JOINS}
                WHERE j.is_active=1 AND c.slug IN ({','.join('?' for _ in providers)})''',providers)
            counts={source:Counter() for source in providers}
            for row in rows:
                if row['source_run_qualifies']:
                    counts[row['slug']][row['latest_successful_source_run_at']]+=1
            for provider,dates in counts.items():
                result[provider]=[dict(verified_at=date,records=count) for date,count in dates.items()]
    except (OSError,sqlite3.Error,ValueError):return None
    return result


def _source_attempt(config,source):
    plan_id=source.get('last_failed_collection_plan_id') or source.get('collection_plan_id')
    if not plan_id:return {}
    try:
        report=maintenance.report(config['journal'],plan_id)
        if report['plan'].get('source')!=source['provider']:return {}
        requests=[e['data'] for e in report['events'] if e['event']=='source_transport'
                  and e['data'].get('event')=='request']
        errors=[e['data'] for e in report['events'] if e['event']=='source_transport'
                and e['data'].get('event')=='transport_error']
        return dict(at=requests[-1].get('observed_at') if requests else None,
                    status=errors[-1].get('status') if errors else None)
    except (OSError,ValueError,KeyError,TypeError):return {}


def _retained_failed_source(config,provider):
    """Find the newest prior attempt when old disabled state lacks provenance.

    Inspect at most 90 retained daily rows. A later qualifying observation
    supersedes the failure; a disabled row alone does not erase it.
    """
    import re
    root=Path(config['state_directory'])/'runs'
    try:run_ids=sorted((p.name for p in root.iterdir() if p.is_dir() and
        re.fullmatch(r'\d{8}T060000Z',p.name)),reverse=True)[:90]
    except OSError:return None
    for run_id in run_ids:
        try:row=read_json(root/run_id/(provider+'.json'))
        except (OSError,ValueError):continue
        if type(row) is not dict:continue
        if row.get('qualifying_observation') is True:return None
        if row.get('outcome') in ('collection_failed_or_interrupted','interrupted_or_failed','failed','partial_or_failed'):
            return row
    return None


def _retain_old_failure(config,provider,previous):
    if previous.get('outcome')=='disabled' and not previous.get('last_failed_collection_plan_id'):
        older=_retained_failed_source(config,provider)
        if older and older.get('collection_plan_id'):
            return dict(previous,last_failed_collection_plan_id=older['collection_plan_id'])
    return previous


def _disabled_coverage(config,provider,source):
    if source and not source.get('last_failed_collection_plan_id') and source.get('outcome')=='disabled':
        source=_retained_failed_source(config,provider) or source
    failed=bool(source and (source.get('last_failed_collection_plan_id') or
                (source.get('qualifying_observation') is False and
                 source.get('outcome') in ('collection_failed_or_interrupted','interrupted_or_failed','failed','partial_or_failed'))))
    attempt=_source_attempt(config,source) if failed else {}
    if attempt.get('status')==403:
        return dict(severity='warning',reason='disabled_after_http_403',readiness='ready',enabled=False,
            http_status=403,last_attempt_at=attempt.get('at'),
            corrective_action='Resolve authorized access to the existing endpoint, then perform bounded validation before re-enabling.')
    if failed:
        return dict(severity='warning',reason='disabled_after_failed_collection',readiness='ready',enabled=False,
            last_attempt_at=attempt.get('at'),
            corrective_action='Review retained collection failure and complete bounded validation before re-enabling.')
    return dict(severity='warning',reason='configured_disabled',readiness='ready',enabled=False,
        corrective_action='Review the source operating decision before enabling collection.')


def health_issues(config,at):
    directory=Path(config['state_directory']);issues={}
    slot=slot_at(at);first=parse(config['first_run_at'])
    try:
        latest=read_json(directory/'runs'/slot.strftime('%Y%m%dT060000Z')/'run.json')
        if latest is not None and type(latest) is not dict:raise ValueError('invalid_run_receipt_shape')
    except (OSError,ValueError):
        latest=None
        issues['run:unreadable']=dict(severity='error',reason='expected_run_receipt_unreadable')
    grace=timedelta(seconds=execution_seconds(config)+RECOVERY_SECONDS+60)
    if slot>=first and at-slot<=grace and (not latest or latest.get('outcome') not in (*SUCCESSFUL_RUN_OUTCOMES,'partial_or_failed')):
        # Starting a new calendar day is not recovery from a missed/failed run.
        previous=read_json(directory/'health.json',{'active':{}})
        issues.update({k:v for k,v in previous['active'].items() if k in ('run:missing','run:failed')})
    if slot>=first and at-slot>grace:
        if latest is None:issues['run:missing']=dict(severity='error',reason='expected_daily_run_missing',scheduled_at=stamp(slot))
        elif latest['outcome']=='partial_or_failed' and not _published_partial_cycle(config,latest):
            issues['run:failed']=dict(severity='error',reason=latest['outcome'],run_id=latest['run_id'])
        elif latest['outcome'] not in (*SUCCESSFUL_RUN_OUTCOMES,'partial_or_failed'):
            issues['run:failed']=dict(severity='error',reason=latest['outcome'],run_id=latest['run_id'])
    if latest and latest.get('ended_at') and latest.get('outcome')=='partial_or_failed' and not _published_partial_cycle(config,latest):
        issues['run:failed']=dict(severity='error',reason='failed_publication_or_recovery',run_id=latest['run_id'])
    if latest and latest.get('ended_at') and latest.get('outcome') in SUCCESSFUL_RUN_OUTCOMES and not _valid_source_rows(latest):
        issues['run:failed']=dict(severity='error',reason='invalid_source_receipt',run_id=latest['run_id'])
    # The historical outbox intentionally never retries ambiguous delivery.
    # Keep that loss of notification visible in stored health state.
    prior=read_json(directory/'health.json',{'events':[]})
    if any(e.get('delivery') in ('attempted','failed_or_uncertain') for e in prior.get('events',[])):
        issues['delivery:uncertain']=dict(severity='error',reason='prior_operational_email_delivery_failed_or_uncertain',
            corrective_action='Inspect the retained delivery ledger and reconcile receipt before any manual resend.')
    missing=[]
    for provider in SOURCES:
        policy=POLICY[provider]
        enabled=source_settings(config)[provider]['enabled']
        try:
            source=read_json(directory/(provider+'-state.json'))
        except (OSError,ValueError):
            source=None
        if policy['readiness']=='blocked':
            issues[provider+':coverage']=dict(severity='warning',reason=policy['blocker'],
                corrective_action=policy['corrective_action'],readiness='blocked',enabled=False)
        elif not enabled:
            issues[provider+':coverage']=_disabled_coverage(config,provider,source)
        elif source is None:
            if at>=first+grace:
                issues[provider+':unverified']=dict(severity='error',reason='no_daily_qualifying_observation_record',
                    corrective_action='Check the expected run and source evidence; do not infer a complete empty inventory.')
            missing.append(provider)
        elif not source.get('qualifying_observation'):
            if policy['readiness']=='ready' and not source.get('cohorts'):missing.append(provider)
            if source.get('outcome') in ('blocked','disabled','not_started','cooldown'):
                issues[provider+':coverage']=dict(severity='warning',reason=source['outcome'],
                    corrective_action='Resolve the recorded eligibility or source blocker before claiming a check.',
                    readiness=policy['readiness'],enabled=enabled)
            else:
                attempt=_source_attempt(config,source)
                issues[provider+':collection']=dict(severity='error',reason=source.get('outcome','unknown'),
                    last_attempt_at=attempt.get('at'),http_status=attempt.get('status'),
                    corrective_action=('Resolve authorized access to the existing endpoint, then perform bounded validation.'
                        if attempt.get('status')==403 else
                        'Inspect the retained failed collection before a bounded authorized validation.'))
        if source and source.get('abnormal_count_drop'):
            issues[provider+':count_drop']=dict(severity='error',reason='observed_record_count_dropped',observed=source['observed'])
        for cohort in (source or {}).get('cohorts',[]):
            issue=_cohort_issue(cohort,at)
            if issue:issues[_cohort_key(provider,cohort.get('verified_at'))]=issue
    if missing:
        baseline=_baseline_cohorts(config,missing)
        if baseline is None:
            issues['inventory:unreadable']=dict(severity='error',reason='stored_verification_evidence_unreadable')
        else:
            for provider,cohorts in baseline.items():
                for cohort in cohorts:
                    issue=_cohort_issue(cohort,at)
                    if issue:issues[_cohort_key(provider,cohort.get('verified_at'))]=issue
    return issues


def _current_cycle(config,at):
    first=parse(config['first_run_at'])
    if at<first:return dict(state='scheduled',scheduled_at=stamp(first))
    slot=slot_at(at);path=Path(config['state_directory'])/'runs'/slot.strftime('%Y%m%dT060000Z')/'run.json'
    try:receipt=read_json(path)
    except (OSError,ValueError):receipt=None
    if receipt is not None and type(receipt) is not dict:receipt=None
    if receipt is None:
        grace=execution_seconds(config)+RECOVERY_SECONDS+60
        return dict(state='pending' if (at-slot).total_seconds()<=grace else 'missed',scheduled_at=stamp(slot))
    outcome=receipt.get('outcome');sources=receipt.get('sources') or {}
    if outcome in ('running','reserved'):
        return dict(state='running',scheduled_at=receipt.get('scheduled_at',stamp(slot)),
            run_id=receipt.get('run_id'),outcome=outcome,qualified_sources=[],failed_sources=[])
    if not _valid_source_rows(receipt):
        return dict(state='failed',scheduled_at=receipt.get('scheduled_at',stamp(slot)),
            run_id=receipt.get('run_id'),outcome=outcome,qualified_sources=[],failed_sources=[])
    qualified=sorted(source for source,row in sources.items() if row.get('qualifying_observation') is True)
    failed=sorted(source for source,row in sources.items() if (row.get('requests_used') or 0)>0
                  and row.get('qualifying_observation') is not True)
    if outcome in SUCCESSFUL_RUN_OUTCOMES:state='complete' if not failed else 'partial'
    elif _published_partial_cycle(config,receipt):state='partial'
    elif outcome in ('running','reserved'):state='running'
    else:state='failed'
    return dict(state=state,scheduled_at=receipt.get('scheduled_at',stamp(slot)),run_id=receipt.get('run_id'),
        outcome=outcome,qualified_sources=qualified,failed_sources=failed,
        requests_used=sum((row.get('requests_used') or 0) for row in sources.values()) if sources else None,
        new_opportunities=sum((row.get('new_canonical_opportunities') or 0) for row in sources.values() if row.get('qualifying_observation')),
        new_variants=sum((row.get('new') or 0) for row in sources.values() if row.get('qualifying_observation')),
        changed_variants=sum((row.get('changed') or 0) for row in sources.values() if row.get('qualifying_observation')),
        confirmed_closed=sum((row.get('confirmed_closed') or 0) for row in sources.values() if row.get('qualifying_observation')))


def _health_context(config,issues,at):
    directory=Path(config['state_directory'])
    qualified=[]
    for source in SOURCES:
        try:row=read_json(directory/(source+'-state.json'))
        except (OSError,ValueError):row=None
        if row and row.get('qualifying_observation') is True and source_settings(config)[source]['enabled']:
            qualified.append(source)
    affected=sorted({key.split(':',1)[0] for key in issues if key.split(':',1)[0] in SOURCES})
    return dict(checked_at=stamp(at),cycle=_current_cycle(config,at),qualified_sources=qualified,
        active_incidents=len(issues),affected_sources=affected,
        blocked_sources=sorted(source for source in SOURCES if POLICY[source]['readiness']=='blocked'),
        disabled_sources=sorted(source for source in SOURCES if not source_settings(config)[source]['enabled']
                                and POLICY[source]['readiness']=='ready'),
        expired_records=sum(value['records'] for key,value in issues.items()
                            if ':cohort_' in key and value.get('state')=='expired'),
        next_scheduled_execution=stamp(max(next_trigger(at),parse(config['first_run_at']))))


def _genuine_resolution(config,key,at,previous_check=None):
    provider,_,kind=key.partition(':')
    if provider in SOURCES:
        state=read_json(Path(config['state_directory'])/(provider+'-state.json')) or {}
        if kind in ('collection','coverage','count_drop') or kind.startswith('cohort_'):
            verified=state.get('ended_at')
            return bool(source_settings(config)[provider]['enabled'] and state.get('qualifying_observation') is True
                and verified and previous_check and parse(verified)>parse(previous_check))
        return False
    if key in ('run:missing','run:unreadable'):
        return _current_cycle(config,at)['state'] in ('complete','partial')
    if key=='run:failed':return _current_cycle(config,at)['state']=='complete'
    if key=='inventory:unreadable':return _baseline_cohorts(config,['mercor']) is not None
    return False


def health(config,at=None):
    """Durable deduplicated outbox. No employer or delivery calls here."""
    at=at or now();directory=Path(config['state_directory']);issues=health_issues(config,at)
    previous=read_json(directory/'health.json',{'active':{},'events':[]})
    old=previous.get('active',{});events=list(previous.get('events',[]));aligned={}
    transitions=[]
    def add(kind,key,*,issue=None,from_key=None,pending=True):
        item=dict(id=uuid.uuid4().hex,kind=kind,key=key,at=stamp(at),delivery='pending' if pending else 'not_applicable')
        if issue is not None:item['issue']=issue
        if from_key is not None:item['from_key']=from_key
        events.append(item)
    # Preserve sent event history. Align obsolete diagnostics in memory so a
    # changed key alone neither announces recovery nor creates a new incident.
    for key,value in old.items():
        provider,_,kind=key.partition(':')
        target=None
        if kind in ('age36','age48'):
            target=_cohort_key(provider,value.get('oldest_verification'))
            if target in issues:
                aligned[target]=issues[target]
                add('reclassified',target,from_key=key,pending=False)
                continue
        if kind=='collection' and provider+':coverage' in issues and key not in issues:
            target=provider+':coverage'
            if target not in old:
                transitions.append(target)
                add('status_changed',target,issue=issues[target],from_key=key)
            else:add('reclassified',target,from_key=key,pending=False)
            aligned[target]=issues[target]
            continue
        if kind=='unverified' and key not in issues:
            add('reclassified',provider+':coverage' if provider+':coverage' in issues else key,
                from_key=key,pending=False)
            continue
        if kind=='partial' and key not in issues:
            add('reclassified',key,from_key=key,pending=False)
            continue
        if key=='run:failed' and key not in issues and _current_cycle(config,at).get('state')=='partial':
            add('reclassified',key,from_key=key,pending=False)
            continue
        aligned[key]=value
    # A legacy age36 total could include a second cohort that only crossed
    # 36 hours after the last email. Account for its exact records once during
    # migration, without opening duplicate identities for the old total.
    for key,value in old.items():
        provider,_,kind=key.partition(':')
        if kind!='age36':continue
        cohorts={name:issue for name,issue in issues.items() if name.startswith(provider+':cohort_')}
        if cohorts and sum(issue['records'] for issue in cohorts.values())<=value.get('records',0):
            for name,issue in cohorts.items():aligned[name]=issue
    for key in sorted(set(issues)-set(aligned)-set(transitions)):
        add('opened',key,issue=issues[key])
    for key in sorted(set(aligned)&set(issues)):
        old_issue=aligned[key];new_issue=issues[key]
        if ':cohort_' in key and (old_issue.get('state')!=new_issue.get('state')
                or new_issue.get('records',0)>old_issue.get('records',0)):
            add('escalated',key,issue=new_issue)
    for key in sorted(set(aligned)-set(issues)):
        if _genuine_resolution(config,key,at,previous.get('checked_at')):add('recovered',key)
        else:add('reclassified',key,from_key=key,pending=False)
    context=_health_context(config,issues,at)
    before=previous.get('context',{}).get('qualified_sources')
    if before is not None:
        for source in sorted(set(context['qualified_sources'])-set(before)):
            if any(event['kind']=='recovered' and event['at']==stamp(at) and
                   event['key'] in (source+':collection',source+':coverage') for event in events):
                continue
            add('first_verified',source+':verification',issue=dict(severity='info',reason='first_daily_qualifying_observation'))
    result=dict(checked_at=stamp(at),next_scheduled_execution=stamp(max(next_trigger(at),parse(config['first_run_at']))),
        active=issues,events=events,context=context)
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
    if summary.get('qualifying_observation') is True:
        summary.pop('last_failed_collection_plan_id',None)
    elif summary.get('collection_plan_id') and summary.get('outcome') in (
            'collection_failed_or_interrupted','interrupted_or_failed','failed','partial_or_failed'):
        summary['last_failed_collection_plan_id']=summary['collection_plan_id']
    elif previous.get('last_failed_collection_plan_id') or (
            previous.get('collection_plan_id') and previous.get('outcome') in (
                'collection_failed_or_interrupted','interrupted_or_failed','failed','partial_or_failed')):
        summary['last_failed_collection_plan_id']=(previous.get('last_failed_collection_plan_id')
            or previous['collection_plan_id'])
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
        previous=_retain_old_failure(config,provider,read_json(directory/(provider+'-state.json'),{}))
        newer_state=previous.get('run_id','')>receipt['run_id']
        history={} if newer_state else previous
        try:
            row=read_json(target/(provider+'.json'))
            if row is None:
                plan=read_json(target/(provider+'-plan.json'))
                scaffold=plan or dict(plan_id=None,config=dict(providers=[provider],http_limit=source_settings(config)[provider]['http_max']),sources=[dict(jobs=[])])
                report=maintenance.report(config['journal'],plan['plan_id']) if plan else {}
                collection=read_json(target/(provider+'-collection.json'))
                retained=maintenance.report(config['journal'],collection['plan_id']) if collection else None
                if retained and report.get('events') and report['events'][-1]['event']=='finished':
                    from wahojobs.crawler.staged_observation import publication_report
                    report=publication_report(retained,report)
                row=summarize_source(scaffold,report,parse(receipt['started_at']),now())
                if not plan:
                    schedule=read_json(target/'coverage-plan.json',{})
                    policy=POLICY[provider];scheduled=schedule.get(provider,{})
                    row.update(outcome=scheduled.get('state','blocked' if policy['readiness']=='blocked' else 'not_started'),reason=scheduled.get('reason',policy['blocker']))
                    if row['outcome']=='due':row['outcome']='not_started'
                failure=read_json(target/(provider+'-failure.json'))
                if failure:
                    row['worker_error_type']=failure['error_type']
                    if not row['qualifying_observation']:row.update(outcome='interrupted_or_failed',error_type=failure['error_type'])
                elif report.get('status')=='interrupted':row.update(outcome='interrupted',qualifying_observation=False)
                if collection:
                    measured=summarize_source(scaffold,retained,parse(receipt['started_at']),now())
                    for field in ('requests_used','http_responses_received','pages_fetched','request_cap_reached','listing_envelope_shape'):
                        if field in measured:row[field]=measured[field]
                    row['collection_plan_id']=collection['plan_id']
                    row['publication_requests_used']=0
                    if not plan:
                        row.update(outcome='collected_unpublished' if retained['status']=='collected_unpublished' else 'collection_failed_or_interrupted',
                            qualifying_observation=False)
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
