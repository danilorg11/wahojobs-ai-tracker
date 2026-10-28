"""One compatible beta cutover: retained repair plus code, one stop/start.

Outer common worker owns maintenance gate and service hooks. Both old/new
readers must be qualified for the amended data. Recovery returns only owned
code/policy, retains accepted content and restarts the compatible accepted site.
"""
from pathlib import Path
import os

import publication_domains as data
import publication_worker as files
import routine_release as r


def publication(action,request,root):
    root=Path(root);payload=request['payload']
    if payload.get('local_fault'):
        r.require(payload['environment']=='local' and payload['local_fault']=='after-content-exit','test_fault_forbidden_in_production')
    r.require(set(payload['code']['targets'])<=set(payload['targets']) and payload['content']['database'] in payload['targets'],'beta_subtargets_not_declared')
    def child(part,verb):
        value=dict(request,action=verb,payload=dict(payload[part],
            environment=payload['environment'],resources=payload['resources'],state_directory=str(root/(part+'-worker'))))
        if part=='code':return files.Worker(value).call()
        return data.content(verb,value,root/'content-worker')
    (root/'content-worker').mkdir(exist_ok=True)
    result=lambda state,**kw:dict(operation=request['operation'],signature=request['signature'],state=state,**kw)
    code,content=child('code','observe'),child('content','observe')
    def observed():
        if 'foreign' in (code['state'],content['state']):return result('foreign')
        if (root/'recovered.json').exists() and code['state'] in ('before','recovered') and content['state'] in ('before','recovered'):
            return result('recovered',data_restored=False,compatible_accepted_code=True)
        if code['state'] in ('applied','accepted') and content['state'] in ('applied','accepted'):
            return result('accepted' if (root/'accepted.json').exists() else 'applied')
        if code['state']=='before' and content['state']=='before' and not (root/'dispatch.json').exists():return result('before')
        if 'unknown' in (code['state'],content['state']):return result('unknown')
        if (root/'dispatch.json').exists():return result('failed',recovery_required=True,owned_partial=True)
        return result('foreign')
    if action in ('observe','cleanup'):return observed()
    if action=='apply':
        r.require(observed()['state']=='before' and not (root/'dispatch.json').exists(),'beta_batch_not_admissible_or_already_dispatched')
        r.once(root/'dispatch.json',dict(at=r.now()))
        child('content','apply')
        if payload.get('local_fault')=='after-content-exit':os._exit(75)
        child('code','apply')
        return result('applied',service_restarts_requested_by_domain=0,retained_records=15)
    if action=='check':
        r.require(observed()['state'] in ('applied','accepted'),'beta_candidate_not_selected')
        r.require(child('code','check')['state']=='accepted' and child('content','check')['state']=='accepted','beta_batch_contract_failed')
        if not (root/'accepted.json').exists():r.once(root/'accepted.json',dict(at=r.now(),retained_records=15))
        return result('accepted',retained_records=15,data_restored=False)
    r.require(action=='recover' and (root/'dispatch.json').exists() and observed()['state']!='foreign','beta_return_not_owned')
    if code['state'] in ('applied','accepted','failed'):child('code','recover')
    elif code['state'] not in ('before','recovered'):raise r.Stop('beta_code_outcome_not_reconciled')
    r.require(child('content','recover')['state']=='recovered','beta_data_compatible_exit_not_verified')
    if not (root/'recovered.json').exists():r.once(root/'recovered.json',dict(at=r.now(),data_restored=False))
    return result('recovered',reviewed_records=15,data_restored=False)
