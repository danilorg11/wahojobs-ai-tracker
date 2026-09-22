"""Native beta daily supervisor, worker, recovery and stored-state health CLI.

Requires an explicitly activated, root-owned policy. Nothing runs on import.
"""
import argparse
from contextlib import suppress
from datetime import timedelta
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from wahojobs import daily_inventory as daily
from wahojobs.maintenance_gate import operation_gate


def bounded_process(args,*,timeout,cwd=ROOT,user=None):
    """Terminate the entire worker group before attempting normal-service recovery."""
    if timeout<=0:raise TimeoutError('execution_deadline_expired')
    options={}
    if user is not None:
        import pwd
        account=pwd.getpwnam(user)
        options=dict(user=account.pw_uid,group=account.pw_gid,extra_groups=[])
    proc=subprocess.Popen(args,cwd=cwd,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1'},
        start_new_session=True,**options)
    try:
        if proc.wait(timeout=max(.01,timeout))!=0:raise RuntimeError('bounded_process_failed')
    except BaseException:
        with suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGKILL)
        proc.wait(timeout=5)
        raise


def private_policy(path):
    path=Path(path)
    if not path.is_absolute() or path.resolve(strict=True)!=path:raise ValueError('policy_path_invalid')
    if os.name!='posix':raise ValueError('native_linux_supervisor_required')
    st=path.stat()
    if st.st_uid!=0 or st.st_mode & 0o022 or st.st_nlink!=1:raise ValueError('root_owned_policy_required')
    return daily.read_json(path)


def verify_release_configuration(config, *, require_effective=True):
    """Shared pins also apply to recovery when the beta process is stopped."""
    import socket
    from wahojobs.remote_beta import validate_remote_configuration
    if os.geteuid()!=0 or socket.gethostname()!=daily.HOST:raise ValueError('wrong_operating_host')
    validate_operator_paths(config)
    expected=Path('/opt/wahojobs-beta/releases')/config['code_commit']
    if ROOT!=expected or Path('/opt/wahojobs-beta/current').resolve(strict=True)!=expected:
        raise ValueError('selected_release_mismatch')
    raw=Path(daily.RUNTIME).read_bytes()
    if require_effective and raw!=Path('/run/wahojobs-beta/runtime.json').read_bytes():raise ValueError('effective_runtime_mismatch')
    document=json.loads(raw);validate_remote_configuration(document)
    if (document['database_path']!=daily.DATABASE or document['environment_namespace']!='private_beta'
            or document['public_origin']!='https://beta.wahojobs.com' or document.get('professional_background_companion') is not None):
        raise ValueError('authoritative_beta_configuration_required')
    document.clear();raw=None
    credential=json.loads(subprocess.check_output(['/usr/bin/busctl','--system','--json=short','--no-pager',
        'get-property','org.freedesktop.systemd1','/org/freedesktop/systemd1/unit/wahojobs_2dbeta_2eservice',
        'org.freedesktop.systemd1.Service','LoadCredential'],timeout=10))
    if credential!={'type':'a(ss)','data':[['runtime.json',daily.RUNTIME]]}:raise ValueError('credential_selection_mismatch')


def verify_runtime(config):
    """Pin actual beta service/configuration before causing an unavailable interval."""
    verify_release_configuration(config)
    if '/system.slice/wahojobs-inventory.service' not in Path('/proc/self/cgroup').read_text():
        raise ValueError('invoke_the_native_inventory_service')
    expected=Path('/opt/wahojobs-beta/releases')/config['code_commit']
    state=dict(line.split('=',1) for line in subprocess.check_output(['/usr/bin/systemctl','show',daily.SERVICE,
        '--property=ActiveState,SubState,MainPID,User,ControlGroup'],timeout=10,text=True).splitlines())
    if (state['ActiveState']!='active' or state['SubState']!='running' or state['User']!='wahojobs-beta'
            or state['ControlGroup']!='/system.slice/'+daily.SERVICE):raise ValueError('beta_not_in_normal_service')
    proc=Path('/proc')/state['MainPID']
    expected_command=['/opt/wahojobs-beta/current/.venv/bin/python','-B','scripts/private_beta_app.py',
        '--config','/run/wahojobs-beta/runtime.json','--logs','/var/log/wahojobs-beta']
    if proc.joinpath('cmdline').read_bytes().rstrip(b'\0').decode().split('\0')!=expected_command or proc.joinpath('cwd').resolve()!=expected:
        raise ValueError('beta_process_mismatch')


