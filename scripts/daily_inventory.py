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


def _live_group(group):
    """Linux process group, excluding zombies which cannot retain file locks."""
    if not Path('/proc').is_dir():return []
    result=[]
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():continue
        try:fields=(proc/'stat').read_text().rsplit(')',1)[1].split()
        except (FileNotFoundError,ProcessLookupError):continue
        if int(fields[2])==group and fields[0]!='Z':result.append(int(proc.name))
    return result


def _stop_group(proc,grace=3):
    with suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGTERM)
    until=time.monotonic()+grace
    while time.monotonic()<until:
        proc.poll()
        if not _live_group(proc.pid):break
        time.sleep(.05)
    if _live_group(proc.pid):
        with suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGKILL)
    proc.wait(timeout=5)
    until=time.monotonic()+2
    while _live_group(proc.pid) and time.monotonic()<until:time.sleep(.05)
    if _live_group(proc.pid):raise RuntimeError('worker_group_not_quiescent')


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
        if _live_group(proc.pid):raise RuntimeError('worker_left_live_child')
    except BaseException:
        _stop_group(proc)
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
    def expired(*_):
        signal.setitimer(signal.ITIMER_REAL,0)
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        raise TimeoutError('worker_execution_deadline_expired')
    signal.signal(signal.SIGALRM,expired)
    signal.signal(signal.SIGTERM,expired)
    # Leave rollback/close time before the parent's hard deadline.
    useful=max(.01,remaining-min(3,remaining/4))
    signal.setitimer(signal.ITIMER_REAL,useful)
    return time.monotonic()+useful


class NativeOperations:
    def __init__(self,config,policy):self.config=config;self.policy=policy
    def preflight(self):
        verify_runtime(self.config)
        from wahojobs.diagnostic_archive import archive_preflight
        try:
            archive_preflight('/var/log/wahojobs-beta')
        except (OSError, ValueError) as error:
            # Request diagnostics are optional to collection. The strict
            # deployment preflight still blocks a planned maintenance entry.
            print('diagnostic_archive_warning:' + type(error).__name__, file=sys.stderr, flush=True)
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
        started=time.monotonic()
        try:
            bounded_process([sys.executable,'-B',str(Path(__file__).resolve()),'worker','--policy',str(self.policy),
                '--run-id',run_id,'--phase',phase],timeout=deadline-time.monotonic(),user='wahojobs-beta')
        finally:
            receipt=daily.read_json(target)
            receipt.setdefault('phase_timings_seconds',{})[phase]=round(time.monotonic()-started,3)
            daily.write_json(target,receipt)

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
        available=[];weights={}
        for source in daily.SOURCES:
            if schedule[source]['state']!='due':continue
            try:observation,_=staged.load(target,source,run_id=run_id,code_commit=self.config['code_commit'],journal_root=self.config['journal'])
            except (OSError,ValueError,KeyError,TypeError):continue
            available.append(source)
            weights[source]=min(20_000,max(1,len(observation.result.jobs),schedule[source].get('stored_records',0)))
        daily.write_json(target/'publication-sources.json',available)
        daily.write_json(target/'publication-weights.json',weights)
        return bool(available)

    def publish(self,run_id,remaining):
        deadline=time.monotonic()+remaining
        self.phase(run_id,'backup',min(deadline,time.monotonic()+60))
        target=Path(self.config['state_directory'])/'runs'/run_id
        sources=daily.read_json(target/'publication-sources.json',[])
        weights=daily.read_json(target/'publication-weights.json',{s:1 for s in sources})
        if set(weights)!=set(sources) or any(type(n) is not int or not 1<=n<=20_000 for n in weights.values()):
            raise ValueError('bounded_publication_weights_required')
        for index,source in enumerate(sources):
            # Full-size native evidence requires more time for 5,624 Alignerr
            # variants than for a small source. Reserve each sibling's minimum,
            # then allocate by existing/observed records inside the SAME cap.
            current=time.monotonic()
            remaining_sources=sources[index:];budget=max(0,deadline-30-current)
            minimum=min(5,budget/len(remaining_sources))
            extra=budget-minimum*len(remaining_sources)
            source_deadline=current+minimum+extra*weights[source]/sum(weights[s] for s in remaining_sources)
            try:self.phase(run_id,'publish-'+source,source_deadline)
            except InterruptedError:raise
            except Exception as error:
                daily.write_json(target/(source+'-failure.json'),dict(error_type=type(error).__name__))
                # Never dispatch another publisher into uncertain storage.
                if isinstance(error,(TimeoutError,subprocess.TimeoutExpired)) or any(
                        os.path.lexists(str(self.config['database'])+suffix) for suffix in ('-journal','-wal','-shm')):
                    raise
        self.phase(run_id,'finish',min(deadline,time.monotonic()+30))
    def restore(self,remaining):
        started=time.monotonic()
        database=Path(self.config['database'])
        # A dedicated process bounds validation/recovery independently of the
        # publication deadline. It acquires lifetime ownership before opening.
        bounded_process([sys.executable,'-B',str(Path(__file__).resolve()),'repair-storage',
            '--policy',str(self.policy)],timeout=max(.01,remaining-40))
        bounded_process(['/usr/bin/systemctl','start',daily.SERVICE],timeout=max(.01,min(75,remaining-(time.monotonic()-started))))
        bounded_process([sys.executable,'-B','scripts/private_beta_health.py','--config','/run/wahojobs-beta/runtime.json'],
            timeout=max(.01,min(35,remaining-(time.monotonic()-started))),user='wahojobs-beta')
    def ready(self):
        bounded_process([sys.executable,'-B','scripts/private_beta_health.py','--config','/run/wahojobs-beta/runtime.json'],
            timeout=25,user='wahojobs-beta')


