"""Narrow systemd entry point; prepared request/authority, durable safe response.

Not an installer, sudo grant or generic shell. Account/service access must be
explicitly provisioned in the final operation-specific unit drop-in.
"""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

import routine_release as r


def run(dispatch, base=Path('/var/lib/wahojobs-publication')):
    r.require(re.fullmatch('[a-f0-9]{64}',dispatch) is not None,'operation_and_request_nonce_required')
    operation,nonce=dispatch[:32],dispatch[32:]
    request=base/'requests'/(dispatch+'.json')
    info=request.lstat()
    r.require(stat.S_ISREG(info.st_mode) and not request.is_symlink(),'regular_request_required')
    if os.name!='nt':
        r.require(info.st_uid==os.geteuid() and stat.S_IMODE(info.st_mode)==0o600,'private_owned_request_required')
    raw=request.read_bytes(); value=json.loads(raw)
    r.require(value['operation']==operation and value['payload']['environment']=='production','bound_production_request_required')
    seconds=value['phase_budget_seconds']
    r.require(type(seconds) is int and 0<seconds<=86400,'bounded_prepared_phase_required')
    output=base/'responses'/operation; output.mkdir(parents=True,exist_ok=True)
    # One request per phase/dispatch; the common driver retains the original ID.
    target=output/(nonce+'.json')
    with r.file_lock(output/'driver.lock'):
        if target.exists():return r.read(target)
        intent=output/(nonce+'-intent.json')
        r.require(not intent.exists(),'host_phase_outcome_unknown_observe_original_worker')
        r.once(intent,dict(request_sha256=r.digest(raw),at=r.now()))
        tick=time.monotonic()
        result=subprocess.run([sys.executable,'-B',str(Path(__file__).with_name('publication_worker.py'))],
            input=raw,capture_output=True,shell=False,timeout=seconds)
        r.require(result.returncode==0 and len(result.stdout)<=262144,'host_phase_failed_or_unknown_reconcile_worker')
        response=json.loads(result.stdout)
        r.require(response['operation']==operation and response['signature']==value['signature'],'host_response_not_bound')
        r.once(target,dict(response=response,seconds=time.monotonic()-tick,at=r.now()))
        return r.read(target)


if __name__=='__main__':
    try:print(json.dumps(run(sys.argv[1])))
    except Exception as error:
        print(json.dumps(dict(stopped=True,reason=str(error) if isinstance(error,r.Stop) else type(error).__name__)))
        raise SystemExit(1)