def validate_operator_paths(config):
    import pwd,stat
    owner=pwd.getpwnam('wahojobs-beta').pw_uid
    root=Path(config['state_directory'])
    for path in (root,root/'runs',root/'backups'):
        value=path.stat()
        if path.resolve(strict=True)!=path or not stat.S_ISDIR(value.st_mode) or value.st_uid!=owner or stat.S_IMODE(value.st_mode)!=0o700:
            raise ValueError('operator_directories_must_be_prepared')
    path=Path(str(config['database'])+'.wahojobs-maintenance.lock')
    value=path.stat()
    if path.resolve(strict=True)!=path or value.st_uid!=owner or stat.S_IMODE(value.st_mode)!=0o600:
        raise ValueError('shared_operator_gate_must_be_prepared')


def native_trigger():
    # systemd 252+ binds these values to this invocation, including queued jobs.
    # A timer's latest firing alone cannot identify a later manual invocation.
    from datetime import datetime,timezone
    import re
    invocation=os.environ.get('INVOCATION_ID','')
    unit=os.environ.get('TRIGGER_UNIT')
    value=os.environ.get('TRIGGER_TIMER_REALTIME_USEC','')
    result=dict(trigger='unknown',trigger_unit=unit if unit=='wahojobs-inventory.timer' else None,
        triggered_at=None,invocation_id=invocation if re.fullmatch('[a-f0-9]{32}',invocation) else None,
        provenance='native_invocation_metadata_unavailable')
    if unit=='wahojobs-inventory.timer' and value.isdecimal():
        try:fired=datetime.fromtimestamp(int(value)/1_000_000,timezone.utc)
        except (OverflowError,OSError,ValueError):return result
        if int(value)>0 and fired<=daily.now():
            result.update(trigger='timer_catch_up' if (fired-daily.slot_at(fired)).total_seconds()>60 else 'timer',
                triggered_at=daily.stamp(fired),provenance='systemd_invocation_environment')
    return result


def claim_dispatch(directory,run_id,parent_pid,monotonic,phase=None):
    target=Path(directory)/'runs'/run_id
    receipt=daily.read_json(target/'run.json')
    if (not receipt or receipt.get('run_id')!=run_id or receipt.get('outcome')!='running'
            or receipt.get('supervisor_pid')!=parent_pid
            or not isinstance(receipt.get('execution_deadline_monotonic'),(int,float))):
        raise ValueError('active_supervisor_reservation_required')
    phase_deadline=receipt['execution_deadline_monotonic']
    if phase is not None:
        dispatch=receipt.get('active_phase',{})
        if dispatch.get('name')!=phase or not isinstance(dispatch.get('deadline'),(float,int)):
            raise ValueError('active_phase_reservation_required')
        phase_deadline=min(phase_deadline,dispatch['deadline'])
    remaining=phase_deadline-monotonic
    if not 0<remaining<=daily.EXECUTION_SECONDS:raise ValueError('worker_execution_deadline_expired')
    # Reservation precedes every possible provider request and survives crashes.
    with (target/((phase or 'worker')+'-dispatch.claim')).open('x') as stream:
        stream.write(str(os.getpid()));stream.flush();os.fsync(stream.fileno())
    return remaining


def claim_worker(config,run_id,phase):
    import pwd,socket,math
    if (socket.gethostname()!=daily.HOST or os.geteuid()!=pwd.getpwnam('wahojobs-beta').pw_uid
            or ROOT!=Path('/opt/wahojobs-beta/releases')/config['code_commit']):
        raise ValueError('wrong_worker_environment')
    parent=Path('/proc')/str(os.getppid())
    if parent.stat().st_uid!=0 or '/system.slice/wahojobs-inventory.service' not in (parent/'cgroup').read_text():
        raise ValueError('native_supervisor_required')
    remaining=claim_dispatch(config['state_directory'],run_id,os.getppid(),time.monotonic(),phase)
    def expired(*_):raise TimeoutError('worker_execution_deadline_expired')
    signal.signal(signal.SIGALRM,expired)
    signal.setitimer(signal.ITIMER_REAL,remaining)
    return time.monotonic()+remaining


