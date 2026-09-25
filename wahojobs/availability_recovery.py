"""Explicit, once-only targeted availability attempt using the daily publisher."""
from pathlib import Path
from datetime import timedelta
import os
from wahojobs import daily_inventory as d
from wahojobs.maintenance_gate import operation_gate

CAPS={'alignerr':100,'mercor':1}

def selected(receipt):
    sources=receipt.get('availability_sources')
    if sources is None:return None
    if (not isinstance(sources,list) or not sources or len(sources)!=len(set(sources))
            or not set(sources)<=set(CAPS)):
        raise ValueError('targeted_source_scope_invalid')
    return sources

def reserve(config,at,sources):
    sources=selected({'availability_sources':sources})
    if any(not d.source_settings(config)[s]['enabled'] for s in sources):
        raise ValueError('targeted_source_disabled')
    cohorts=d._baseline_cohorts(config,sources)
    # A qualifying observation must already exist and expire before the next
    # scheduled publication's bounded completion. Recheck under the shared gate.
    due=d.next_trigger(at)+timedelta(seconds=d.PUBLICATION_SECONDS)
    if cohorts is None or any(not any(at<d.parse(c['verified_at'])+timedelta(hours=72)<=due
            for c in cohorts[s]) for s in sources):
        raise ValueError('current_targeted_freshness_need_required')
    run_id=d.slot_at(at).strftime('%Y%m%dT060000Z')+'-availability'
    target=Path(config['state_directory'])/'runs'/run_id
    try:target.mkdir(mode=0o700,parents=True,exist_ok=False)
    except FileExistsError:return None
    if os.name=='posix' and os.geteuid()==0:
        owner=target.parent.stat();os.chown(target,owner.st_uid,owner.st_gid)
    receipt=dict(version=d.VERSION,run_id=run_id,trigger='availability_recovery',
        code_commit=config['code_commit'],availability_sources=sources,baseline_cohorts=cohorts,scheduled_at=d.stamp(d.slot_at(at)),
        started_at=d.stamp(at),ended_at=None,outcome='reserved',sources={},maintenance_seconds=0,
        next_scheduled_execution=d.stamp(d.next_trigger(at)))
    d.write_json(target/'run.json',receipt)
    d.write_json(target/'availability-http-ledger.json',dict(run_id=run_id,code_commit=config['code_commit'],sources=sources,
        caps={s:min(CAPS[s],d.source_settings(config)[s]['http_max']) for s in sources},attempts=[]))
    return receipt

def audit(target,source,event):
    if event.get('event')!='request':return
    path=Path(target)/'availability-http-ledger.json'
    with operation_gate(str(path)):
        ledger=d.read_json(path)
        receipt=d.read_json(Path(target)/'run.json',{})
        if (not ledger or ledger.get('run_id')!=receipt.get('run_id')
                or ledger.get('code_commit')!=receipt.get('code_commit')
                or ledger.get('sources')!=selected(receipt) or source not in ledger['caps'] or event.get('kind')!='catalog'):
            raise ValueError('targeted_http_scope_invalid')
        attempts=ledger['attempts'];cap=ledger['caps'][source]
        if (cap>CAPS[source] or sum(a['source']==source for a in attempts)>=cap
                or len(attempts)>=sum(ledger['caps'].values())):
            raise ValueError('targeted_http_budget_consumed')
        attempts.append(dict(event,source=source,cumulative_ordinal=len(attempts)+1))
        d.write_json(path,ledger)  # fsync before transport; uncertainty consumes it.
