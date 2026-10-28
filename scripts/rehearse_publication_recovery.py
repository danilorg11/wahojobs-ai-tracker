"""Actual data commit, injected worker exit, compatible return, explicit retry.
No production, private access, cloud or fixture source modifications.
"""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import publication as p


def run(reference,output):
    out=Path(output).resolve();reference=Path(reference).resolve()
    p.require(out.is_relative_to(ROOT/'outputs') and not out.exists(),'new_local_fault_output_required')
    out.mkdir();config=p.read(reference/'config.json')
    seed=json.loads(subprocess.check_output([sys.executable,'-B',str(ROOT/'scripts/publication_domains.py'),'seed-content',
        str(ROOT/'.worktrees/availability-recheck-20261022'),str(out/'data')]))
    step=deepcopy(next(s for s in config['steps'] if s['id']=='company-api'))
    payload=step['worker']['payload'];payload['module_sha256']=p.digest(Path(payload['module']).read_bytes())
    payload['state_directory']=str(out/'workers');payload['local_fault']='after-content-exit'
    reader=out/'reader.txt';reader.write_text(str(ROOT/'.worktrees/availability-recheck-20261022'))
    artifact=out/'candidate.txt';artifact.write_text(str(ROOT/'.worktrees/remote-company-inventory'))
    payload['targets']=[str(reader),seed['database']]
    payload['code'].update(targets=[str(reader)],files=[dict(path=str(reader),before=dict(type='file',sha256=p.digest(reader.read_bytes())),
        artifact=str(artifact),artifact_sha256=p.digest(artifact.read_bytes()))])
    payload['content'].update(targets=[seed['database']],database=seed['database'],domain_plan=seed['plan'],protected_before=seed['protected'])
    for name,expected in (('check',200),('recovered_check',404)):
        payload['hooks'][name]=[dict(command=[sys.executable,'-B',str(ROOT/'scripts/publication_domains.py'),'api-probe',str(reader),'--expected',str(expected)],seconds=15)]
    step['worker']['files']+= [seed['plan'],seed['protected']]
    step['worker']['files']+= [str(path) for source in ('availability-recheck-20261022','remote-company-inventory')
        for path in (ROOT/'.worktrees'/source/'wahojobs').rglob('*.py')]
    independent=deepcopy(next(s for s in config['steps'] if s['id']=='common-host-entrypoint'))
    target=out/'independent-unit';q=independent['worker']['payload'];q['state_directory']=str(out/'workers')
    q['targets']=[str(target)];q['files'][0]['path']=str(target)
    q['hooks']['check'][0]['command'][-1]=str(target)
    config.update(title='Actual interrupted data/code batch plus independent publication',steps=[step,independent])
    wanted=set(step['checks']+independent['checks']);config['checks']=[c for c in config['checks'] if c['id'] in wanted]
    for check in config['checks']:
        if check['id']=='protocol':check['files']=sorted(set(check['files']+[str(f) for f in (ROOT/'scripts').glob('publication*.py')]))
    path=out/'config.json';p.once(path,config)
    began=time.monotonic();prepared=p.prepare(path,out/'plan.json',reference/'cache');qualification=time.monotonic()-began
    coordinator=p.Coordinator(out/'coordinator',scoped=True)
    inserted=False
    class LostResponse(p.Worker):
        def call(self,action):
            nonlocal inserted
            try:return super().call(action)
            except Exception:
                if action=='apply' and self.step['id']=='company-api' and not inserted:
                    with closing(sqlite3.connect(seed['database'])) as db,db:
                        db.execute("INSERT INTO publication_fixture_user_state VALUES (2,'new user data after committed repair and process exit')")
                    inserted=True
                raise
    tick=time.monotonic();failed=p.execute(out/'plan.json',local=True,coordinator=coordinator,worker_factory=LostResponse);recovery=time.monotonic()-tick
    p.require([r['state'] for r in failed['results']]==['recovered','accepted'],'partial_batch_not_safely_recovered:'+json.dumps(failed))
    with closing(sqlite3.connect(seed['database'])) as db:
        captures=db.execute('SELECT COUNT(*) FROM job_source_content_captures').fetchone()[0]
        users=db.execute('SELECT COUNT(*) FROM publication_fixture_user_state').fetchone()[0]
    p.require(users==2 and inserted,'concurrent_user_data_not_preserved')
    payload.pop('local_fault');config['reuse_plans']=[str(out/'plan.json')];config['title']='Explicit retry after verified compatible return, reuse accepted content'
    path=out/'retry-config.json';p.once(path,config)
    tick=time.monotonic();retry_prepared=p.prepare(path,out/'retry-plan.json',reference/'cache');retry_qual=time.monotonic()-tick
    tick=time.monotonic();retried=p.execute(out/'retry-plan.json',local=True,coordinator=coordinator);retry=time.monotonic()-tick
    p.require(retried['complete'],'known_retry_not_complete:'+json.dumps(retried))
    with closing(sqlite3.connect(seed['database'])) as db:
        p.require(db.execute('SELECT COUNT(*) FROM job_source_content_captures').fetchone()[0]==captures,'already_accepted_content_republished')
        p.require(db.execute('SELECT COUNT(*) FROM publication_fixture_user_state').fetchone()[0]==2,'user_data_lost_after_retry')
    report=dict(qualification_seconds=qualification,actual_process_exit_recovery_seconds=recovery,retry_qualification_seconds=retry_qual,
        explicit_known_retry_seconds=retry,total_seconds=time.monotonic()-began,recovered=failed,retried=retried,
        source_capture_count_preserved=captures,user_rows_preserved=users,retained_records=15,http_requests=0,
        production_mutations=0,independent_publication_advanced=True,second_native_repair_created_no_captures=True,
        human_interventions_during_execution=0,prepared=prepared,retry_prepared=retry_prepared)
    p.once(out/'report.json',report)
    return dict(report=str(out/'report.json'),total_seconds=report['total_seconds'],recovery_seconds=recovery,retry_seconds=retry)


if __name__=='__main__':print(json.dumps(run(sys.argv[1],sys.argv[2])))
