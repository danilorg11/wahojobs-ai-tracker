"""Real SQLite crash/cancellation tests on disposable, labelled stress data."""
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from scripts import daily_inventory as cli
from wahojobs import sqlite_recovery as recovery, daily_inventory as daily
from wahojobs.workos_authkit_staging import _require_no_sqlite_sidecars
from wahojobs.maintenance_gate import operation_gate

WRITER = '''
import sqlite3,sys,time,signal
from pathlib import Path
db=sqlite3.connect(sys.argv[1]); db.execute('PRAGMA cache_size=10')
if sys.argv[3]=='graceful':
 def cancel(*args):raise TimeoutError('cancelled real large publication')
 signal.signal(signal.SIGTERM,cancel)
db.execute("UPDATE sources SET value='committed' WHERE name='A'");db.commit()
try:
 db.execute('BEGIN IMMEDIATE')
 db.execute("UPDATE sources SET value='uncommitted' WHERE name='B'")
 db.execute("UPDATE candidate SET value='uncommitted'")
 db.execute("UPDATE overrides SET value='uncommitted'")
 db.execute('UPDATE payload SET value=zeroblob(4096)')
 Path(sys.argv[2]).write_text('transaction-spilled')
 while True:time.sleep(.02)
except BaseException:
 db.rollback()
finally:db.close()
'''


class NativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.db=self.root/'product.sqlite3'
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript("CREATE TABLE sources(name PRIMARY KEY,value); INSERT INTO sources VALUES('A','old'),('B','old');"
                "CREATE TABLE candidate(value);INSERT INTO candidate VALUES('profile-history');"
                "CREATE TABLE overrides(value);INSERT INTO overrides VALUES('manual-pay-evidence');"
                "CREATE TABLE payload(id INTEGER PRIMARY KEY,value BLOB);")
            db.executemany('INSERT INTO payload(value) VALUES(?)',[(b'x'*4096,)]*500);db.commit()
        self.expected=self.protected(self.db)
    def protected(self,path):
        with closing(sqlite3.connect(path)) as db:
            return {table:db.execute('SELECT * FROM '+table).fetchall() for table in ('candidate','overrides')}
    def validate(self,path):
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(),[('ok',)])
    def launch(self,mode='kill'):
        marker=self.root/'ready'
        proc=subprocess.Popen([sys.executable,'-B','-c',WRITER,str(self.db),str(marker),mode],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.addCleanup(lambda:proc.kill() if proc.poll() is None else None)
        until=time.monotonic()+10
        while not marker.exists() and time.monotonic()<until:
            if proc.poll() is not None:self.fail('writer exited before real SQLite spill')
            time.sleep(.02)
        self.assertTrue(marker.exists());return proc
    def check_recovered(self):
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(dict(db.execute('SELECT * FROM sources')) ,{'A':'committed','B':'old'})
            self.assertEqual(db.execute('SELECT count(*) FROM payload WHERE value=?',(b'x'*4096,)).fetchone()[0],500)
        self.assertEqual(self.protected(self.db),self.expected)
        _require_no_sqlite_sidecars(self.db)
    def test_real_hot_journal_native_rollback_preserves_earlier_commit_and_candidate_data(self):
        proc=self.launch();proc.kill();proc.wait(timeout=5)
        side=Path(str(self.db)+'-journal')
        self.assertEqual(side.read_bytes()[:8],bytes.fromhex('d9d505f920a163d7'))
        with self.assertRaises(Exception):_require_no_sqlite_sidecars(self.db)
        recovery.native_finalize(self.db);self.check_recovered()
        recovery.native_finalize(self.db);self.check_recovered()
    @unittest.skipUnless(os.name=='posix','native beta uses Linux process groups')
    def test_managed_rehearsal_preserves_evidence_and_rejects_live_writer(self):
        proc=self.launch()
        with operation_gate(self.db),self.assertRaisesRegex(ValueError,'process_still_active'):
            recovery.recover_storage(self.db,self.root/'evidence',validate=self.validate,
                protected=self.protected,expected_protected=self.expected)
        proc.kill();proc.wait(timeout=5)
        original=Path(str(self.db)+'-journal').read_bytes()
        with operation_gate(self.db):
            result=recovery.recover_storage(self.db,self.root/'evidence',validate=self.validate,
                protected=self.protected,expected_protected=self.expected)
        self.assertTrue(result['recovered']);self.check_recovered()
        preserved=Path(result['evidence'])/'evidence'/Path(str(self.db)+'-journal').name
        self.assertEqual(preserved.read_bytes(),original)
        with operation_gate(self.db):
            repeated=recovery.recover_storage(self.db,self.root/'evidence',validate=self.validate,
                protected=self.protected,expected_protected=self.expected)
        self.assertFalse(repeated['recovered'])
    def test_native_zero_header_finalization_without_database_byte_change(self):
        # Genuine non-hot journal: no spill occurs before process interruption.
        code="import sqlite3,sys,time;from pathlib import Path;d=sqlite3.connect(sys.argv[1]);d.execute(\"UPDATE sources SET value='pending'\");Path(sys.argv[2]).touch();time.sleep(30)"
        marker=self.root/'cold';proc=subprocess.Popen([sys.executable,'-c',code,str(self.db),str(marker)])
        try:
            until=time.monotonic()+10
            while not marker.exists() and time.monotonic()<until:time.sleep(.02)
            self.assertTrue(marker.exists())
        finally:proc.kill();proc.wait(timeout=5)
        before=self.db.read_bytes();self.assertEqual(Path(str(self.db)+'-journal').read_bytes()[:8],b'\0'*8)
        recovery.native_finalize(self.db)
        self.assertEqual(self.db.read_bytes(),before);_require_no_sqlite_sidecars(self.db)
    @unittest.skipUnless(os.name=='posix','native beta uses Linux process groups')
    def test_timeout_during_large_transaction_gracefully_rolls_back(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            cli.bounded_process([sys.executable,'-B','-c',WRITER,str(self.db),str(self.root/'ready'),'graceful'],timeout=5)
        self.assertTrue((self.root/'ready').exists());self.check_recovered()
    @unittest.skipUnless(os.name=='posix','native beta uses Linux process groups')
    def test_child_cannot_outlive_completed_parent_into_recovery(self):
        marker=self.root/'child'
        child="import sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(__import__('os').getpid()));time.sleep(30)"
        parent="import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);\nwhile not Path(sys.argv[2]).exists():time.sleep(.01)"
        with self.assertRaisesRegex(RuntimeError,'live_child'):
            cli.bounded_process([sys.executable,'-c',parent,child,str(marker)],timeout=5)
        pid=int(marker.read_text());statfile=Path('/proc')/str(pid)/'stat'
        self.assertTrue(not statfile.exists() or statfile.read_text().rsplit(')',1)[1].split()[0]=='Z')
    @unittest.skipUnless(os.name=='posix','POSIX evidence ownership')
    def test_rehearsal_validation_failure_keeps_authoritative_pair_untouched(self):
        proc=self.launch();proc.kill();proc.wait(timeout=5)
        before=self.db.read_bytes();journal=Path(str(self.db)+'-journal').read_bytes()
        with operation_gate(self.db),self.assertRaisesRegex(ValueError,'validation_fixture'):
            recovery.recover_storage(self.db,self.root/'evidence',validate=Mock(side_effect=ValueError('validation_fixture')),
                protected=self.protected,expected_protected=self.expected)
        self.assertEqual(self.db.read_bytes(),before);self.assertEqual(Path(str(self.db)+'-journal').read_bytes(),journal)
    def test_failed_recovery_holds_future_publication_and_alert_needs_real_readiness(self):
        config=dict(state_directory=str(self.root/'state'),database=str(self.db),first_run_at='2026-09-25T06:00:00+00:00')
        cli.suspend_publication(config,{'run_id':'failed-fixture'})
        ops=Mock()
        with self.assertRaisesRegex(ValueError,'operator_clearance'):cli.supervise(config,'fixture','timer',operations=ops)
        ops.preflight.assert_not_called()
        with patch.object(daily,'_baseline_cohorts',return_value={}):
            failed=daily.health(config,application_ready=False)
            unknown=daily.health(config)
            ready=daily.health(config,application_ready=True)
        self.assertEqual(failed['active']['application:unavailable']['severity'],'critical')
        self.assertIn('application:unavailable',unknown['active'])
        self.assertNotIn('application:unavailable',ready['active'])
        self.assertEqual(sum(e['key']=='application:unavailable' and e['kind']=='recovered' for e in ready['events']),1)

    @unittest.skipUnless(os.name=='posix','native beta storage tests')
    def test_full_application_schema_and_protected_hashes_after_real_interruption(self):
        from tests.evidence_maintenance_support import new_inventory
        from wahojobs.beta_recovery import _check_sqlite
        path=new_inventory(self.root/'application')
        expected=daily.protected_domains(path)
        marker=self.root/'application-ready'
        code="import sqlite3,sys,time;from pathlib import Path;d=sqlite3.connect(sys.argv[1]);d.execute('PRAGMA cache_size=10');d.execute('BEGIN IMMEDIATE');d.execute('UPDATE companies SET name=name || hex(randomblob(50000))');Path(sys.argv[2]).touch();time.sleep(30)"
        proc=subprocess.Popen([sys.executable,'-c',code,str(path),str(marker)])
        try:
            until=time.monotonic()+10
            while not marker.exists() and time.monotonic()<until:time.sleep(.02)
            self.assertTrue(marker.exists())
        finally:proc.kill();proc.wait(timeout=5)
        with operation_gate(path):
            result=recovery.recover_storage(path,self.root/'app-evidence',
                validate=lambda p:_check_sqlite(p,product=True,read_only=False),
                protected=daily.protected_domains,expected_protected=expected)
        self.assertTrue(result['validated']);self.assertEqual(daily.protected_domains(path),expected)

    @unittest.skipUnless(os.name=='posix','native beta storage tests')
    def test_clean_storage_without_completed_backup_is_validated(self):
        with operation_gate(self.db):
            result=recovery.recover_storage(self.db,self.root/'unused-evidence',
                validate=self.validate,protected=self.protected,expected_protected=None)
        self.assertEqual(result,{'recovered':False,'validated':True})
        self.assertFalse((self.root/'unused-evidence').exists())

    @unittest.skipUnless(os.name=='posix','native beta storage tests')
    def test_live_drafts_connection_excludes_recovery(self):
        drafts=Path(str(self.db)+'.correction-drafts.sqlite3')
        with closing(sqlite3.connect(drafts)) as db:db.execute('CREATE TABLE draft(value)')
        marker=self.root/'draft-ready'
        code="import sqlite3,sys,time;from pathlib import Path;d=sqlite3.connect(sys.argv[1]);d.execute('BEGIN IMMEDIATE');d.execute(\"INSERT INTO draft VALUES('private fixture')\");Path(sys.argv[2]).touch();time.sleep(30)"
        proc=subprocess.Popen([sys.executable,'-c',code,str(drafts),str(marker)])
        try:
            until=time.monotonic()+10
            while not marker.exists() and time.monotonic()<until:time.sleep(.02)
            self.assertTrue(marker.exists())
            with operation_gate(self.db),self.assertRaisesRegex(ValueError,'process_still_active'):
                recovery.recover_storage(self.db,self.root/'draft-evidence',validate=self.validate,
                    protected=self.protected,expected_protected=self.expected)
        finally:proc.kill();proc.wait(timeout=5)

    @unittest.skipUnless(os.name=='posix','POSIX permission enforcement')
    def test_real_permission_failure_leaves_pair_untouched(self):
        proc=self.launch();proc.kill();proc.wait(timeout=5)
        before=self.db.read_bytes();side=Path(str(self.db)+'-journal');journal=side.read_bytes()
        self.db.chmod(0)
        kwargs={'user':65534,'group':65534,'extra_groups':[]} if os.geteuid()==0 else {}
        try:
            child=subprocess.run([sys.executable,'-c','from pathlib import Path;from wahojobs.sqlite_recovery import native_finalize;import sys;native_finalize(Path(sys.argv[1]))',str(self.db)],capture_output=True,timeout=10,**kwargs)
            self.assertNotEqual(child.returncode,0)
        finally:self.db.chmod(0o600)
        self.assertEqual(self.db.read_bytes(),before);self.assertEqual(side.read_bytes(),journal)

    @unittest.skipUnless(os.name=='posix' and hasattr(os,'geteuid') and os.geteuid()==0,'production multi-UID rehearsal runs as root on Linux')
    def test_database_owner_recovery_and_root_ownership_rejection(self):
        import pwd
        try:account=pwd.getpwnam('wahojobs-beta')
        except KeyError:self.skipTest('beta owner absent on this test host')
        proc=self.launch();proc.kill();proc.wait(timeout=5)
        for path in [self.root,*self.root.iterdir()]:os.chown(path,account.pw_uid,account.pw_gid)
        with self.assertRaises(Exception):
            recovery.recover_storage(self.db,self.root/'evidence',validate=self.validate,
                protected=self.protected,expected_protected=self.expected)
        code="from pathlib import Path;from wahojobs.sqlite_recovery import recover_storage;import sys;from wahojobs.maintenance_gate import operation_gate;p=Path(sys.argv[1]);\nwith operation_gate(p):\n r=recover_storage(p,p.parent/'evidence',validate=lambda p:None,protected=lambda p:True,expected_protected=True)\n assert r['validated']"
        child=subprocess.run([sys.executable,'-B','-c',code,str(self.db)],capture_output=True,timeout=30,
            user=account.pw_uid,group=account.pw_gid,extra_groups=[])
        self.assertEqual(child.returncode,0,child.stderr.decode());self.check_recovered()

    def test_terminal_failure_is_append_only_and_recovery_deadline_is_shared(self):
        state=self.root/'state';config=dict(state_directory=str(state),database=str(self.db))
        at=daily.parse('2026-09-25T06:00:00+00:00')
        receipt=daily.reserve_run(state,at,at,'timer')
        receipt.update(outcome='recovery_failed',maintenance_started_at=daily.stamp(at),
            normal_service_resumed=False,maintenance_seconds=179.353)
        path=state/'runs'/receipt['run_id']/'run.json';daily.write_json(path,receipt);original=path.read_bytes()
        with operation_gate(self.db):pass
        clock=[0.0]
        def preflight():clock[0]+=3
        def stopped(remaining):
            self.assertEqual(remaining,407)
            clock[0]+=2
            return False
        def unavailable(remaining):
            self.assertEqual(remaining,405)
            clock[0]+=20
            raise RuntimeError('unavailable')
        ops=Mock();ops.recovery_preflight.side_effect=preflight
        ops.application_stopped.side_effect=stopped;ops.ready.side_effect=unavailable
        with patch.object(cli,'NativeOperations',return_value=ops),patch.object(
                cli.time,'monotonic',side_effect=lambda:clock[0]),patch.object(
                daily,'now',return_value=daily.parse('2026-09-25T08:00:00+00:00')):
            cli.recover(config,'fixture')
        ops.recovery_preflight.assert_called_once()
        ops.application_stopped.assert_called_once_with(407)
        ops.ready.assert_called_once_with(405)
        self.assertEqual(ops.restore.call_args.args,(385,))
        self.assertEqual(path.read_bytes(),original)
        self.assertTrue(daily.read_json(path.parent/'application-recovery.json')['application_ready'])
        with patch.object(cli,'NativeOperations',return_value=Mock()) as constructor:
            cli.recover(config,'fixture')
        constructor.return_value.restore.assert_not_called();self.assertEqual(path.read_bytes(),original)

    @unittest.skipUnless(os.name=='posix' and hasattr(os,'geteuid') and os.geteuid()==0,'root parent gate on Linux')
    def test_repair_child_requires_actual_verified_parent_gate(self):
        with self.assertRaisesRegex(ValueError,'supervisor_required'):
            cli.verify_recovery_parent({'database':str(self.db)})
        fixture=self.root/'parent';(fixture/'scripts').mkdir(parents=True)
        real_root=Path(cli.__file__).resolve().parents[1]
        child="import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from scripts import daily_inventory as c;c.ROOT=Path(sys.argv[2]);c.verify_recovery_parent({'database':sys.argv[3]})"
        parent="import sys,subprocess;sys.path.insert(0,sys.argv[2]);from wahojobs.maintenance_gate import operation_gate;\nwith operation_gate(sys.argv[3]):\n p=subprocess.run([sys.executable,'-c',sys.argv[4],sys.argv[2],str(__import__('pathlib').Path.cwd()),sys.argv[3]],capture_output=True)\n if p.returncode:print(p.stderr.decode())\n raise SystemExit(p.returncode)"
        (fixture/'scripts/daily_inventory.py').write_text(parent)
        result=subprocess.run([sys.executable,'-B','scripts/daily_inventory.py','recover',str(real_root),str(self.db),child],cwd=fixture,capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout.decode()+result.stderr.decode())


if __name__=='__main__':unittest.main()