class NativeOperations:
    def __init__(self,config,policy):self.config=config;self.policy=policy
    def preflight(self):verify_runtime(self.config)
    def recovery_preflight(self):verify_release_configuration(self.config,require_effective=False)
    def stop(self,remaining):
        bounded_process(['/usr/bin/systemctl','stop',daily.SERVICE],timeout=min(90,remaining))
        state=subprocess.check_output(['/usr/bin/systemctl','show',daily.SERVICE,'--property=ActiveState,MainPID'],timeout=10,text=True)
        if dict(line.split('=',1) for line in state.splitlines())!={'ActiveState':'inactive','MainPID':'0'}:
            raise ValueError('beta_not_stopped')
    def phase(self,run_id,phase,deadline):
        target=Path(self.config['state_directory'])/'runs'/run_id/'run.json'
        receipt=daily.read_json(target)
        receipt['active_phase']=dict(name=phase,deadline=deadline)
        daily.write_json(target,receipt)
        bounded_process([sys.executable,'-B',str(Path(__file__).resolve()),'worker','--policy',str(self.policy),
            '--run-id',run_id,'--phase',phase],timeout=deadline-time.monotonic(),user='wahojobs-beta')

    def collect(self,run_id,remaining):
        deadline=time.monotonic()+remaining
        self.phase(run_id,'prepare',min(deadline,time.monotonic()+20))
        target=Path(self.config['state_directory'])/'runs'/run_id
        schedule=daily.read_json(target/'coverage-plan.json')
        if not schedule or set(schedule)!=set(daily.SOURCES):raise ValueError('coverage_plan_required')
        for source in daily.SOURCES:
            if schedule[source]['state']!='due':continue
            cap=daily.source_settings(self.config)[source]['seconds_max']
            try:self.phase(run_id,'collect-'+source,min(deadline-daily.PUBLICATION_SECONDS,time.monotonic()+cap))
            except InterruptedError:
                raise  # SIGTERM cancels the whole run; restore without more dispatch.
            except Exception as error:
                # Child is reaped before another source obtains the lifetime lease.
                daily.write_json(target/(source+'-failure.json'),dict(error_type=type(error).__name__))
        from wahojobs.crawler import staged_observation as staged
        available=[]
        for source in daily.SOURCES:
            if schedule[source]['state']!='due':continue
            try:staged.load(target,source,run_id=run_id,code_commit=self.config['code_commit'],journal_root=self.config['journal'])
            except (OSError,ValueError,KeyError,TypeError):continue
            available.append(source)
        daily.write_json(target/'publication-sources.json',available)
        return bool(available)

    def publish(self,run_id,remaining):
        deadline=time.monotonic()+remaining
        self.phase(run_id,'backup',min(deadline,time.monotonic()+60))
        target=Path(self.config['state_directory'])/'runs'/run_id
        sources=daily.read_json(target/'publication-sources.json',[])
        for index,source in enumerate(sources):
            # A failed publisher gets only its share of the remaining interval.
            # Unused shares remain available to subsequent sources.
            current=time.monotonic()
            source_deadline=current+max(0,(deadline-30-current)/(len(sources)-index))
            try:self.phase(run_id,'publish-'+source,source_deadline)
            except InterruptedError:raise
            except Exception as error:
                daily.write_json(target/(source+'-failure.json'),dict(error_type=type(error).__name__))
        self.phase(run_id,'finish',min(deadline,time.monotonic()+30))
    def restore(self,remaining):
        started=time.monotonic()
        bounded_process(['/usr/bin/systemctl','start',daily.SERVICE],timeout=min(75,remaining))
        bounded_process([sys.executable,'-B','scripts/private_beta_health.py','--config','/run/wahojobs-beta/runtime.json'],
            timeout=max(.01,min(35,remaining-(time.monotonic()-started))),user='wahojobs-beta')
    def ready(self):
        bounded_process([sys.executable,'-B','scripts/private_beta_health.py','--config','/run/wahojobs-beta/runtime.json'],
            timeout=25,user='wahojobs-beta')


