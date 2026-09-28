"""Bounded daily inventory policy and stored-state health; dormant on import.

The product database and maintenance journal remain the evidence authority.
These small operator receipts never change candidate eligibility or user data.
"""
from collections import Counter
from contextlib import closing
from contextvars import ContextVar
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
# The complete 14-source September 28 rehearsal outgrew the former four-minute
# pool. Reserve six minutes within the unchanged overall execution ceiling;
# worker rollback, 60-second backup and 120-second recovery stay independent.
PUBLICATION_SECONDS=360
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


def repair_sources(receipt):
    sources=receipt.get('repair_sources')
    if sources is None:return None
    if (type(sources) is not list or not sources or any(type(s) is not str for s in sources)
            or len(sources)!=len(set(sources)) or not set(sources)<=set(SOURCES)
            or receipt.get('availability_sources') is not None):
        raise ValueError('repair_source_scope_invalid')
    return sources


def selected_sources(receipt):
    from wahojobs.availability_recovery import selected
    return repair_sources(receipt) or selected(receipt)


def repair_policy_binding(config):
    return maintenance.digest(dict(code_commit=config['code_commit'],database=config['database'],
        journal=config.get('journal'),sources=source_settings(config)))


def validate_repair_binding(config,receipt):
    sources=repair_sources(receipt)
    if sources is not None and (receipt.get('trigger')!='operator_repair'
            or receipt.get('code_commit')!=config['code_commit']
            or receipt.get('repair_policy_sha256')!=repair_policy_binding(config)
            or any(not source_settings(config)[source]['enabled'] for source in sources)):
        raise ValueError('repair_release_or_policy_changed')
    return sources


def reserve_repair_run(config,at,request_id,sources):
    """Consume one explicit operator request, independent of the daily slot.

    The request directory is a global durable claim. Even interruption before a
    run receipt is written consumes it; retrying never issues employer requests.
    """
    import re
    if (not isinstance(request_id,str) or not re.fullmatch('[a-z][a-z0-9-]{2,63}',request_id)
            or not re.fullmatch('[a-f0-9]{40}',config.get('code_commit',''))
            or at.tzinfo is None or config.get('enabled') is False):
        raise ValueError('explicit_repair_request_required')
    sources=repair_sources({'repair_sources':sources})
    if sources is None:raise ValueError('repair_source_scope_invalid')
    sources=[source for source in SOURCES if source in sources]
    if any(not source_settings(config)[source]['enabled'] or POLICY[source]['readiness']!='ready' for source in sources):
        raise ValueError('repair_source_disabled_or_blocked')
    identity=sha256(request_id.encode('ascii')).hexdigest()
    binding=dict(request_id=request_id,code_commit=config['code_commit'],sources=sources,
        policy_sha256=repair_policy_binding(config))
    root=Path(config['state_directory']);claim=root/'repair-requests'/identity
    claim.parent.mkdir(mode=0o750,parents=True,exist_ok=True)
    native_root=os.name=='posix' and os.geteuid()==0
    if native_root:
        # Root owns the once-only claims; the existing beta group can read
        # their immutable bindings for stored-state health reporting.
        owner=root.stat();os.chown(claim.parent,0,owner.st_gid);os.chmod(claim.parent,0o750)
    try:claim.mkdir(mode=0o700,parents=True,exist_ok=False)
    except FileExistsError:
        original=read_json(claim/'request.json')
        if original is not None and original.get('binding')!=binding:
            raise ValueError('repair_request_already_bound')
        return None
    if native_root:os.chown(claim,0,owner.st_gid);os.chmod(claim,0o750)
    run_id=at.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-repair-'+identity[:16]
    write_json(claim/'request.json',dict(binding=binding,run_id=run_id,reserved_at=stamp(at)))
    if native_root:os.chmod(claim/'request.json',0o640)
    target=root/'runs'/run_id;target.mkdir(mode=0o700,parents=True,exist_ok=False)
    if os.name=='posix' and os.geteuid()==0:
        parent=target.parent.stat();os.chown(target,parent.st_uid,parent.st_gid)
    receipt=dict(version=VERSION,run_id=run_id,trigger='operator_repair',
        code_commit=config['code_commit'],repair_request_id=request_id,
        repair_policy_sha256=binding['policy_sha256'],repair_sources=sources,
        scheduled_at=stamp(at),started_at=stamp(at),ended_at=None,outcome='reserved',sources={},
        maintenance_seconds=0,next_scheduled_execution=stamp(next_trigger(at)))
    write_json(target/'run.json',receipt)
    return receipt


_cooldown_checks=ContextVar('daily_cooldown_checks',default=None)


def verified_cooldown(config,source,at):
    """Classify a retained skip; never renew its evidence or authorize collection.

    Cache only during one health observation. Original publication proof is read
    with the shared transaction reader, so a release change cannot erase a real
    earlier verification, and a copied success timestamp cannot create one.
    """
    if not isinstance(source,dict) or source.get('outcome')!='cooldown':return None
    cache=_cooldown_checks.get()
    key=(str(config['state_directory']),maintenance.digest(source),stamp(at))
    if cache is not None and key in cache:return cache[key]
    result=None
    try:result=_verified_cooldown(config,source,at)
    except (OSError,ValueError,KeyError,TypeError,AttributeError,sqlite3.Error):pass
    if cache is not None:cache[key]=result
    return result