def supervise(config,policy,trigger,*,operations=None,availability_sources=None):
    operations=operations or NativeOperations(config,policy)
    directory=Path(config['state_directory'])
    with operation_gate(config['database'],require_existing=isinstance(operations,NativeOperations)):
        if (directory/'publication-hold.json').exists():raise ValueError('publication_requires_operator_clearance')
        operations.preflight()
        trigger_evidence=native_trigger() if trigger=='auto' else dict(trigger=trigger,provenance='explicit_runner_argument')
        trigger=trigger_evidence['trigger']
        if availability_sources is not None:
            from wahojobs.availability_recovery import reserve
            operations.ready()
            receipt=reserve(config,daily.now(),availability_sources)
        else:receipt=daily.reserve_run(directory,daily.now(),daily.parse(config['first_run_at']),trigger)
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
        allowance=daily.execution_seconds(config)
        if availability_sources is not None:
            allowance=min(allowance,20+daily.PUBLICATION_SECONDS+sum(daily.source_settings(config)[s]['seconds_max'] for s in availability_sources))
        start=time.monotonic();deadline=start+allowance
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
            measured=daily.read_json(target,{})
            if measured.get('phase_timings_seconds'):
                receipt['phase_timings_seconds']=measured['phase_timings_seconds']
            maintenance_start=time.monotonic()
            receipt.update(maintenance_started_at=daily.stamp(daily.now()),normal_service_resumed=False,
                publication_deadline_monotonic=publication_deadline)
            daily.write_json(target,receipt)
            stop_started=time.monotonic()
            try:
                operations.stop(publication_deadline-time.monotonic())
            finally:
                receipt['stop_seconds']=round(time.monotonic()-stop_started,3)
            operations.publish(receipt['run_id'],publication_deadline-time.monotonic())
            worker=daily.read_json(target.parent/'worker.json')
            summaries={source:daily.read_json(target.parent/(source+'.json')) for source in availability_sources or daily.SOURCES}
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
                measured=daily.read_json(target,{})
                if measured.get('phase_timings_seconds'):
                    receipt['phase_timings_seconds']=measured['phase_timings_seconds']
                receipt['recovery_started_at']=daily.stamp(daily.now())
                with suppress(OSError):daily.write_json(target,receipt)
                try:
                    operations.restore(daily.RECOVERY_SECONDS)
                    receipt['restore_and_readiness_seconds']=round(time.monotonic()-recovery_started,3)
                    receipt.update(normal_service_resumed=True,maintenance_finished_at=daily.stamp(daily.now()))
                except BaseException as error:
                    receipt.update(outcome='recovery_failed',recovery_error_type=type(error).__name__)
                    suspend_publication(config,receipt)
            measured=daily.read_json(target,{})
            if measured.get('phase_timings_seconds'):
                receipt['phase_timings_seconds']=measured['phase_timings_seconds']
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


