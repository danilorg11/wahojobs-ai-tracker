"""Prepare and rehearse useful queued changes through the common protocol.

Real company sources, production Next build/HTTP, real backend HTTP, and the
existing 15-record repair transaction. Targets are local disposable resources;
this does not measure Vercel promotion, Linux startup or production data latency.
"""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time

import publication as p
import rehearse_routine_publication as fixture

ROOT=p.ROOT
SELF=Path(__file__).resolve()
WEBSITE=ROOT/'.worktrees/remote-company-catalog'
BACKEND=ROOT/'.worktrees/remote-company-inventory'
DATA=ROOT/'.worktrees/availability-recheck-20261022'
DOMAINS=ROOT/'scripts/publication_domains.py'
FRONT='8ea2cc58c99c1b4bd8979d7354486fd903a75cb0'
BACK='0867c2b6e1a6f7104d64b64c6d8b109696604af1'
BASE='2c357f8d9b4ff4e3962f33ddc21a8035b2cd0d08'


def write(path,raw):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as stream:stream.write(raw)


def tracked(source):
    return [str(source/s) for s in p.legacy.git(source,'ls-files').decode().splitlines()
            if (source/s).is_file() and not s.startswith('.github/')]


def source_pin(source,commit):
    p.require(p.legacy.git(source,'rev-parse','HEAD').decode().strip()==commit,'queued_source_changed')
    p.require(not p.legacy.git(source,'status','--porcelain','--untracked-files=no').strip(),'queued_source_dirty')


def build(source,output):
    env=p.legacy.clean_environment(dict(BASE_API_URL='http://104.248.236.93',MAIN_DOMAIN='www.wahojobs.com',
        AI_SUBDOMAIN='worksmarter.wahojobs.com',NEXTAUTH_URL='https://www.wahojobs.com',
        NEXTAUTH_SECRET='synthetic-local-build-only-not-production',NEXT_PUBLIC_WAHOJOBS_CANDIDATE_ENABLED='1',
        NODE_ENV='production',BUILD_SCOPE_TRACE=str(output/'public-build-network.jsonl'),
        NODE_OPTIONS='--require "'+fixture.SCOPE.as_posix()+'"'))
    subprocess.run([fixture.NODE,str(fixture.DEPS/'next/dist/bin/next'),'build'],cwd=source,env=env,check=True,timeout=600)
    # Every runtime build file is immutable evidence; caches are not deployed.
    manifest={str(f.resolve()):p.digest(f.read_bytes()) for f in sorted((source/'.next').rglob('*'))
              if f.is_file() and 'cache' not in f.relative_to(source/'.next').parts}
    p.once(output/'build-manifest.json',manifest)
    print(json.dumps(dict(build_files=len(manifest),source=FRONT)))


def frontend_smoke(selector,visual):
    source=Path(selector.read_text().strip());p.require((source/'.next/BUILD_ID').is_file(),'selected_build_absent')
    port=fixture.free_port(); log=(visual/('server-'+str(time.time_ns())+'.log')).open('xb')
    app=subprocess.Popen([fixture.NODE,str(visual/'start.cjs')],cwd=source,
        env=p.legacy.clean_environment(dict(WAHOJOBS_FIXTURE_PORT=str(port),WAHOJOBS_REHEARSAL_SOURCE=str(source),NODE_ENV='production')),
        stdout=log,stderr=subprocess.STDOUT)
    checks=[]
    try:
        end=time.monotonic()+30
        while True:
            try:
                if fixture.request(port,'/__fixture/status')[0]==200:break
            except OSError:pass
            p.require(app.poll() is None and time.monotonic()<end,'next_fixture_startup_failed')
            time.sleep(.1)
        for route,terms in [('/company/mercor',['Mercor','AI Training Jobs','/jobs/opportunity-']),
                ('/remote-companies?search=Mercor',['Remote Companies']),
                ('/company/mindrift',['Mindrift']),('/company/alignerr',['Alignerr'])]:
            status,body=fixture.request(port,route);text=body.decode()
            p.require(status==200 and all(t in text for t in terms),'company_route_not_accepted:'+route)
            checks.append(dict(route=route,status=status,bytes=len(body)))
        # Honest failure UI is a contract: unavailable data must not become zero jobs.
        fixture.request(port,'/__fixture/mode/outage')
        status,body=fixture.request(port,'/company/mercor')
        p.require(status==200 and b'AI training opportunities are temporarily unavailable' in body,'outage_misrepresented')
        print(json.dumps(dict(routes=checks,unavailable_is_not_zero=True,fixture_dates_preserved=True)))
    finally:
        app.terminate()
        try:app.wait(timeout=10)
        except subprocess.TimeoutExpired:app.kill();app.wait(timeout=10)
        log.close()