def _verified_cooldown(config,source,at):
    provider=source['provider'];policy=POLICY[provider]
    if (provider not in SOURCES or not source_settings(config)[provider]['enabled']
            or not policy.get('cooldown_hours') or source.get('reason')!='source_success_cooldown'
            or source.get('qualifying_observation') is not False or source.get('requests_used')!=0
            or source.get('plan_id') or source.get('collection_plan_id')
            or source.get('pending_qualification_count') or source.get('missing')
            or source.get('abnormal_count_drop')):return None
    root=Path(config['state_directory']);run_id=source['run_id']
    if Path(run_id).name!=run_id or run_id in ('.','..'):return None
    schedule=read_json(root/'runs'/run_id/'coverage-plan.json',{}).get(provider,{})
    if (schedule.get('state')!='cooldown' or schedule.get('reason')!=source.get('reason')
            or source.get('next_eligible_at') not in (None,schedule.get('next_eligible_at'))):return None
    skipped=parse(source['started_at']);due=parse(schedule['next_eligible_at'])
    verified=parse(source['last_qualifying_verification'])
    if not verified<=skipped<due or at<skipped:return None
    cohorts=source.get('cohorts')
    if not cohorts or any(not c.get('verified_at') or type(c.get('records')) is not int
            or c['records']<=0 or not timedelta(0)<=at-parse(c['verified_at'])<timedelta(hours=72)
            for c in cohorts):return None
    with maintenance.read_connection(config['database']) as db:
        latest=db.execute("""SELECT r.* FROM crawl_runs r JOIN companies c ON c.id=r.company_id
            WHERE c.slug=? ORDER BY r.id DESC LIMIT 1""",(provider,)).fetchone()
    if latest is None:return None
    terminal=dict(latest)
    if (terminal['status']!='success' or terminal['used_sample_data'] or terminal['error_message']
            or parse(terminal['started_at'])+timedelta(hours=policy['cooldown_hours'])!=due):return None
    # Source receipts are small; only a matching retained success opens journals.
    for path in sorted((root/'runs').glob('*/'+provider+'.json'),reverse=True):
        prior=read_json(path,{})
        if (prior.get('qualifying_observation') is not True or prior.get('last_qualifying_verification')!=source['last_qualifying_verification']
                or prior.get('cohorts')!=cohorts or not prior.get('ended_at')
                or not verified<=parse(prior['ended_at'])<=skipped):continue
        original=dict(run_id=path.parent.name,trigger=prior.get('trigger','timer'),
            started_at=prior['started_at'],ended_at=prior['ended_at'])
        proven=reconstruct_source_receipt(config,original,provider)
        proof=proven.get('reconciliation_proof') or {}
        if (proven.get('qualifying_observation') is not True
                or proof.get('crawl_run_id')!=terminal['id']
                or proof.get('terminal_crawl_row_sha256')!=maintenance.digest(terminal)
                or not proof.get('prepared_event_hash')
                or proven.get('pending_qualification_count') or proven.get('missing')
                or proven.get('cohorts')!=cohorts
                or proven.get('last_qualifying_verification')!=source['last_qualifying_verification']):continue
        return dict(source=provider,verified_at=stamp(verified),published_at=prior['ended_at'],
            original_run_id=path.parent.name,skipped_run_id=run_id,skipped_at=stamp(skipped),
            next_eligible_at=stamp(due),reason='retained_success_cooldown',
            crawl_run_id=terminal['id'])
    return None