def pending_recovery(directory):
    from hashlib import sha256
    result=[]
    for path in sorted((directory/'runs').glob('*/run.json')):
        receipt=daily.read_json(path)
        if not receipt.get('maintenance_started_at') or receipt.get('normal_service_resumed'):continue
        recovery=daily.read_json(path.parent/'application-recovery.json',{})
        if recovery.get('application_ready') is True and recovery.get('run_sha256')==sha256(path.read_bytes()).hexdigest():continue
        result.append(path)
    return result


def record_recovery(path,receipt):
    from hashlib import sha256
    # Terminal outcome and outage measurements remain byte-for-byte evidence.
    if receipt.get('outcome') in ('running','reserved'):
        receipt.update(outcome='interrupted',normal_service_resumed=True,ended_at=daily.stamp(daily.now()),
            maintenance_seconds=None,recovery_seconds=None)
        daily.write_json(path,receipt)
    daily.write_json(path.parent/'application-recovery.json',dict(application_ready=True,
        run_sha256=sha256(path.read_bytes()).hexdigest(),observed_at=daily.stamp(daily.now())))


def recover(config,policy):
    """Fresh bounded recovery window, independent of old publication deadlines."""
    directory=Path(config['state_directory'])
    recovery_deadline=time.monotonic()+daily.RECOVERY_SECONDS-10
    with operation_gate(config['database'],require_existing=True):
        operations=NativeOperations(config,policy)
        operations.recovery_preflight()
        def finish_online():
            for path in sorted((directory/'runs').glob('*/run.json')):
                interrupted=daily.read_json(path)
                if interrupted.get('outcome') not in ('running','reserved') or interrupted.get('maintenance_started_at'):continue
                interrupted.update(outcome='interrupted',normal_service_resumed=True,
                    ended_at=daily.stamp(daily.now()),maintenance_seconds=0,recovery_seconds=0)
                daily.write_json(path,interrupted)
                with suppress(OSError,ValueError):
                    daily.finish_run_sources(config,interrupted)
                    daily.write_json(path,interrupted)
        pending=pending_recovery(directory)
        if not pending:
            finish_online();return
        p=pending[-1];receipt=daily.read_json(p)
        old=(daily.now()-daily.parse(receipt['maintenance_started_at'])).total_seconds()>daily.PUBLICATION_SECONDS+daily.RECOVERY_SECONDS
        try:
            online=False
            if old:
                try:operations.ready();online=True
                except Exception:pass
            if not online:
                remaining=recovery_deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('recovery_deadline_expired')
                operations.restore(remaining)
        except BaseException:
            suspend_publication(config,receipt)
            raise
        record_recovery(p,receipt)
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
    packet=dict(recipient=delivery['recipient'],events=pending,context=state.get('context',{}),
                application='wahojobs-beta',version=2)
    try:
        subprocess.run(delivery['command'],input=json.dumps(packet).encode(),stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,timeout=15,check=True)
        status='accepted_by_adapter'
    except (OSError,subprocess.SubprocessError):status='failed_or_uncertain'
    for event in pending:event['delivery']=status
    daily.write_json(path,state)


def suspend_publication(config,receipt):
    try:
        try:
            daily.record_availability_failure(config,reason='publication_recovery_failed',unit='wahojobs-inventory.service')
        finally:
            daily.write_json(Path(config['state_directory'])/'publication-hold.json',dict(
                reason='unrecovered_application_failure',run_id=receipt.get('run_id'),at=daily.stamp(daily.now())))
    finally:
        if os.name=='posix' and os.geteuid()==0 and config.get('database')==daily.DATABASE:
            try:
                subprocess.run(['systemctl','disable','--now','wahojobs-inventory.timer'],
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15,check=True)
            finally:
                # A separate invocation cannot be swallowed by an active hourly
                # health scan. Queue only: delivery never waits inside recovery.
                subprocess.run(['systemctl','start','--no-block','wahojobs-inventory-urgent.service'],
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,check=True)