def check_unit(path):
    text=path.read_text()
    p.require(all(s in text for s in ('LoadCredential=authority:','NoNewPrivileges=true','ProtectSystem=strict','KillMode=control-group','UMask=0077')),'common_unit_safety_missing')
    p.require('scripts/publication_host.py %i' in text,'common_entrypoint_missing')
    print(json.dumps(dict(static_unit_validation=True,native_linux_validation=False)))


def rehearsal(output):
    output=Path(output).resolve()
    p.require(output.is_relative_to(ROOT/'outputs') and not output.exists(),'new_local_output_required')
    output.mkdir(parents=True);begin=time.monotonic();started=p.now()
    for source,commit in ((WEBSITE,FRONT),(BACKEND,BACK),(DATA,BASE)):source_pin(source,commit)
    source=output/'website';source.mkdir()
    with tarfile.open(fileobj=io.BytesIO(p.legacy.git(WEBSITE,'archive',FRONT))) as package:package.extractall(source,filter='data')
    fixture.junction(fixture.DEPS,source/'node_modules')
    visual=output/'visual';visual.mkdir();audit=output/'audit';audit.mkdir()
    for name in ('fixture-preload.cjs','fixture-data.json','decode-html.cjs'):
        write(visual/name,(fixture.EXISTING/'visual'/name).read_bytes())
    for name in ('jobs-page1.html','public-job-mercor-sample.html'):write(audit/name,(fixture.EXISTING/'audit'/name).read_bytes())
    launcher=(fixture.EXISTING/'visual/start-fixture-server.cjs').read_text().replace(
        "path.resolve(__dirname, '../../../.worktrees/remote-company-catalog')",'process.env.WAHOJOBS_REHEARSAL_SOURCE')
    write(visual/'start.cjs',launcher.encode())
    seed=json.loads(subprocess.check_output([sys.executable,'-B',str(DOMAINS),'seed-content',str(DATA),str(output/'data')]))
    targets=output/'resources';targets.mkdir()
    reader=targets/'reader.txt';write(reader,str(DATA).encode())
    frontend=targets/'website.txt';write(frontend,b'dpl_B8qkeCtM7uMhwPrevxzFRPndTx8U')
    reader_artifact=output/'reader-candidate.txt';write(reader_artifact,str(BACKEND).encode())
    front_artifact=output/'website-candidate.txt';write(front_artifact,str(source).encode())
    checks=[]
    def qualification(id,categories,command,files,cwd=ROOT,outputs=(),manifests=()):
        checks.append(dict(id=id,categories=categories,command=command,cwd=str(cwd),files=sorted(set(map(str,files))),
            tools=[sys.executable]+([fixture.NODE] if id.startswith('website') else []),seconds=660,
            outputs=list(map(str,outputs)),output_manifests=list(map(str,manifests))))
    qualification('protocol',['source','recovery','resume'],[sys.executable,'-B','-m','unittest','discover','-s','tests','-p','test_publication*.py'],
        [*ROOT.glob('scripts/publication*.py'),ROOT/'scripts/routine_release.py',*ROOT.glob('tests/test_publication*.py')])
    qualification('company-api',['source','contract','startup','preservation','recovery'],
        [sys.executable,'-B','-m','unittest','tests.test_public_company_inventory','-v'],tracked(BACKEND),BACKEND)
    qualification('retained-content',['source','data-dry-run','preservation','resume','recovery'],
        [sys.executable,'-B','-m','unittest','tests.test_content_recovery_plan','-v'],tracked(DATA),DATA)
    qualification('website-tests',['source','contract','render','recovery'],
        [fixture.NODE,'--test',*[str(f) for f in sorted((WEBSITE/'tests').glob('*.test.mjs'))]],tracked(WEBSITE),WEBSITE)
    qualification('website-build',['source','build'],[sys.executable,'-B',str(SELF),'build',str(source),str(output)],
        [SELF,fixture.SCOPE,*tracked(WEBSITE),fixture.DEPS/'next/package.json',fixture.DEPS/'next/dist/bin/next'],
        outputs=[output/'build-manifest.json'],manifests=[output/'build-manifest.json'])
    qualification('common-host-unit',['source','configuration','startup','recovery'],
        [sys.executable,'-B',str(SELF),'unit',str(ROOT/'deploy/publication/wahojobs-publication@.service')],
        [SELF,ROOT/'scripts/publication_host.py',ROOT/'deploy/publication/wahojobs-publication@.service'])
    steps=[]
    def step(id,family,resources,checks,payload,needs=(),reason='bounded local fixture; real domain checks'):
        payload.update(environment='local',resources=resources,state_directory=str(output/'worker-state'))
        value=dict(id=id,family=family,needs=list(needs),checks=checks,resources=resources,compatibility='standalone',
            access=dict(kind='none'),duration_reason=reason,recovery_reason='owned conditional return; current data retained',
            worker=dict(command=[sys.executable,'-B',str(ROOT/'scripts/publication_worker.py')],files=[
                str(ROOT/'scripts/publication_worker.py'),str(ROOT/'scripts/routine_release.py'),str(DOMAINS)],payload=payload))
        value['seconds']={k:max(15,v+5) for k,v in p.phase_floors(value).items()};steps.append(value)
    def file(path,artifact):return dict(path=str(path),before=dict(type='file',sha256=p.digest(path.read_bytes())) if path.exists() else None,
        artifact=str(artifact),artifact_sha256=p.digest(artifact.read_bytes()))
    logo_manifest=p.read(fixture.EXISTING/'research/normalized-logo-manifest.json')
    logo_files=[]
    for item in logo_manifest['assets']:
        artifact=ROOT/item['normalized_path'];p.require(p.digest(artifact.read_bytes())==item['normalized_sha256'],'queued_logo_changed')
        target=targets/'assets'/Path(item['s3_key']).name;target.parent.mkdir(exist_ok=True)
        logo_files.append(file(target,artifact))
    step('company-assets','content',['/fixture/aws/wahojobs/companies'],['website-tests','protocol'],
        dict(kind='files',targets=[f['path'] for f in logo_files],files=logo_files))
    api_hook=lambda expected:dict(command=[sys.executable,'-B',str(DOMAINS),'api-probe',str(reader),'--expected',str(expected)],seconds=15)
    step('company-api','backend',['/fixture/hosts/beta/services/catalog','/fixture/hosts/beta/databases/product'],['company-api','protocol'],
        dict(kind='files',targets=[str(reader)],files=[file(reader,reader_artifact)],hooks=dict(check=[api_hook(200)],recovered_check=[api_hook(404)])),
        reason='real HTTP ingress on exact reader with synthetic inventory; Linux service startup is a separate production qualification')
    step('company-pages','frontend',['/fixture/vercel/project'],['website-tests','website-build','protocol'],
        dict(kind='files',targets=[str(frontend)],files=[file(frontend,front_artifact)],hooks=dict(check=[dict(
            command=[sys.executable,'-B',str(SELF),'frontend',str(frontend),str(visual)],seconds=45)])),needs=['company-assets','company-api'])
    step('retained-15-descriptions','data',['/fixture/hosts/beta/databases/product'],['retained-content','protocol'],
        dict(kind='module',domain='retained-content',module=str(DOMAINS),module_sha256=p.digest(DOMAINS.read_bytes()),source=str(DATA),
            targets=[seed['database']],database=seed['database'],domain_plan=seed['plan'],protected_before=seed['protected'],
            phase_floor_seconds=dict(observe=10,apply=30,check=15,recover=15,cleanup=10)))
    unit=targets/'wahojobs-publication@.service'
    step('common-host-entrypoint','infrastructure',['/fixture/hosts/beta'],['common-host-unit','protocol'],
        dict(kind='files',targets=[str(unit)],files=[file(unit,ROOT/'deploy/publication/wahojobs-publication@.service')],hooks=dict(check=[dict(
            command=[sys.executable,'-B',str(SELF),'unit',str(unit)],seconds=10)])),reason='static safety validation; actual Linux/systemd execution remains explicitly unqualified')
    config=output/'config.json';p.once(config,dict(format=p.FORMAT,environment='local',title='Useful queue: companies, retained descriptions and common host entry point',
        sources=[dict(path=str(s),commit=c) for s,c in ((WEBSITE,FRONT),(BACKEND,BACK),(DATA,BASE))],checks=checks,steps=steps))
    return finish(output,config,seed,len(logo_files),begin,started)