def latest_operator_repair(config,at):
    """Show a separate repair outcome; never rewrite the scheduled run."""
    root=Path(config['state_directory']);candidates=[]
    for path in (root/'runs').glob('*-repair-*/run.json'):
        try:
            receipt=read_json(path);sources=repair_sources(receipt)
            if not sources or receipt.get('trigger')!='operator_repair':continue
            started=parse(receipt['started_at'])
            if not slot_at(at)<=started<=at:continue
            request_id=receipt['repair_request_id']
            identity=sha256(request_id.encode('ascii')).hexdigest()
            claim=read_json(root/'repair-requests'/identity/'request.json',{})
            expected=dict(request_id=request_id,code_commit=receipt['code_commit'],sources=sources,
                policy_sha256=receipt['repair_policy_sha256'])
            if (claim.get('binding')!=expected or claim.get('run_id')!=receipt['run_id']
                    or path.parent.name!=receipt['run_id']):continue
            rows=receipt.get('sources') or {}
            qualified=sorted(s for s in sources if isinstance(rows.get(s),dict)
                and rows[s].get('qualifying_observation') is True)
            recent={s:proof for s in sources if (proof:=verified_cooldown(config,rows.get(s),at))}
            covered=set(qualified)|set(recent)
            ended=parse(receipt['ended_at']) if receipt.get('ended_at') else None
            if ended and not started<=ended<=at:continue
            worker=read_json(path.parent/'worker.json',{})
            complete=(receipt.get('outcome') in (*SUCCESSFUL_RUN_OUTCOMES,'partial_or_failed')
                and set(rows)==set(sources) and covered==set(sources) and ended is not None
                and all(not rows[s].get('pending_qualification_count') and not rows[s].get('missing') for s in sources)
                and receipt.get('normal_service_resumed') is True
                and worker.get('protected_domains_unchanged') is True
                and (receipt.get('outcome') in SUCCESSFUL_RUN_OUTCOMES or
                    bool(recent) and worker.get('completed') is True and not cycle_failure(receipt)))
            state=('complete' if complete else 'running' if receipt.get('outcome') in ('running','reserved') else
                'partial' if receipt.get('outcome') in (*SUCCESSFUL_RUN_OUTCOMES,'partial_or_failed') else 'failed')
            enabled={s for s,row in source_settings(config).items() if row['enabled']}
            current={s:read_source_state(config,s) or {} for s in enabled} if complete else {}
            still_verified=complete and all(
                (row.get('qualifying_observation') is True and not row.get('pending_qualification_count')
                    and not row.get('missing') and row.get('ended_at')
                    and (parse(row['ended_at'])>=ended or row.get('run_id')==receipt['run_id']
                        and started<=parse(row['ended_at'])<=ended))
                or (s in recent and row==rows[s] and parse(recent[s]['verified_at'])>=slot_at(at))
                for s,row in current.items())
            candidates.append(dict(run_id=receipt['run_id'],started_at=stamp(started),ended_at=stamp(ended) if ended else None,
                state=state,selected_sources=sources,qualified_sources=qualified,
                recently_verified_sources=sorted(recent),recent_verifications=recent,
                remaining_sources=sorted(set(sources)-covered),
                incomplete_sources=sorted(s for s in sources if rows.get(s,{}).get('pending_qualification_count')
                    or rows.get(s,{}).get('missing')),
                resolves_daily_failure=still_verified and enabled<=covered))
        except (OSError,ValueError,KeyError,TypeError,AttributeError):continue
    return max(candidates,key=lambda r:(r['started_at'],r['run_id']),default=None)


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
    if provider in ('outlier','dataforce','surge','mercor'):
        row['pending_qualification_ids']=pending[-1]['identities'] if pending else []
        row['pending_qualification_count']=len(row['pending_qualification_ids'])
        row['pending_qualification_index_sha256']=pending[-1]['index_sha256'] if pending else None
        row['pending_qualification_scope']='identities without a qualifying detail in this run; prior qualification is not inferred'
        row['discovery_scope']=('observed public board; only versioned, individually attested IDs can publish'
            if provider=='outlier' else
            'exact public pages of known IDs missing from the explorer; the general catalog remains partial'
            if provider=='mercor' else
            'observed public workforce index; exact workforce records with attested details can publish'
            if provider=='surge' else
            'observed public project index; exact supported paid remote contributor families require individual index/detail/application evidence; no absence closure')
    before={j['job_id']:j for j in plan['sources'][0]['jobs']}
    if results and results[-1].get('failure_diagnostic'):
        row['failure_diagnostic']=results[-1]['failure_diagnostic']
    if not results or 'summary' not in results[-1].get('result',{}):return row
    result=results[-1]['result'];summary=result['summary'];state=result['after'];run=state.get('latest_run') or {}
    run_id=run.get('id')
    good=[j for j in state['jobs'] if j['verification'].get('source_run_id')==run_id
          and j['verification'].get('latest_successful_source_run_at')]
    valid=(row['requests_used']>0 and not summary['used_sample_data'] and run.get('status') in ('success','partial')
        and summary['normalized_record_count']==summary['jobs_found']
        and summary['raw_record_count']==summary['normalized_record_count']+summary['rejected_record_count']+summary.get('filtered_record_count',0))
    from wahojobs.mindrift_observation import COUNT_DROP_WARNING
    individual=(provider in ('mercor','dataannotation','dataforce','handshake','surge','outlier')
        or provider=='mindrift' and run.get('status')=='partial'
            and COUNT_DROP_WARNING in summary.get('warnings',[]))
    exact_closed=[]
    if provider=='mercor' and summary['jobs_removed']>0:
        from wahojobs.mercor_availability import CLOSED_CONTRACTS
        collected=[event['data']['result'] for event in events if event['event']=='collected_result']
        records=collected[0].get('source_records',[]) if len(collected)==1 else []
        closed=[record for record in records if record.get('contract_id') in CLOSED_CONTRACTS
            and record.get('state')=='closed' and type(record.get('known_job_id')) is int]
        changed={job['job_id'] for job in state['jobs'] if job['job_id'] in before
            and before[job['job_id']]['verification']['status']!='inactive'
            and job['verification']['status']=='inactive'}
        if (len(closed)==summary['jobs_removed']==len(changed)
                and {record['known_job_id'] for record in closed}==changed):exact_closed=closed
    qualifies=valid and (bool(good or exact_closed) if individual else
        summary['snapshot_complete'] and summary['pagination_complete'] and run.get('status')=='success')
    row.update(qualifying_observation=bool(qualifies),outcome=('partial_individual' if individual else 'complete') if qualifies else 'partial_or_failed',
        observed=summary['jobs_found'],new=summary['jobs_new'],confirmed_closed=summary['jobs_removed'])
    if provider=='mindrift' and COUNT_DROP_WARNING in summary.get('warnings',[]):
        row['coverage_warnings']=[COUNT_DROP_WARNING]
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
    row['uncertain']=max(0,summary['rejected_record_count']-len(exact_closed))+max(0,summary['jobs_found']-len(good))
    dates=Counter(j['verification'].get('latest_successful_source_run_at') for j in active)
    row['cohorts']=[dict(verified_at=date,records=count,expires_at=stamp(parse(date)+timedelta(hours=72)) if date else None)
        for date,count in sorted(dates.items(),key=lambda p:p[0] or '')]
    row['stale']=sum(c['records'] for c in row['cohorts'] if not c['verified_at'] or ended-parse(c['verified_at'])>timedelta(hours=72))
    qualifying_dates=[j['verification']['latest_successful_source_run_at'] for j in good]
    qualifying_dates.extend(record['observed_at'] for record in exact_closed)
    row['last_qualifying_verification']=max(qualifying_dates,default=None) if qualifies else None
    deadlines=[c['expires_at'] for c in row['cohorts'] if c['expires_at']]
    row['next_verification_deadline']=min(deadlines,default=None)
    return row


def publication_fields(summary, publication):
    """Classify retained outcomes without granting authority to diagnostics."""
    failure=summary.get('failure_diagnostic') or {}
    rejected=(summary['observed'] is not None or
        failure.get('phase')=='qualification' and failure.get('reason')=='mindrift_count_drop'
        and failure.get('error_type')=='MindriftCountDropRejected')
    fields=dict(publication_outcome=publication['status'],publication_requests_used=0,
        qualification_outcome='accepted' if summary['qualifying_observation'] else
            'rejected' if rejected else 'not_established')
    if not summary['qualifying_observation'] and publication['status']=='failed':
        fields['outcome']='qualification_failed' if rejected else 'publication_failed'
    if publication.get('reconciliation_proof'):fields['reconciliation_proof']=publication['reconciliation_proof']
    return fields


def retained_worker_diagnostic(target, run_id, provider):
    return retained_phase_diagnostic(target,run_id,('publish-'+provider,'collect-'+provider))


def retained_phase_diagnostic(target, run_id, phases):
    """Use only the exact current run/phase; retain no arbitrary failure values."""
    import re
    for phase in phases:
        try:value=read_json(target/(phase+'-failure.json'),{})
        except (OSError,ValueError):continue
        if (type(value) is not dict or value.get('phase')!=phase or value.get('run_id')!=run_id
                or not isinstance(value.get('error_type'),str)
                or not re.fullmatch('[A-Za-z_][A-Za-z_0-9]{0,89}',value['error_type'])):continue
        frames=value.get('frames',[])
        if type(frames) is not list:continue
        kept=[]
        for frame in frames[-8:]:
            if (type(frame) is dict and type(frame.get('line')) is int and frame['line']>0
                    and all(isinstance(frame.get(k),str) and len(frame[k])<=120 for k in ('file','function'))
                    and Path(frame['file']).name==frame['file']):
                kept.append({k:frame[k] for k in ('file','function','line')})
        return dict(phase=phase,error_type=value['error_type'],frames=kept,
            reason=value.get('reason') if value.get('reason') in
                maintenance.FAILURE_REASONS else None)
    return None