def supervise(config,policy,trigger,*,operations=None):
    operations=operations or NativeOperations(config,policy)
    directory=Path(config['state_directory'])
    with operation_gate(config['database'],require_existing=isinstance(operations,NativeOperations)):
        operations.preflight()
        trigger_evidence=native_trigger() if trigger=='auto' else dict(trigger=trigger,provenance='explicit_runner_argument')
        trigger=trigger_evidence['trigger']
        receipt=daily.reserve_run(directory,daily.now(),daily.parse(config['first_run_at']),trigger)
        if receipt is None:return {'outcome':'already_consumed_or_not_due'}
        receipt['trigger_evidence']=trigger_evidence
        target=directory/'runs'/receipt['run_id']/'run.json'
        if receipt['outcome']=='missed_window':
            daily.write_json(target,receipt)
            return receipt
        # Worker owns its output directory; the root supervisor's policy stays root-owned.
        if os.name=='posix' and operations.__class__ is NativeOperations:
            import pwd
            account=pwd.getpwnam('wahojobs-beta')
            os.chown(target.parent,account.pw_uid,account.pw_gid)
        start=time.monotonic();deadline=start+daily.execution_seconds(config)
        receipt.update(outcome='running',normal_service_resumed=True,supervisor_pid=os.getpid(),execution_deadline_monotonic=deadline)
        daily.write_json(target,receipt)
        maintenance_start=None
        try:
            available=operations.collect(receipt['run_id'],deadline-time.monotonic())
            receipt['collection_finished_at']=daily.stamp(daily.now())
            if available is False:raise ValueError('no_completed_observations_to_publish')
            publication_deadline=min(deadline,time.monotonic()+daily.PUBLICATION_SECONDS)
            if publication_deadline<=time.monotonic():raise TimeoutError('publication_deadline_expired')
            # The online phase never creates a maintenance marker or stops beta.
            maintenance_start=time.monotonic()
            receipt.update(maintenance_started_at=daily.stamp(daily.now()),normal_service_resumed=False,
                publication_deadline_monotonic=publication_deadline)
            daily.write_json(target,receipt)
            operations.stop(publication_deadline-time.monotonic())
            operations.publish(receipt['run_id'],publication_deadline-time.monotonic())
            worker=daily.read_json(target.parent/'worker.json')
            summaries={source:daily.read_json(target.parent/(source+'.json')) for source in daily.SOURCES}
            receipt['sources']=summaries
            if not worker or worker.get('protected_domains_unchanged') is not True:
                raise ValueError('worker_receipt_missing')
            enabled=[s for name,s in summaries.items() if daily.source_settings(config)[name]['enabled']]
            receipt['outcome']='complete_with_coverage_gaps' if all(s and s['qualifying_observation'] for s in enabled) else 'partial_or_failed'
        except BaseException as error:
            receipt.update(outcome='failed',error_type=type(error).__name__)
        finally:
            # Reporting and alert delivery happen only after this recovery block.
            recovery_started=time.monotonic()
            if maintenance_start is not None:
                receipt['recovery_started_at']=daily.stamp(daily.now())
                with suppress(OSError):daily.write_json(target,receipt)
                try:
                    operations.restore(daily.RECOVERY_SECONDS)
                    receipt.update(normal_service_resumed=True,maintenance_finished_at=daily.stamp(daily.now()))
                except BaseException as error:
                    receipt.update(outcome='recovery_failed',recovery_error_type=type(error).__name__)
            receipt.update(ended_at=daily.stamp(daily.now()),total_runtime_seconds=round(time.monotonic()-start,3),
                maintenance_seconds=round(time.monotonic()-maintenance_start,3) if maintenance_start is not None else 0,
                recovery_seconds=round(time.monotonic()-recovery_started,3) if maintenance_start is not None else 0)
            for source in receipt.get('sources',{}).values():
                if source:source.update(run_id=receipt['run_id'],trigger=receipt['trigger'],maintenance_seconds=receipt['maintenance_seconds'])
            with suppress(OSError):daily.write_json(target,receipt)
        # Reading retained journals and preparing reporting must not extend the
        # unavailable interval. A failed worker still gets explicit source rows.
        if receipt['normal_service_resumed']:
            with suppress(OSError,ValueError):
                daily.finish_run_sources(config,receipt)
                daily.write_json(target,receipt)
        return receipt