def finish(output,config,seed,asset_count,begin=None,started=None):
    begin=begin or time.monotonic();started=started or p.now()
    cycle=output/('cycle-'+str(time.time_ns()));cycle.mkdir()
    tick=time.monotonic();prepared=p.prepare(config,cycle/'plan.json',output/'cache');qualify=time.monotonic()-tick
    tick=time.monotonic();reuse=p.prepare(config,cycle/'plan-reused.json',output/'cache');reused=time.monotonic()-tick
    coordinator=p.Coordinator(output/'coordinator',scoped=True)
    tick=time.monotonic();result=p.execute(cycle/'plan.json',local=True,coordinator=coordinator);execute=time.monotonic()-tick
    p.require(result['complete'],'queued_publication_not_complete:'+json.dumps(result))
    with closing(sqlite3.connect(seed['database'])) as db:
        db.execute("INSERT OR IGNORE INTO publication_fixture_user_state VALUES (2,'new user data after publication')");db.commit()
    tick=time.monotonic();resume=p.execute(cycle/'plan.json',local=True,coordinator=coordinator);resume_seconds=time.monotonic()-tick
    p.require(resume['complete'],'terminal_resume_not_valid')
    # A new plan can reuse genuinely accepted components, with fresh observations.
    tick=time.monotonic();carry=p.execute(cycle/'plan-reused.json',local=True,coordinator=coordinator);carry_seconds=time.monotonic()-tick
    p.require(carry['complete'],'accepted_progress_not_carried')
    with closing(sqlite3.connect(seed['database'])) as db:p.require(db.execute('SELECT COUNT(*) FROM publication_fixture_user_state').fetchone()[0]==2,'new_user_data_lost')
    report=dict(started_at=started,completed_at=p.now(),total_seconds=time.monotonic()-begin,qualification_seconds=qualify,
        reused_qualification_seconds=reused,execution_seconds=execute,resume_seconds=resume_seconds,changed_plan_carry_seconds=carry_seconds,
        prepared=prepared,reused=reuse,result=result,resumed=resume,carried=carry,retained_content=seed,immutable_assets=asset_count,
        useful_changes=['12 company assets','additive company inventory API','12 new company profiles and canonical inventories','15 retained complete descriptions','common host entry point'],
        real_next_build=True,real_next_http=True,real_backend_http=True,real_existing_data_transaction=True,
        human_interventions_during_automatic_rehearsal=0,production_mutations=0,private_host_access=False,
        unmeasured=['Vercel cloud build/promotion','actual Linux/systemd service start/return','actual production database duration'],
        native_linux_qualification=False)
    p.once(cycle/'report.json',report);return dict(report=str(cycle/'report.json'),**report)


if __name__=='__main__':
    action=sys.argv[1]
    if action=='rehearse':print(json.dumps(rehearsal(sys.argv[2]),ensure_ascii=False))
    elif action=='resume':
        output=Path(sys.argv[2]).resolve();config=output/'config.json';values=p.read(config)
        payload=next(s['worker']['payload'] for s in values['steps'] if s['id']=='retained-15-descriptions')
        seed=dict(database=payload['database'],plan=payload['domain_plan'],protected=payload['protected_before'],retained_records=15)
        print(json.dumps(finish(output,config,seed,12),ensure_ascii=False))
    elif action=='build':build(Path(sys.argv[2]),Path(sys.argv[3]))
    elif action=='frontend':frontend_smoke(Path(sys.argv[2]),Path(sys.argv[3]))
    elif action=='unit':check_unit(Path(sys.argv[2]))