def cycle_failure(receipt):
    """A bounded explanation only; this never establishes source qualification."""
    failure=(receipt.get('recovery_failure_diagnostic') if receipt.get('outcome')=='recovery_failed'
        else receipt.get('failure_diagnostic')) or {}
    phase=failure.get('phase')
    phases={'collection','publication','prepare','prepare-backup','backup','finish','stop','restore',
        *('collect-'+source for source in SOURCES),*('publish-'+source for source in SOURCES)}
    if phase not in phases:return None
    return dict(phase=phase,reason=failure.get('reason') if failure.get('reason') in maintenance.FAILURE_REASONS else None,
        timed_out=failure.get('error_type') in ('TimeoutError','TimeoutExpired') or failure.get('reason') in
            ('worker_execution_deadline_expired','execution_deadline_expired','publication_deadline_expired'))


def source_settings(config):
    return config.get('sources', default_sources())


def execution_seconds(config):
    return aggregate(source_settings(config))['execution_seconds']


def publication_seconds(config):
    # A small enabled-source configuration must still have time to collect its
    # largest source. The six-minute full-inventory maximum does not increase
    # the configured execution ceiling or starve one-source maintenance runs.
    largest=max((row['seconds_max'] for row in source_settings(config).values()
                 if row['enabled']),default=0)
    return min(PUBLICATION_SECONDS,execution_seconds(config)-largest)


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
    previous=_retain_old_failure(config,source,read_source_state(config,source) or {})
    summary=merge_source_history(summary,previous)
    summary.update(run_id=run_id,trigger=read_json(target/'run.json',{}).get('trigger','isolated_worker'))
    write_json(target/(source+'.json'),summary)
    write_json(directory/(source+'-state.json'),summary)


def read_source_state(config,source):
    from wahojobs.daily_receipt_reconciliation import source_state
    return source_state(config,source)


def collect_phase(config, run_id, phase, *, expected_preparation_sha256=None):
    """One backup, independently bounded source processes, one integrity finish.

    Native parent holds the common operation gate throughout. Publication obtains
    the existing lifetime lease; online collection changes no product records.
    No provider can spend a sibling's request or time allowance.
    """
    from wahojobs.database_lifetime_ownership import acquire_database_lifetime_ownership,release_database_lifetime_ownership,ROLE_OFFLINE_OPERATOR
    from wahojobs.beta_recovery import create_snapshot,verify_snapshot
    database=Path(config['database']);directory=Path(config['state_directory']);target=directory/'runs'/run_id
    from wahojobs.crawler import staged_observation as staged
    from wahojobs import availability_recovery as targeted
    run_receipt=read_json(target/'run.json',{})
    validate_repair_binding(config,run_receipt)
    availability=targeted.selected(run_receipt)
    selected=selected_sources(run_receipt)
    if phase=='prepare-backup':
        # The native parent retains the operation gate while the application
        # remains online. Evidence preparation takes no SQLite lifetime lease.
        from wahojobs.beta_recovery import _prepare_snapshot_journal, _json
        from wahojobs.recovery_archive import VERSION as snapshot_version
        prepared=directory/'backups'/(run_id+'.prepared')
        prepared.parent.mkdir(parents=True,exist_ok=True)
        result=_prepare_snapshot_journal(database,prepared,run_id=run_id,
            code_commit=config['code_commit'],configuration_revision='config-002',snapshot_version=snapshot_version)
        if result.get('snapshot_version')!=snapshot_version:
            raise ValueError('trusted_journal_preparation_required')
        write_json(target/'journal-preparation.json',dict(path=str(prepared),run_id=run_id,
            code_commit=config['code_commit'],prepared_at=stamp(now()),files=len(result['files']),
            snapshot_version=snapshot_version))
        return dict(prepared_sha256=sha256(_json(result)).hexdigest(),snapshot_version=snapshot_version)
    if phase=='prepare':
        # Read-only source/configuration inspection is safe while beta owns the
        # database. No database copy or offline lifetime lease is taken here.
        plan=coverage_plan(config,database,now())
        if selected is not None:
            for source,row in plan.items():
                if source not in selected:row.update(state='not_targeted',reason='outside_authorized_attempt')
        write_json(target/'coverage-plan.json',plan)
        for source,row in plan.items():
            if selected is not None and source not in selected:continue
            if row['state']!='due':
                summary=empty_source(source,now(),outcome=row['state'],reason=row['reason'])
                summary['next_eligible_at']=row['next_eligible_at'];save_source(config,run_id,summary)
        return
    if phase.startswith('collect-'):
        source=phase.removeprefix('collect-')
        if selected is not None and source not in selected:raise ValueError('source_outside_targeted_attempt')
        schedule=read_json(target/'coverage-plan.json')
        if source not in SOURCES or not schedule or schedule[source]['state']!='due' or not source_settings(config)[source]['enabled']:
            raise ValueError('source_not_due_in_reserved_plan')
        with maintenance.read_connection(database) as connection:
            company=connection.execute('SELECT id,careers_url FROM companies WHERE slug=?',(source,)).fetchone()
            known_jobs=None
            if company and source=='mercor':
                from wahojobs.mercor_availability import load_known_jobs
                known_jobs=load_known_jobs(connection,company['id'])
        if not company:raise ValueError('configured_source_required')
        binding=maintenance.journal_binding(database)
        if not binding or Path(binding['journal_root'])!=Path(config['journal']):raise ValueError('authoritative_journal_mismatch')
        staged.collect(source,company['careers_url'],target,run_id=run_id,code_commit=config['code_commit'],
            http_max=min(source_settings(config)[source]['http_max'],targeted.CAPS[source]) if availability else source_settings(config)[source]['http_max'],
            journal_root=config['journal'],**({'known_jobs':known_jobs} if known_jobs is not None else {}),
            **({'audit_sink':lambda event:targeted.audit(target,source,event)} if availability else {}))
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
            prepared=directory/'backups'/(run_id+'.prepared')
            created_manifest=create_snapshot(database,snapshot,code_commit=config['code_commit'],configuration_revision='config-002',ownership=lease,
                **({'prepared_journal':prepared,'run_id':run_id,
                    'expected_preparation_sha256':expected_preparation_sha256}
                    if run_receipt.get('prepared_backup_required') is True or prepared.exists() else {}))
            # The returned object is the proof from this creation call. A
            # manifest reread from storage must never grant this authority.
            manifest=verify_snapshot(snapshot,trusted_manifest=created_manifest)
            write_json(target/'backup.json',dict(verified=True,files=len(manifest['files']),protected_domains=before,
                snapshot_version=manifest['version']))
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
            if selected is not None and source not in selected:raise ValueError('source_outside_targeted_attempt')
            schedule=read_json(target/'coverage-plan.json')
            if not schedule or schedule[source]['state']!='due' or not source_settings(config)[source]['enabled']:
                raise ValueError('source_not_due_in_reserved_plan')
            cap=source_settings(config)[source]['http_max'];started=now()
            observation=None;collection_report=None
            if publishing:
                observation,collection_report=staged.load(target,source,run_id=run_id,code_commit=config['code_commit'],journal_root=config['journal'],consume=True)
                started=parse(observation.started_at)
            plan=maintenance.build_plan(database,[source],http_limit=cap,detail_limit=0,
                details=None,phase='source',daily_discovery=True,staged_baseline=publishing)
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
                publication=maintenance.committed_publication_report(result,database)
                result=staged.publication_report(collection_report,publication)
            summary=summarize_source(plan,result,started,now())
            if observation is not None:
                summary.update(collection_plan_id=observation.collection_plan_id,collection_completed_at=observation.completed_at,
                    publication_completed_at=stamp(now()),publication_requests_used=0,
                    attempt_started='yes',attempt_started_at=observation.started_at,
                    capture_outcome=collection_report['status'],accounting_status='complete')
                summary.update(publication_fields(summary,publication))
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