def verify_recovery_parent(config):
    # Only the verified root supervisor may lend its operation gate to this
    # short-lived child. Standalone repair is deliberately unavailable.
    parent=Path('/proc')/str(os.getppid())
    command=(parent/'cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
    if (parent.stat().st_uid!=0 or (parent/'cwd').resolve()!=ROOT
            or len(command)<4 or command[1]!='-B' or command[3] not in ('run','recover')
            or command[2] not in ('scripts/daily_inventory.py',str(ROOT/'scripts/daily_inventory.py'))):
        raise ValueError('recovery_supervisor_required')
    lock_path=str(config['database'])+'.wahojobs-maintenance.lock'
    if not any(os.readlink(fd)==lock_path for fd in (parent/'fd').iterdir()):
        raise ValueError('parent_operation_gate_required')
    # A descriptor is not proof of ownership: prove the native gate is locked.
    try:
        with operation_gate(config['database'],require_existing=True):pass
    except BlockingIOError:pass
    else:raise ValueError('parent_operation_gate_not_locked')


def repair_storage(config):
    verify_release_configuration(config,require_effective=False)
    verify_recovery_parent(config)
    state=subprocess.check_output(['systemctl','show',daily.SERVICE,'-p','MainPID','-p','ControlPID'],text=True,timeout=5)
    if any(row.split('=')[1]!='0' for row in state.splitlines()):raise ValueError('application_still_active')
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name)==os.getpid():continue
        try:
            for fd in (proc/'fd').iterdir():
                try:target=os.readlink(fd)
                except FileNotFoundError:continue
                if target.startswith(config['database']) and target not in (
                        config['database']+'.wahojobs-maintenance.lock',config['database']+'.wahojobs-lifetime.lock'):
                    raise ValueError('database_process_still_active')
        except (FileNotFoundError,ProcessLookupError):continue
    from wahojobs.sqlite_recovery import recover_storage
    from wahojobs.beta_recovery import _check_sqlite
    directory=Path(config['state_directory'])
    pending=pending_recovery(directory)
    if not pending:raise ValueError('interrupted_run_required')
    baseline=daily.read_json(pending[-1].parent/'backup.json')
    interrupted=any(os.path.lexists(config['database']+suffix) for suffix in ('-journal','-wal','-shm'))
    if interrupted and (not baseline or not baseline.get('verified')):
        raise ValueError('verified_prepublication_baseline_required')
    # The storage owner, never root, creates/reopens SQLite recovery files.
    import pwd
    account=pwd.getpwnam('wahojobs-beta')
    os.setgroups([]);os.setgid(account.pw_gid);os.setuid(account.pw_uid)
    if not baseline or not baseline.get('verified'):
        # Stopped before backup/publication: strict validation can restore clean
        # storage without pretending an absent backup was completed.
        baseline={'protected_domains':None}
    def validate(path):
        import sqlite3
        from contextlib import closing
        _check_sqlite(path,product=True,read_only=False)
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
            if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise ValueError('full_integrity_failed')
    result=recover_storage(Path(config['database']),directory/'native-recovery',
        validate=validate,
        protected=daily.protected_domains,expected_protected=baseline['protected_domains'])
    daily.write_json(pending[-1].parent/'storage-recovery.json',result)


def application_ready():
    try:
        bounded_process([sys.executable,'-B','scripts/private_beta_health.py','--config','/run/wahojobs-beta/runtime.json'],timeout=25)
        return True
    except Exception:return False


def collection_observation(config):
    """Observe effective schedule and known unit gates without changing them."""
    def output(*args):
        return subprocess.check_output(['systemctl',*args],text=True,timeout=5)
    checked=daily.stamp(daily.now())
    try:
        timer=dict(line.split('=',1) for line in output('show','wahojobs-inventory.timer',
            '-p','ActiveState','-p','UnitFileState','-p','NextElapseUSecRealtime').splitlines())
        unit=output('cat','wahojobs-inventory.service')
        reload_needed=output('show','wahojobs-inventory.service','-p','NeedDaemonReload','--value').strip()!='no'
        conditions=[];unsupported=False
        for line in unit.splitlines():
            line=line.strip()
            if line.startswith('ConditionPathExists='):
                value=line.split('=',1)[1]
                if not value:conditions=[]
                else:conditions.append(value)
            elif line.startswith(('Condition','Assert','ExecCondition=')):unsupported=True
        blocked=unsupported or reload_needed
        for value in conditions:
            negate=value.startswith('!');path=value[1:] if negate else value
            if not path.startswith('/') or any(c in path for c in ('%','|','"')):blocked=True
            elif Path(path).exists()==negate:blocked=True
        hold=(Path(config['state_directory'])/'publication-hold.json').exists()
        if hold:state,reason='suspended','publication_hold'
        elif not config.get('enabled'):state,reason='disabled','policy_disabled'
        elif blocked:state,reason='blocked','effective_unit_condition'
        elif timer.get('ActiveState')!='active':state,reason='suspended','timer_not_active'
        elif timer.get('NextElapseUSecRealtime') in (None,'','n/a'):state,reason='blocked','next_execution_unavailable'
        else:state,reason='enabled','schedule_and_publication_gates_verified'
        return dict(state=state,reason=reason,checked_at=checked,
            next_execution=timer.get('NextElapseUSecRealtime'),timer_state=timer.get('ActiveState'))
    except (OSError,subprocess.SubprocessError,ValueError):
        return dict(state='unknown',reason='effective_schedule_unavailable',checked_at=checked,next_execution=None)


