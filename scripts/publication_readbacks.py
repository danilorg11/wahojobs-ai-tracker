"""Read-only observations using existing credentials; safe fields only.

The GitHub connector cannot see these repositories. Git's existing credential
helper is used in memory for the same repository's official API, never exported
or written to a packet. No workflow state, deployment, key or grant is changed.
"""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request,urlopen

import publication as p


def github():
    env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='Never')
    reply=subprocess.run(['git','credential','fill'],input=b'protocol=https\nhost=github.com\npath=Wahojobs/wahojobs-back.git\n\n',
        capture_output=True,env=env,shell=False,timeout=30)
    p.require(reply.returncode==0,'existing_git_credential_unavailable')
    values=dict(line.split('=',1) for line in reply.stdout.decode().splitlines() if '=' in line)
    p.require(values.get('password'),'existing_git_credential_unavailable')
    credential=base64.b64encode((values.get('username','x-access-token')+':'+values['password']).encode()).decode()
    def get(path):
        request=Request('https://api.github.com/repos/Wahojobs/wahojobs-back/'+path,headers={
            'Authorization':'Basic '+credential,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28',
            'User-Agent':'WahoJobs-Publication-Readback'})
        with urlopen(request,timeout=30) as response:return json.load(response)
    flows=get('actions/workflows?per_page=100')
    runs=get('actions/runs?per_page=30')
    return dict(observed_at=p.now(),repository='Wahojobs/wahojobs-back',
        workflows=[{k:row.get(k) for k in ('id','name','path','state','updated_at')} for row in flows['workflows']],
        recent_runs=[{k:row.get(k) for k in ('id','workflow_id','status','conclusion','head_sha','created_at','updated_at')} for row in runs['workflow_runs']],
        mutated=False,credential_exported=False)


def vercel():
    folder=Path('C:/Users/danrg/Documents/Codex/2026-09-30/task-2')
    helper=folder/'regional_frontend_release_proof.py'
    p.require(p.digest(helper.read_bytes())=='1dea0886c6da0161606c33075bb8e9311247e4e043da0e77585c2ce0d09bdff1','existing_vercel_client_changed')
    sys.path.insert(0,str(folder));import regional_frontend_release_proof as client
    aliases=[client.meta(host) for host in ('www.wahojobs.com','worksmarter.wahojobs.com')]
    project=json.loads(client.cli('api','/v9/projects/prj_m2RkRKpgA79X6BLqVXqHm0rNVEgv','--method','GET','--raw'))
    return dict(observed_at=p.now(),aliases=aliases,project={k:project.get(k) for k in ('id','accountId','lastAliasRequest','rollingRelease')},
        git_integration={k:project.get('link',{}).get(k) for k in ('type','repo','productionBranch','deployHooks')},
        mutated=False,new_private_host_access=False)


def main(output):
    destination=Path(output);destination.mkdir(parents=True,exist_ok=True);result={}
    for name,action in (('github',github),('vercel',vercel)):
        started=time.monotonic()
        try:
            value=action();p.once(destination/(name+'.json'),value)
            result[name]=dict(observed=True,path=str(destination/(name+'.json')),seconds=time.monotonic()-started)
        except Exception as error:
            result[name]=dict(observed=False,error_class=type(error).__name__,seconds=time.monotonic()-started)
    p.once(destination/'result.json',result);return result


if __name__=='__main__':print(json.dumps(main(sys.argv[1])))