def _source_attempt(config,source,*,historical=False):
    # An older failure is useful for an explicitly disabled source, but cannot
    # establish that the current scheduled run dispatched a request.
    plan_id=(source.get('last_failed_collection_plan_id') if historical else None) or source.get('collection_plan_id')
    if not plan_id:return {}
    try:
        report=maintenance.report(config['journal'],plan_id)
        if report['plan'].get('source')!=source['provider']:return {}
        if not historical and source.get('run_id') and report['plan'].get('run_id')!=source['run_id']:return {}
        requests=[e['data'] for e in report['events'] if e['event']=='source_transport'
                  and e['data'].get('event')=='request']
        errors=[e['data'] for e in report['events'] if e['event']=='source_transport'
                and e['data'].get('event')=='transport_error']
        return dict(at=requests[0].get('observed_at') if requests else None,
                    status=errors[-1].get('status') if errors and report['status']=='collection_failed' else None)
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
    attempt=_source_attempt(config,source,historical=True) if failed else {}
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


def source_condition(source):
    """One reporting boundary for receipt states, never an evidence upgrade."""
    if source.get('qualifying_observation') is True:
        pending=source.get('pending_qualification_count')
        return 'undercoverage' if type(pending) is int and pending>0 else None
    outcome=source.get('outcome')
    if outcome in ('blocked','disabled','not_started','cooldown','not_targeted'):return 'coverage'
    if outcome in ('accounting_unavailable','receipt_finalization_failed'):return 'accounting'
    if source.get('qualification_outcome')=='rejected':return 'qualification'
    if source.get('publication_outcome') in ('failed','interrupted','blocked') or outcome in ('collected_unpublished','publication_failed'):
        return 'publication'
    if outcome=='partial_or_failed' and source.get('observed') is not None:return 'qualification'
    used=source.get('requests_used')
    if type(used) is int and used>0:return 'collection'
    if type(used) is int and used==0 and outcome in ('failed','interrupted_or_failed','collection_failed_or_interrupted'):
        return 'coverage'
    return 'accounting'


def health_issues(config,at,*,application_ready=None):
    directory=Path(config['state_directory']);issues={}
    previous=read_json(directory/'health.json',{'active':{}})
    if application_ready is False:
        issues['application:unavailable']=dict(severity='critical',reason='application_readiness_failed',
            corrective_action='Restore validated storage and application readiness; keep publication paused.')
    elif application_ready is None and 'application:unavailable' in previous.get('active',{}):
        issues['application:unavailable']=previous['active']['application:unavailable']
    slot=slot_at(at);first=parse(config['first_run_at'])
    try:
        from wahojobs.daily_receipt_reconciliation import read_current
        latest=read_current(directory/'runs'/slot.strftime('%Y%m%dT060000Z')/'run.json')
        if latest is not None and type(latest) is not dict:raise ValueError('invalid_run_receipt_shape')
    except (OSError,ValueError):
        latest=None
        issues['run:unreadable']=dict(severity='error',reason='expected_run_receipt_unreadable')
    grace=timedelta(seconds=execution_seconds(config)+RECOVERY_SECONDS+60)
    if latest and latest.get('outcome')=='recovery_failed':
        issues['run:failed']=dict(severity='error',reason='recovery_failed',run_id=latest['run_id'])
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
    repair=latest_operator_repair(config,at)
    if repair and repair['resolves_daily_failure'] and latest is not None:
        issues.pop('run:failed',None)
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
            source=read_source_state(config,provider)
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
        elif source_condition(source) is not None and not verified_cooldown(config,source,at):
            if not source.get('qualifying_observation') and policy['readiness']=='ready' and not source.get('cohorts'):missing.append(provider)
            kind=source_condition(source)
            if kind=='coverage':
                issues[provider+':coverage']=dict(severity='warning',reason=source['outcome'],
                    corrective_action='Resolve the recorded eligibility or source blocker before claiming a check.',
                    readiness=policy['readiness'],enabled=enabled)
            else:
                attempt=_source_attempt(config,source)
                actions={
                    'accounting':'Inspect retained run and source receipts; attempt status must not be inferred from missing accounting.',
                    'qualification':'Inspect retained observation and qualification decisions; no freshness or closure may be inferred from a rejected observation.',
                    'publication':'Inspect retained publication evidence and the original capture; do not replay it to renew verification.',
                    'undercoverage':'Inspect the retained pending IDs and detail outcomes; only qualifying exact records were refreshed.',
                    'collection':('Resolve authorized access to the existing endpoint, then perform bounded validation.'
                        if attempt.get('status')==403 else
                        'Inspect the retained failed collection before a bounded authorized validation.')}
                issues[provider+':'+kind]=dict(severity='error',reason=source.get('outcome','unknown'),
                    last_attempt_at=attempt.get('at'),http_status=attempt.get('status'),
                    attempt_started=source.get('attempt_started','yes' if attempt.get('at') else 'unknown'),
                    corrective_action=actions[kind])
                if kind=='undercoverage':issues[provider+':'+kind]['pending_records']=source['pending_qualification_count']
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