def operating_snapshot(config):
    """Bound observations in one interval; never silently mix a changed gate."""
    started=daily.stamp(daily.now());before=collection_observation(config)
    available=application_ready();checked=daily.stamp(daily.now())
    after=collection_observation(config)
    coherent={k:v for k,v in before.items() if k!='checked_at'}=={k:v for k,v in after.items() if k!='checked_at'}
    if not coherent:after.update(state='unknown',reason='collection_changed_during_readiness_observation')
    elif available is False and after['state']=='enabled':after.update(state='blocked',reason='application_unavailable')
    return dict(started_at=started,completed_at=daily.stamp(daily.now()),coherent=coherent,
        application=dict(available=available,checked_at=checked),collection=after)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('run','worker','recover','repair-storage','health','report','failure-signal'))
    parser.add_argument('--policy',type=Path,required=True)
    parser.add_argument('--trigger',choices=('auto','timer','manual','restart'),default='auto')
    parser.add_argument('--phase',choices=('prepare','backup','finish',*('collect-'+s for s in daily.SOURCES),*('publish-'+s for s in daily.SOURCES)));parser.add_argument('--run-id');parser.add_argument('--deliver',action='store_true');parser.add_argument('--urgent',action='store_true')
    parser.add_argument('--availability-recovery',action='store_true')
    parser.add_argument('--source',action='append',choices=('alignerr','mercor'))
    args=parser.parse_args(argv)
    if (args.availability_recovery or args.source) and (args.command!='run' or not args.availability_recovery or not args.source):
        raise ValueError('explicit_targeted_run_required')
    config=private_policy(args.policy)
    daily.validate_policy(config,activation=args.command in ('run','worker') or args.deliver)
    os.umask(0o077)
    if args.command=='failure-signal':
        if '/system.slice/wahojobs-beta.service' not in Path('/proc/self/cgroup').read_text():
            raise ValueError('native_application_failure_signal_required')
        result=os.environ.get('SERVICE_RESULT')
        if result and result!='success':daily.record_availability_failure(config,reason='application_service_'+result,unit=daily.SERVICE)
    elif args.command=='worker':
        import re
        if not args.run_id or not re.fullmatch(r'\d{8}T060000Z(?:-availability)?',args.run_id):raise ValueError('run_identity_required')
        if args.phase is None:raise ValueError('worker_phase_required')
        deadline=claim_worker(config,args.run_id,args.phase)
        from wahojobs.crawler.local_inventory import request_deadline
        with request_deadline(deadline):daily.collect_phase(config,args.run_id,args.phase)
    elif args.command=='repair-storage':repair_storage(config)
    elif args.command=='recover':recover(config,args.policy)
    elif args.command=='run':
        def interrupted(*_):raise InterruptedError('supervisor_terminated')
        signal.signal(signal.SIGTERM,interrupted)
        result=supervise(config,args.policy,args.trigger,availability_sources=args.source if args.availability_recovery else None)
        print(json.dumps(result))
        return 0 if result['outcome'] in (*daily.SUCCESSFUL_RUN_OUTCOMES,'already_consumed_or_not_due') else 2
    elif args.command=='health':
        with operation_gate(str(Path(config['state_directory'])/'health')):
            # Claim the wake-up before checking: a concurrent new failure writes
            # another marker, and the path unit reruns after this service exits.
            if args.urgent:
                with suppress(FileNotFoundError):(Path(config['state_directory'])/'urgent-health-pending.json').unlink()
            operating=operating_snapshot(config)
            result=daily.health(config,application_ready=operating['application']['available'],operating=operating,urgent=args.urgent)
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