def recover(config,policy):
    """systemd ExecStopPost/boot fallback after a killed supervisor; no collection."""
    directory=Path(config['state_directory'])
    with operation_gate(config['database'],require_existing=True):
        operations=NativeOperations(config,policy)
        operations.recovery_preflight()
        def finish_online():
            for path in sorted((directory/'runs').glob('*/run.json')):
                interrupted=daily.read_json(path)
                if interrupted.get('outcome') not in ('running','reserved') or interrupted.get('maintenance_started_at'):continue
                # Reporting follows any necessary restoration, even if its
                # persistence fails or an old collection journal is large.
                interrupted.update(outcome='interrupted',normal_service_resumed=True,
                    ended_at=daily.stamp(daily.now()),maintenance_seconds=0,recovery_seconds=0)
                daily.write_json(path,interrupted)
                with suppress(OSError,ValueError):
                    daily.finish_run_sources(config,interrupted)
                    daily.write_json(path,interrupted)
        pending=[p for p in sorted((directory/'runs').glob('*/run.json'))
                 if (r:=daily.read_json(p)).get('maintenance_started_at') and not r.get('normal_service_resumed')]
        if not pending:
            finish_online();return
        p=pending[-1];receipt=daily.read_json(p)
        # The parent cgroup is terminated by systemd before ExecStopPost.
        recovery_start=receipt.get('recovery_started_at')
        remaining=min(daily.RECOVERY_SECONDS, daily.PUBLICATION_SECONDS+daily.RECOVERY_SECONDS-(daily.now()-daily.parse(receipt['maintenance_started_at'])).total_seconds())
        if recovery_start:remaining=min(remaining,daily.RECOVERY_SECONDS-(daily.now()-daily.parse(recovery_start)).total_seconds())
        if remaining<=0:
            # A reboot can already have restored the enabled beta service. Observe
            # that recovery without opening another maintenance interval or retry.
            operations.ready()
            receipt.update(outcome='interrupted',normal_service_resumed=True,
                recovery_observed_late_at=daily.stamp(daily.now()),maintenance_seconds=None,
                maintenance_duration_status='unknown_after_restart')
            daily.write_json(p,receipt)
            with suppress(OSError,ValueError):
                daily.finish_run_sources(config,receipt)
                daily.write_json(p,receipt)
            finish_online()
            return
        receipt['recovery_started_at']=recovery_start or daily.stamp(daily.now())
        with suppress(OSError):daily.write_json(p,receipt)
        operations.restore(remaining)
        receipt.update(outcome='interrupted',normal_service_resumed=True,ended_at=daily.stamp(daily.now()),
            maintenance_finished_at=daily.stamp(daily.now()),
            maintenance_seconds=(daily.now()-daily.parse(receipt['maintenance_started_at'])).total_seconds())
        daily.write_json(p,receipt)
        with suppress(OSError,ValueError):
            daily.finish_run_sources(config,receipt)
            daily.write_json(p,receipt)
        finish_online()


def deliver(config,state):
    """One configurable boundary: reviewed executable receives event JSON on stdin.

    Mark uncertainty before dispatch. No retry after an ambiguous delivery; no
    WorkOS/email-account reuse. The receiver must deduplicate the event ID.
    """
    delivery=config['alert_delivery'];path=Path(config['state_directory'])/'health.json'
    pending=[event for event in state['events'] if event['delivery']=='pending']
    if not pending:return
    for event in pending:event['delivery']='attempted'
    daily.write_json(path,state)
    packet=dict(recipient=delivery['recipient'],events=pending,application='wahojobs-beta',version=2)
    try:
        subprocess.run(delivery['command'],input=json.dumps(packet).encode(),stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,timeout=15,check=True)
        status='accepted_by_adapter'
    except (OSError,subprocess.SubprocessError):status='failed_or_uncertain'
    for event in pending:event['delivery']=status
    daily.write_json(path,state)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('run','worker','recover','health','report'))
    parser.add_argument('--policy',type=Path,required=True)
    parser.add_argument('--trigger',choices=('auto','timer','manual','restart'),default='auto')
    parser.add_argument('--phase',choices=('prepare','backup','finish',*('collect-'+s for s in daily.SOURCES),*('publish-'+s for s in daily.SOURCES)));parser.add_argument('--run-id');parser.add_argument('--deliver',action='store_true')
    args=parser.parse_args(argv)
    config=private_policy(args.policy)
    daily.validate_policy(config,activation=args.command in ('run','worker') or args.deliver)
    os.umask(0o077)
    if args.command=='worker':
        import re
        if not args.run_id or not re.fullmatch(r'\d{8}T060000Z',args.run_id):raise ValueError('run_identity_required')
        if args.phase is None:raise ValueError('worker_phase_required')
        deadline=claim_worker(config,args.run_id,args.phase)
        from wahojobs.crawler.local_inventory import request_deadline
        with request_deadline(deadline):daily.collect_phase(config,args.run_id,args.phase)
    elif args.command=='recover':recover(config,args.policy)
    elif args.command=='run':
        def interrupted(*_):raise InterruptedError('supervisor_terminated')
        signal.signal(signal.SIGTERM,interrupted)
        result=supervise(config,args.policy,args.trigger)
        print(json.dumps(result))
        return 0 if result['outcome'] in (*daily.SUCCESSFUL_RUN_OUTCOMES,'already_consumed_or_not_due') else 2
    elif args.command=='health':
        with operation_gate(str(Path(config['state_directory'])/'health')):
            result=daily.health(config)
            if args.deliver:deliver(config,result)
            print(json.dumps(result))
    else:
        print(json.dumps(dict(health=daily.read_json(Path(config['state_directory'])/'health.json'),
            runs=[daily.read_json(p) for p in sorted((Path(config['state_directory'])/'runs').glob('*/run.json'))[-7:]])))
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print('daily_inventory_failed: '+type(error).__name__,file=sys.stderr);raise SystemExit(2)