def _current_cycle(config,at,*,verify_cooldowns=True):
    first=parse(config['first_run_at'])
    if at<first:return dict(state='scheduled',scheduled_at=stamp(first))
    slot=slot_at(at);path=Path(config['state_directory'])/'runs'/slot.strftime('%Y%m%dT060000Z')/'run.json'
    from wahojobs.daily_receipt_reconciliation import read_current
    try:receipt=read_current(path)
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
            run_id=receipt.get('run_id'),outcome=outcome,qualified_sources=[],failed_sources=[],failure=cycle_failure(receipt))
    qualified=sorted(source for source,row in sources.items() if row.get('qualifying_observation') is True)
    recent=({s:proof for s,row in sources.items() if (proof:=verified_cooldown(config,row,at))}
        if verify_cooldowns else {})
    groups={kind:sorted(source for source,row in sources.items() if source_condition(row)==kind and source not in recent)
            for kind in ('collection','accounting','qualification','publication','coverage','undercoverage')}
    failed=groups['collection']
    if outcome in SUCCESSFUL_RUN_OUTCOMES:
        state='partial' if any(groups[k] for k in ('collection','accounting','qualification','publication','undercoverage')) else 'complete'
    elif _published_partial_cycle(config,receipt):state='partial'
    elif outcome in ('running','reserved'):state='running'
    else:state='failed'
    return dict(state=state,scheduled_at=receipt.get('scheduled_at',stamp(slot)),run_id=receipt.get('run_id'),
        outcome=outcome,qualified_sources=qualified,recently_verified_sources=sorted(recent),
        recent_verifications=recent,failed_sources=failed,failure=cycle_failure(receipt),
        reporting_reconciliation=receipt.get('reporting_reconciliation'),
        accounting_unavailable_sources=groups['accounting'],qualification_failed_sources=groups['qualification'],
        publication_failed_sources=groups['publication'],not_attempted_sources=groups['coverage'],
        undercovered_sources=groups['undercoverage'],
        requests_used=(sum(row['requests_used'] for row in sources.values())
            if sources and all(type(row.get('requests_used')) is int for row in sources.values()) else None),
        known_requests_used=sum((row.get('requests_used') or 0) for row in sources.values()),
        new_opportunities=sum((row.get('new_canonical_opportunities') or 0) for row in sources.values() if row.get('qualifying_observation')),
        new_variants=sum((row.get('new') or 0) for row in sources.values() if row.get('qualifying_observation')),
        changed_variants=sum((row.get('changed') or 0) for row in sources.values() if row.get('qualifying_observation')),
        confirmed_closed=sum((row.get('confirmed_closed') or 0) for row in sources.values() if row.get('qualifying_observation')))


def _health_context(config,issues,at):
    directory=Path(config['state_directory'])
    qualified=[];recent={}
    for source in SOURCES:
        try:row=read_source_state(config,source)
        except (OSError,ValueError):row=None
        if row and row.get('qualifying_observation') is True and source_settings(config)[source]['enabled']:
            qualified.append(source)
        elif (proof:=verified_cooldown(config,row,at)):recent[source]=proof
    affected=sorted({key.split(':',1)[0] for key in issues if key.split(':',1)[0] in SOURCES})
    return dict(checked_at=stamp(at),cycle=_current_cycle(config,at),operator_repair=latest_operator_repair(config,at),qualified_sources=qualified,
        recently_verified_sources=sorted(recent),recent_verifications=recent,
        active_incidents=len(issues),affected_sources=affected,
        blocked_sources=sorted(source for source in SOURCES if POLICY[source]['readiness']=='blocked'),
        disabled_sources=sorted(source for source in SOURCES if not source_settings(config)[source]['enabled']
                                and POLICY[source]['readiness']=='ready'),
        expired_records=sum(value['records'] for key,value in issues.items()
                            if ':cohort_' in key and value.get('state')=='expired'),
        next_scheduled_execution=stamp(max(next_trigger(at),parse(config['first_run_at']))))


def _genuine_resolution(config,key,at,previous_check=None,application_ready=None):
    if key=='application:unavailable':return application_ready is True
    provider,_,kind=key.partition(':')
    if provider in SOURCES:
        state=read_source_state(config,provider) or {}
        if kind in ('collection','coverage','accounting','qualification','publication','count_drop','undercoverage') or kind.startswith('cohort_'):
            verified=state.get('ended_at')
            return bool(source_settings(config)[provider]['enabled'] and state.get('qualifying_observation') is True
                and (kind!='undercoverage' or not state.get('pending_qualification_count'))
                and verified and previous_check and parse(verified)>parse(previous_check))
        return False
    if key in ('run:missing','run:unreadable'):
        return _current_cycle(config,at)['state'] in ('complete','partial')
    if key=='run:failed':
        repair=latest_operator_repair(config,at)
        return (_current_cycle(config,at)['state']=='complete' or bool(repair and repair['resolves_daily_failure']
            and (not previous_check or parse(repair['ended_at'])>parse(previous_check))))
    if key=='inventory:unreadable':return _baseline_cohorts(config,['mercor']) is not None
    return False


def record_availability_failure(config,*,reason,unit,occurred_at=None):
    """Persist before deferred observation/delivery; this is not a network call."""
    directory=Path(config['state_directory']);signals=directory/'availability-signals'
    signals.mkdir(mode=0o700,parents=True,exist_ok=True)
    if os.name=='posix' and os.geteuid()==0:
        owner=directory.stat();os.chown(signals,owner.st_uid,owner.st_gid)
    identifier=uuid.uuid4().hex;observed=stamp(now())
    signal=dict(id=identifier,reason=reason,unit=unit,occurred_at=occurred_at,
        observed_at=observed,recorded_at=stamp(now()))
    write_json(signals/(identifier+'.json'),signal)
    write_json(directory/'urgent-health-pending.json',dict(signal_id=identifier,recorded_at=stamp(now())))
    return signal


def health(config,at=None,*,application_ready=None,operating=None,urgent=False):
    token=_cooldown_checks.set({})
    try:return _health(config,at,application_ready=application_ready,operating=operating,urgent=urgent)
    finally:_cooldown_checks.reset(token)


def _health(config,at=None,*,application_ready=None,operating=None,urgent=False):
    """Durable deduplicated outbox. No employer or delivery calls here."""
    at=at or now();directory=Path(config['state_directory'])
    previous=read_json(directory/'health.json',{'active':{},'events':[]})
    if urgent:
        # Urgent availability reporting must survive broken source receipts and
        # never open product storage or wait for a full cohort inspection.
        issues=dict(previous.get('active',{}))
        if application_ready is False:
            issues['application:unavailable']=dict(severity='critical',reason='application_readiness_failed',observed_at=stamp(at))
        elif application_ready is True:issues.pop('application:unavailable',None)
    else:issues=health_issues(config,at,application_ready=application_ready)
    old=previous.get('active',{});events=list(previous.get('events',[]));aligned={}
    transitions=[]
    def add(kind,key,*,issue=None,from_key=None,pending=True):
        item=dict(id=uuid.uuid4().hex,kind=kind,key=key,at=stamp(at),delivery='pending' if pending else 'not_applicable')
        if issue is not None:item['issue']=issue
        if from_key is not None:item['from_key']=from_key
        events.append(item)
    processed=list(previous.get('processed_availability_signals',[]))
    signals=[read_json(p) for p in (directory/'availability-signals').glob('*.json')]
    observed_through=(operating or {}).get('application',{}).get('checked_at') or stamp(at)
    for signal in sorted(signals,key=lambda s:s['observed_at']):
        if signal['id'] in processed:continue
        # A failure newer than the readiness check must await another check.
        # Its durable wake-up was written after urgent claimed the previous one.
        if parse(signal['observed_at'])>parse(observed_through):continue
        processed.append(signal['id'])
        if 'application:unavailable' not in old:
            issue=dict(severity='critical',reason=signal['reason'],failure_signal=signal['id'],
                observed_at=signal['observed_at'],recorded_at=signal['recorded_at'],occurred_at=signal.get('occurred_at'))
            old['application:unavailable']=issue
            add('opened','application:unavailable',issue=issue)
            events[-1]['at']=signal['observed_at']
        if application_ready is not True:issues['application:unavailable']=old['application:unavailable']
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
        if kind in ('collection','coverage','accounting','qualification','publication') and key not in issues and any(
                provider+':'+stage in issues for stage in ('collection','coverage','accounting','qualification','publication')):
            target=next(provider+':'+stage for stage in ('collection','coverage','accounting','qualification','publication')
                        if provider+':'+stage in issues)
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
        previous_check=(previous.get('context',{}).get('source_state_checked_at') or previous.get('checked_at')) if key.split(':',1)[0] in SOURCES else previous.get('checked_at')
        if _genuine_resolution(config,key,at,previous_check,application_ready):add('recovered',key)
        else:add('reclassified',key,from_key=key,pending=False)
    if urgent:
        context=dict(previous.get('context',{}))
        context['source_state_checked_at']=context.get('source_state_checked_at') or context.get('checked_at')
        context.update(checked_at=stamp(at),active_incidents=len(issues))
        try:context['cycle']=_current_cycle(config,at,verify_cooldowns=False)
        except (OSError,ValueError):context['cycle']={'state':'unavailable'}
    else:
        context=_health_context(config,issues,at)
        context['source_state_checked_at']=stamp(at)
    context['application_ready']=application_ready
    context['source_issues']={key:value for key,value in issues.items() if key.split(':',1)[0] in SOURCES}
    context['publication_paused']=(directory/'publication-hold.json').exists()
    if operating is not None:
        context['operating']=operating
        context['publication_paused']=operating['collection']['state']!='enabled'
        current=operating['collection']['state']
        prior=previous.get('context',{}).get('operating',{}).get('collection',{}).get('state')
        if operating.get('coherent') and current!=prior:
            add('status_changed','collection:state',issue=dict(previous_state=prior,current_state=current,
                observed_at=operating['collection']['checked_at'],reason=operating['collection'].get('reason')))
    reconciliation=context.get('cycle',{}).get('reporting_reconciliation')
    if not urgent and reconciliation and not any(
            e['key']=='run:accounting_reconciled' and e.get('issue',{}).get('report_id')==reconciliation for e in events):
        add('status_changed','run:accounting_reconciled',issue=dict(report_id=reconciliation,
            run_id=context['cycle']['run_id'],reason='retained_cycle_accounting_reconciled'))
    before=previous.get('context',{}).get('qualified_sources')
    if before is not None:
        before=set(before)|set(previous.get('context',{}).get('recently_verified_sources',[]))
        for source in sorted(set(context['qualified_sources'])-set(before)):
            source_state=read_source_state(config,source) or {}
            previous_check=previous.get('context',{}).get('source_state_checked_at') or previous.get('checked_at')
            if source_state.get('reconciliation_proof') and previous_check and (
                    not source_state.get('ended_at') or parse(source_state['ended_at'])<=parse(previous_check)):
                continue
            if any(event['kind']=='recovered' and event['at']==stamp(at) and
                   event['key'] in tuple(source+':'+kind for kind in ('collection','coverage','accounting','qualification','publication')) for event in events):
                continue
            add('first_verified',source+':verification',issue=dict(severity='info',reason='first_daily_qualifying_observation'))
    result=dict(checked_at=stamp(at),next_scheduled_execution=stamp(max(next_trigger(at),parse(config['first_run_at']))),
        active=issues,events=events,context=context,processed_availability_signals=processed)
    write_json(directory/'health.json',result);return result


def merge_source_history(summary,previous):
    baseline=previous.get('count_baseline',previous.get('observed')) or 0
    observed=summary['observed']
    summary['abnormal_count_drop']=(previous.get('abnormal_count_drop',False) if observed is None else
        bool(baseline>=10 and observed<baseline*.5))
    summary['count_baseline']=baseline if observed is None or summary['abnormal_count_drop'] else observed
    if not summary['cohorts'] and not summary.get('qualifying_observation'):
        summary['cohorts']=previous.get('cohorts',[])
    if not summary['last_qualifying_verification']:
        summary['last_qualifying_verification']=previous.get('last_qualifying_verification')
    if not summary['next_verification_deadline'] and not summary.get('qualifying_observation'):
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


def reconstruct_source_receipt(config,receipt,provider):
    """Read collection and publication independently; never replay a capture."""
    target=Path(config['state_directory'])/'runs'/receipt['run_id']
    started=parse(receipt.get('started_at') or receipt['scheduled_at'])
    ended=parse(receipt['ended_at']) if receipt.get('ended_at') else now()
    row=empty_source(provider,started,outcome='not_started');row['ended_at']=stamp(ended)
    schedule=read_json(target/'coverage-plan.json',{}).get(provider,{})
    row.update(outcome=schedule.get('state','not_started'),reason=schedule.get('reason'),
        attempt_started='no' if schedule.get('state') in ('disabled','blocked','cooldown','not_targeted') else 'unknown',
        capture_outcome='not_recorded',publication_outcome='not_recorded',accounting_status='complete')
    if row['outcome']=='due':row['outcome']='not_started'
    collection=None
    try:
        ref=read_json(target/(provider+'-collection.json'))
        if ref:
            collection=maintenance.report(config['journal'],ref['plan_id'])
            if collection['plan'].get('source')!=provider or collection['plan'].get('run_id')!=receipt['run_id']:
                raise ValueError('collection_run_binding_invalid')
            requests=[e['data'] for e in collection['events'] if e['event']=='source_transport' and e['data'].get('event')=='request']
            measured=summarize_source(dict(plan_id=None,config=dict(providers=[provider]),sources=[dict(jobs=[])]),collection,started,ended)
            for field in ('requests_used','http_responses_received','pages_fetched','request_cap_reached','listing_envelope_shape'):
                if field in measured:row[field]=measured[field]
            row.update(collection_plan_id=ref['plan_id'],capture_outcome=collection['status'],
                attempt_started='yes' if requests else 'no',attempt_started_at=requests[0].get('observed_at') if requests else None,
                started_at=collection['plan']['started_at'],outcome='collected_unpublished'
                    if collection['status']=='collected_unpublished' else 'collection_failed_or_interrupted')
    except (OSError,ValueError,KeyError,TypeError) as error:
        collection=None
        row.update(outcome='accounting_unavailable',requests_used=None,http_responses_received=None,pages_fetched=None,
            attempt_started='unknown',accounting_status='collection_receipt_unavailable',accounting_error_type=type(error).__name__)
    try:
        plan=read_json(target/(provider+'-plan.json'))
        if plan:
            maintenance._validate_plan(plan)
            publication=maintenance.report(config['journal'],plan['plan_id'])
            if plan!=publication['plan'] or plan['config']['providers']!=[provider]:
                raise ValueError('publication_plan_binding_invalid')
            publication=maintenance.committed_publication_report(publication,config['database'])
            if collection is None:raise ValueError('bound_collection_accounting_required')
            from wahojobs.crawler.staged_observation import publication_report
            report=publication_report(collection,publication)
            summary=summarize_source(plan,report,parse(row['started_at']),ended)
            row.update(summary,**publication_fields(summary,publication))
        elif collection and collection['status']=='collected_unpublished':
            row.update(publication_outcome='not_started',outcome='collected_unpublished')
    except (OSError,ValueError,KeyError,TypeError) as error:
        row.update(outcome='accounting_unavailable',publication_outcome='unknown',
            accounting_status='publication_receipt_unavailable',accounting_error_type=type(error).__name__)
    failure=read_json(target/(provider+'-failure.json'))
    if failure:row['worker_error_type']=failure.get('error_type')
    diagnostic=retained_worker_diagnostic(target,receipt['run_id'],provider)
    if diagnostic:row['worker_failure_diagnostic']=diagnostic
    row.update(run_id=receipt['run_id'],trigger=receipt['trigger'],maintenance_seconds=receipt.get('maintenance_seconds'))
    return row


def finish_run_sources(config,receipt):
    """Complete timeout/undispatched summaries using retained records after restore.

    Read-only transaction verification is permitted for prepared receipts. An
    interrupted journal's reservations count; missing accounting stays unknown.
    """
    directory=Path(config['state_directory']);target=directory/'runs'/receipt['run_id']
    rows={}
    for provider in selected_sources(receipt) or SOURCES:
        previous=_retain_old_failure(config,provider,read_source_state(config,provider) or {})
        newer_state=previous.get('run_id','')>receipt['run_id']
        if previous.get('run_id')==receipt['run_id']:newer_state=False
        elif previous.get('started_at') and receipt.get('started_at'):
            try:newer_state=parse(previous['started_at'])>parse(receipt['started_at'])
            except (TypeError,ValueError):pass
        history={} if newer_state else previous
        try:
            row=read_json(target/(provider+'.json'))
            if row is None:
                row=merge_source_history(reconstruct_source_receipt(config,receipt,provider),history)
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
