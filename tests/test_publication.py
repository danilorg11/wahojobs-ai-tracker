"""Common protocol: dependency progress, resource fencing, real file/SQLite workers."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import publication as p
import publication_worker as w


def step(name='change', resource='/website/project', family='frontend'):
    return dict(id=name, family=family, needs=[], checks=['q'], resources=[resource],
        compatibility='standalone', access={'kind':'none'}, duration_reason='measured bounded fixture',
        recovery_reason='conditional same-data compensation',
        seconds=dict(observe=10, apply=20, check=10, recover=20, cleanup=10),
        worker=dict(command=[sys.executable], files=[str(ROOT/'scripts/publication_worker.py')],
            payload=dict(environment='local', kind='files', state_directory=str(ROOT/'outputs'),
                         targets=[], resources=[resource], files=[])), signature=name*32)


class Cloud:
    def __init__(self):
        self.states, self.actions, self.faults = {}, [], {}

    def factory(self, s, operation, plan_hash):
        cloud = self
        class Adapter:
            def call(self, action):
                cloud.actions.append((s['id'], action))
                state = cloud.states.get(s['id'], 'before')
                fault = cloud.faults.get((s['id'], action))
                if fault == 'before': raise TimeoutError()
                if action == 'apply': state = 'applied'
                if action == 'check':
                    if fault == 'check-failed': raise RuntimeError('check')
                    state = 'accepted'
                if action == 'recover': state = 'recovered'
                cloud.states[s['id']] = state
                if fault == 'after': raise TimeoutError()
                if fault == 'exit': raise KeyboardInterrupt()
                return dict(operation=operation, signature=s['signature'], state=state)
        return Adapter()


class CommonTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT/'outputs/publication-v2-tests'; scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to(scratch.resolve()))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.coordinator = p.Coordinator(self.root/'coordinator', scoped=True)
        self.cloud = Cloud()

    def run_step(self, s=None, plan_hash='a'*64):
        return p.drive({}, plan_hash, s or step(), self.coordinator, worker_factory=self.cloud.factory)

    def test_success_terminal_resume_no_new_mutation(self):
        self.assertEqual(self.run_step()['state'], 'accepted')
        self.assertEqual(self.run_step()['state'], 'accepted')
        self.assertEqual(self.cloud.actions.count(('change', 'apply')), 1)

    def test_lost_response_after_commit_is_observed_once(self):
        self.cloud.faults[('change','apply')] = 'after'
        self.assertEqual(self.run_step()['state'], 'accepted')
        self.assertEqual(self.cloud.actions.count(('change','apply')), 1)

    def test_interrupted_process_retains_hold_then_resumes(self):
        self.cloud.faults[('change','apply')] = 'exit'
        with self.assertRaises(KeyboardInterrupt): self.run_step()
        with self.assertRaisesRegex(p.Stop, 'shared_resource_busy'):
            self.run_step(step('other'))
        del self.cloud.faults[('change','apply')]
        self.assertEqual(self.run_step()['state'], 'accepted')
        self.assertEqual(self.cloud.actions.count(('change','apply')), 1)

    def test_no_commit_after_lost_response_never_redispatches(self):
        self.cloud.faults[('change','apply')] = 'before'
        for _ in range(2):
            with self.assertRaisesRegex(p.Stop, 'never_replay'): self.run_step()
        self.assertEqual(self.cloud.actions.count(('change','apply')), 1)

    def test_known_check_failure_returns(self):
        self.cloud.faults[('change','check')] = 'check-failed'
        self.assertEqual(self.run_step()['state'], 'recovered')
        self.assertEqual(self.cloud.actions.count(('change','recover')), 1)

    def test_unknown_return_is_not_repeated(self):
        self.cloud.faults[('change','check')] = 'check-failed'
        self.cloud.faults[('change','recover')] = 'before'
        for _ in range(2):
            with self.assertRaises(p.Stop): self.run_step()
        self.assertEqual(self.cloud.actions.count(('change','recover')), 1)
        self.cloud.states['change'] = 'recovered'
        self.assertEqual(self.run_step()['state'], 'recovered')

    def test_recovery_lost_after_commit_completes(self):
        self.cloud.faults[('change','check')] = 'check-failed'
        self.cloud.faults[('change','recover')] = 'after'
        self.assertEqual(self.run_step()['state'], 'recovered')

    def test_foreign_successor_is_not_changed(self):
        self.cloud.states['change'] = 'foreign'
        with self.assertRaisesRegex(p.Stop, 'foreign_successor'): self.run_step()
        self.assertNotIn(('change','apply'), self.cloud.actions)

    def test_pending_resource_does_not_block_independent_publication(self):
        self.cloud.states['change'] = 'running'
        self.assertEqual(self.run_step()['state'], 'unknown')
        self.assertEqual(self.run_step(step('independent', '/different/project'))['state'], 'accepted')

    def test_host_parent_conflicts_with_service_child(self):
        self.cloud.states['host'] = 'running'
        self.run_step(step('host','/hosts/1'))
        with self.assertRaisesRegex(p.Stop, 'shared_resource_busy'):
            self.run_step(step('service','/hosts/1/services/beta'))

    def test_accepted_step_can_be_carried_into_changed_plan(self):
        first = self.run_step()
        second = self.run_step(plan_hash='b'*64)
        self.assertEqual(second['state'], 'accepted')
        self.assertEqual(second['reused_original']['operation'], first['operation'])
        self.assertEqual(self.cloud.actions.count(('change','apply')), 1)

    def test_changed_accepted_resource_is_not_restamped_as_valid(self):
        self.run_step()
        self.cloud.states['change'] = 'foreign'
        with self.assertRaisesRegex(p.Stop, 'previously_accepted_step_changed'):
            self.run_step(plan_hash='b'*64)

    def test_same_database_for_different_services_conflicts(self):
        a = step('api','/hosts/1/services/api'); a['resources'].append('/hosts/1/databases/product')
        b = step('writer','/hosts/1/services/writer'); b['resources'].append('/hosts/1/databases/product')
        self.cloud.states['api'] = 'running'; self.run_step(a)
        with self.assertRaisesRegex(p.Stop, 'shared_resource_busy'): self.run_step(b)

    def test_legacy_unknown_is_visible_and_history_survives(self):
        registry = self.root/'legacy.json'
        p.once(registry, dict(history=dict(actual=None, protection='712820')))
        coordinator = p.Coordinator(self.root/'bridge', legacy_registry=registry)
        self.cloud.faults[('change','apply')] = 'before'
        with self.assertRaises(p.Stop):
            p.drive({}, 'a'*64, step(), coordinator, worker_factory=self.cloud.factory)
        rows = p.read(registry)
        self.assertEqual(rows['history'], dict(actual=None, protection='712820'))
        self.assertTrue(any(v.get('hold_active') for v in rows.values()))

    def test_legacy_hold_blocks_without_leaking_new_reserved_resource(self):
        registry = self.root/'legacy.json'; p.once(registry, dict(other=dict(hold_active=True)))
        coordinator = p.Coordinator(self.root/'bridge', legacy_registry=registry)
        with self.assertRaisesRegex(p.Stop, 'custodian_release'):
            p.drive({}, 'a'*64, step(), coordinator, worker_factory=self.cloud.factory)
        self.assertEqual(p.read(registry), dict(other=dict(hold_active=True)))
        with coordinator.connection() as db:
            self.assertEqual(db.execute('SELECT state FROM operations').fetchone()[0], 'unchanged')

    def worker_request(self, payload, action, operation='1'*32):
        return dict(protocol=p.FORMAT, action=action, operation=operation, signature='a'*64,
                    plan_sha256='b'*64, payload=payload)

    def files_payload(self):
        target = self.root/'service-selector'; target.write_text('accepted')
        artifact = self.root/'candidate'; artifact.write_text('candidate')
        return dict(environment='local',kind='files',state_directory=str(self.root/'worker'),
            resources=['/service'], targets=[str(target)],
            files=[dict(path=str(target),before=dict(type='file',sha256=p.digest(target.read_bytes())),
                        artifact=str(artifact),artifact_sha256=p.digest(artifact.read_bytes()))])

    def test_real_files_apply_check_return_preserve_unrelated_data(self):
        payload = self.files_payload()
        unrelated = self.root/'user-data'; unrelated.write_text('new user write')
        call = lambda action: w.Worker(self.worker_request(payload,action)).call()
        self.assertEqual(call('observe')['state'], 'before')
        self.assertEqual(call('apply')['state'], 'applied')
        self.assertEqual(call('check')['state'], 'accepted')
        self.assertEqual(call('recover')['state'], 'recovered')
        self.assertEqual(Path(payload['targets'][0]).read_text(), 'accepted')
        self.assertEqual(unrelated.read_text(), 'new user write')

    def test_real_file_worker_does_not_overwrite_a_successor(self):
        payload = self.files_payload()
        w.Worker(self.worker_request(payload,'apply')).call()
        Path(payload['targets'][0]).write_text('foreign')
        self.assertEqual(w.Worker(self.worker_request(payload,'observe')).call()['state'], 'foreign')
        with self.assertRaises(p.Stop): w.Worker(self.worker_request(payload,'recover')).call()

    def sqlite_payload(self):
        target = self.root/'product.sqlite'
        with closing(sqlite3.connect(target)) as db, db:
            db.execute('CREATE TABLE items(id INTEGER PRIMARY KEY, body TEXT, user_note TEXT)')
            db.execute("INSERT INTO items VALUES (1,'teaser','before')")
        return dict(environment='local',kind='sqlite',state_directory=str(self.root/'worker'),
            database=str(target),targets=[str(target)],resources=['/database'],allow_audit_schema=True,
            allowed_tables=['items'], changes=[dict(table='items',key={'id':1},before={'body':'teaser'},after={'body':'complete'})])

    def test_real_sqlite_transaction_and_conditional_compensation_preserve_new_user_field(self):
        payload = self.sqlite_payload()
        call = lambda action: w.Worker(self.worker_request(payload,action)).call()
        self.assertEqual(call('apply')['state'],'applied'); self.assertEqual(call('check')['state'],'accepted')
        with closing(sqlite3.connect(payload['database'])) as db, db: db.execute("UPDATE items SET user_note='new user write' WHERE id=1")
        self.assertEqual(call('recover')['state'],'recovered')
        with closing(sqlite3.connect(payload['database'])) as db, db:
            self.assertEqual(db.execute('SELECT body,user_note FROM items').fetchone(),('teaser','new user write'))

    def test_sqlite_failed_batch_is_atomic(self):
        payload = self.sqlite_payload()
        payload['changes'].append(dict(table='items',key={'id':99},before={'body':'missing'},after={'body':'bad'}))
        with self.assertRaises(p.Stop): w.Worker(self.worker_request(payload,'apply')).call()
        self.assertEqual(w.Worker(self.worker_request(payload,'observe')).call()['state'],'before')
        with closing(sqlite3.connect(payload['database'])) as db, db:
            self.assertEqual(db.execute('SELECT body FROM items').fetchone()[0],'teaser')

    def test_data_successor_prevents_compensation(self):
        payload = self.sqlite_payload(); w.Worker(self.worker_request(payload,'apply')).call()
        with closing(sqlite3.connect(payload['database'])) as db, db: db.execute("UPDATE items SET body='newer source content'")
        self.assertEqual(w.Worker(self.worker_request(payload,'observe')).call()['state'],'foreign')
        with self.assertRaises(p.Stop): w.Worker(self.worker_request(payload,'recover')).call()

    def test_database_commit_is_observed_after_response_is_lost(self):
        payload = self.sqlite_payload(); w.Worker(self.worker_request(payload,'apply')).call()
        self.assertEqual(w.Worker(self.worker_request(payload,'observe')).call()['state'],'applied')
        with self.assertRaises(p.Stop): w.Worker(self.worker_request(payload,'apply')).call()

    def test_direct_worker_refuses_production_without_authority(self):
        payload = self.files_payload(); payload['environment']='production'
        with self.assertRaises((KeyError, FileNotFoundError)):
            w.Worker(self.worker_request(payload,'apply')).call()

    def test_risk_paths_exist_for_every_family(self):
        for family in ('content','frontend','backend','data','integration','infrastructure','accounts'):
            self.assertIn('source', p.FAMILIES[family])
            self.assertIn('recovery', p.FAMILIES[family])

    def test_nested_service_deadlines_are_derived_from_all_hooks(self):
        s=step(family='backend'); s['worker']['payload']['hooks']={'stop':[dict(command=['systemctl','stop','beta'],seconds=90)],
            'start':[dict(command=['systemctl','start','beta'],seconds=690)]}
        self.assertGreaterEqual(p.phase_floors(s)['apply'],785)
        self.assertGreaterEqual(p.phase_floors(s)['recover'],785)

    def test_qualification_reuse_and_exact_input_change(self):
        file = self.root/'input'; file.write_text('source')
        check=dict(id='q',categories=['source'], files=[str(file)],tools=[sys.executable],
            command=[sys.executable,'-c','print("checked")'], cwd=str(self.root),seconds=10)
        first=p.run_check(check,self.root/'cache'); original=Path(first['original']).read_bytes()
        second=p.run_check(check,self.root/'cache')
        self.assertTrue(second['reused']); self.assertEqual(original,Path(second['original']).read_bytes())
        file.write_text('new source'); third=p.run_check(check,self.root/'cache')
        self.assertFalse(third['reused']); self.assertNotEqual(first['sha256'],third['sha256'])

    def test_terminal_resume_reobserves_and_refuses_changed_target(self):
        self.run_step();self.cloud.states['change']='foreign'
        with self.assertRaisesRegex(p.Stop,'completed_operation_target_changed'):self.run_step()
        self.assertEqual(self.cloud.actions.count(('change','apply')),1)

    def test_real_json_policy_return_changes_only_its_owned_key(self):
        target=self.root/'policy.json';p.once(target,dict(code_commit='accepted',access_scope='original'))
        payload=dict(environment='local',kind='files',state_directory=str(self.root/'worker'),
            resources=['/service'],targets=[str(target)],files=[dict(path=str(target),type='json-key',key='code_commit',
                before=dict(type='json-key',value='accepted'),after_value='candidate')])
        call=lambda action:w.Worker(self.worker_request(payload,action)).call()
        call('apply');call('check')
        target.write_bytes(p.encoded(dict(code_commit='candidate',access_scope='new authorized setting')))
        self.assertEqual(call('recover')['state'],'recovered')
        self.assertEqual(p.read(target),dict(code_commit='accepted',access_scope='new authorized setting'))

    def test_sqlite_check_refuses_foreign_content_after_apply(self):
        payload=self.sqlite_payload();w.Worker(self.worker_request(payload,'apply')).call()
        with closing(sqlite3.connect(payload['database'])) as db,db:db.execute("UPDATE items SET body='foreign'")
        with self.assertRaisesRegex(p.Stop,'data_changed_before_acceptance'):w.Worker(self.worker_request(payload,'check')).call()

    def test_module_nested_deadlines_include_native_stop_start_and_recovery(self):
        s=step();s['worker']['payload']=dict(kind='module',phase_floor_seconds=dict(observe=10,apply=30,check=20,recover=30,cleanup=10),
            hooks=dict(stop=[dict(seconds=90)],start=[dict(seconds=690)],recovered_check=[dict(seconds=40)]))
        self.assertEqual(p.phase_floors(s)['apply'],810)
        self.assertEqual(p.phase_floors(s)['recover'],850)

    def test_authority_requires_full_remaining_recovery_and_preserves_unknown_debit(self):
        s=step();s['access']['kind']='private-host';s['signature']='a'*64
        path=self.root/'authority.json';clock=datetime.now(timezone.utc)
        grant=dict(status='granted',plan_sha256='b'*64,human_decision_reference='test fixture actual decision',
            not_before=(clock-timedelta(seconds=1)).isoformat(),expires_at=(clock+timedelta(seconds=900)).isoformat(),
            resources=s['resources'],signatures=[s['signature']],host_maintenance=dict(
                historical_ledger_references=['original ledger fixture'],historical_actual_debit=None,
                window_started_at=(clock-timedelta(seconds=1)).isoformat(),
                carried_protective_capacity='712820',new_full_allowance='1000',approved_ceiling='769600'))
        p.once(path,grant)
        with patch.dict(os.environ,WAHOJOBS_PUBLICATION_AUTHORITY=str(path)):
            p.authorize(dict(steps=[s]),'b'*64,s,{})
            grant['expires_at']=(clock+timedelta(seconds=1)).isoformat();path.write_bytes(p.encoded(grant))
            with self.assertRaisesRegex(p.Stop,'remaining'):p.authorize(dict(steps=[s]),'b'*64,s,{})
            grant['expires_at']=(clock+timedelta(seconds=900)).isoformat();grant['host_maintenance']['approved_ceiling']='712821'
            path.write_bytes(p.encoded(grant))
            with self.assertRaisesRegex(p.Stop,'ceiling'):p.authorize(dict(steps=[s]),'b'*64,s,{})
            grant['host_maintenance']['approved_ceiling']='769600'
            grant['not_before']=(clock-timedelta(seconds=1000)).isoformat()
            grant['host_maintenance']['window_started_at']=(clock-timedelta(seconds=990)).isoformat()
            path.write_bytes(p.encoded(grant))
            with self.assertRaisesRegex(p.Stop,'elapsed_plus_full_remaining'):p.authorize(dict(steps=[s]),'b'*64,s,{})

    def test_real_subprocess_protocol_can_execute_every_class(self):
        checks=[];steps=[]
        for family,categories in p.FAMILIES.items():
            target=self.root/(family+'-selected');target.write_text('accepted')
            artifact=self.root/(family+'-artifact');artifact.write_text('candidate')
            s=step(family,'/fixture/'+family,family);s.pop('signature');s['checks']=[family]
            s['worker'].update(command=[sys.executable,'-B',str(ROOT/'scripts/publication_worker.py')])
            s['worker']['payload'].update(state_directory=str(self.root/'native-workers'),targets=[str(target)],
                files=[dict(path=str(target),before=dict(type='file',sha256=p.digest(target.read_bytes())),
                    artifact=str(artifact),artifact_sha256=p.digest(artifact.read_bytes()))])
            steps.append(s);checks.append(dict(id=family,categories=sorted(categories),files=[str(artifact)],tools=[sys.executable],
                cwd=str(self.root),seconds=10,command=[sys.executable,'-c','print("fixture qualification")']))
        config=self.root/'config.json';p.once(config,dict(format=p.FORMAT,environment='local',title='all domain dispatch routes',checks=checks,steps=steps))
        p.prepare(config,self.root/'plan.json',self.root/'checks')
        result=p.execute(self.root/'plan.json',local=True,coordinator=self.coordinator)
        self.assertTrue(result['complete'],result)
        self.assertEqual(len(result['results']),7)

    def test_artifact_manifest_is_checked_even_on_cached_qualification(self):
        file=self.root/'immutable';file.write_text('accepted');manifest=self.root/'manifest.json'
        p.once(manifest,{str(file):p.digest(file.read_bytes())})
        check=dict(id='q',categories=['source'],files=[],tools=[sys.executable],command=[sys.executable,'-c','print("qualified")'],
            cwd=str(self.root),seconds=10,outputs=[str(manifest)],output_manifests=[str(manifest)])
        p.run_check(check,self.root/'cache');file.write_text('mutated')
        with self.assertRaisesRegex(p.Stop,'artifact_manifest_content_changed'):p.run_check(check,self.root/'cache')

    def test_changed_qualification_content_is_bound_to_step_signature(self):
        s=step();s.pop('signature');file=self.root/'source';file.write_text('one')
        check=dict(id='q',categories=sorted(p.FAMILIES['frontend']),files=[str(file)],tools=[sys.executable],
            command=[sys.executable,'-c','print("qualified")'],cwd=str(self.root),seconds=10)
        config=self.root/'config.json';p.once(config,dict(format=p.FORMAT,environment='local',title='signature binding',checks=[check],steps=[s]))
        p.prepare(config,self.root/'first.json',self.root/'cache');file.write_text('two')
        p.prepare(config,self.root/'second.json',self.root/'cache')
        self.assertNotEqual(p.read(self.root/'first.json')['steps'][0]['signature'],p.read(self.root/'second.json')['steps'][0]['signature'])

    def test_new_checks_preserve_accepted_effect_without_another_apply(self):
        payload=self.files_payload();s=step();s.pop('signature');s['worker'].update(
            command=[sys.executable,'-B',str(ROOT/'scripts/publication_worker.py')],payload=payload)
        s['resources']=payload['resources']
        test_source=self.root/'new-test';test_source.write_text('previous check')
        check=dict(id='q',categories=sorted(p.FAMILIES['frontend']),files=[str(test_source)],tools=[sys.executable],
            command=[sys.executable,'-c','print("qualified")'],cwd=str(self.root),seconds=10)
        config=self.root/'config.json';value=dict(format=p.FORMAT,environment='local',title='preserve accepted work',checks=[check],steps=[s])
        p.once(config,value);p.prepare(config,self.root/'first.json',self.root/'cache')
        self.assertTrue(p.execute(self.root/'first.json',local=True,coordinator=self.coordinator)['complete'])
        test_source.write_text('additional regression check');value['reuse_plans']=[str(self.root/'first.json')];config.write_bytes(p.encoded(value))
        p.prepare(config,self.root/'second.json',self.root/'cache')
        result=p.execute(self.root/'second.json',local=True,coordinator=self.coordinator)
        self.assertTrue(result['complete'],result)
        self.assertIn('reused_original',result['results'][0])
        self.assertEqual(len(list((self.root/'worker').glob('*/apply-intent.json'))),1)

    def test_adoption_can_never_apply_or_return(self):
        payload=self.files_payload();request=self.worker_request(payload,'apply')
        request['adoption']=dict(read_only=True,original_operation=request['operation'],original_signature=request['signature'],
            current_plan_sha256='c'*64,authorized_signature='d'*64)
        with self.assertRaisesRegex(p.Stop,'adoption_is_observation_only'):w.Worker(request).call()

    def test_transition_requires_every_publisher_and_original_evidence(self):
        import publication_transition as t
        proposal=self.root/'proposal.json';receipts=self.root/'handoff.json';evidence=self.root/'observed.json'
        p.once(evidence,dict(actual_remote_state='fixture'))
        p.once(proposal,dict(publishers=[dict(id='legacy')]))
        p.once(receipts,dict(mode='scoped',human_decision_reference='fixture decision',approved_proposal_sha256=p.digest(proposal.read_bytes()),
            publishers=[dict(id='legacy',unknown_prior_outcome_resolved=True,adopted=False,common_coordinator=str(p.STATE),
                evidence_references=[dict(path=str(evidence),sha256=p.digest(evidence.read_bytes()))])]))
        with self.assertRaisesRegex(p.Stop,'publisher_not_adopted'):t.activate(proposal,receipts,p.digest(proposal.read_bytes()),state=self.root/'handoff')

    def test_dependency_failure_does_not_skip_independent_steps(self):
        s=step('dependent');s['needs']=['blocked'];self.cloud.states['blocked']='unknown'
        plan=dict(format=p.FORMAT,environment='local',engine_sha256=p.digest((ROOT/'scripts/publication.py').read_bytes()),
            qualifications=[],steps=[step('blocked'),s,step('independent','/different')])
        path=self.root/'plan.json';p.once(path,plan)
        result=p.execute(path,local=True,coordinator=self.coordinator,worker_factory=self.cloud.factory)
        self.assertEqual([r['state'] for r in result['results']],['unknown','waiting_for_dependency','accepted'])

    def test_generic_sqlite_worker_cannot_delete_rows(self):
        payload=self.sqlite_payload();payload['changes'][0]['after']=None
        with self.assertRaisesRegex(p.Stop,'no_row_deletion'):w.Worker(self.worker_request(payload,'apply')).call()
        with closing(sqlite3.connect(payload['database'])) as db:
            self.assertEqual(db.execute('SELECT body FROM items').fetchone()[0],'teaser')

    def test_missing_acceptance_after_finite_domain_exits_compatibly(self):
        s=step(family='accounts');s['human_acceptance']=dict(mode='inside_finite_domain',receipt=str(self.root/'actual.json'))
        self.assertEqual(self.run_step(s)['state'],'recovered')
        self.assertEqual(self.cloud.actions.count(('change','recover')),1)

    def test_genuine_acceptance_can_follow_finite_domain_apply(self):
        s=step(family='accounts');s['human_acceptance']=dict(mode='inside_finite_domain',receipt=str(self.root/'actual.json'))
        p.once(self.root/'actual.json',dict(accepted=True,human_reference='explicit synthetic test actor',effect_signature=p.effect_signature(s)))
        self.assertEqual(self.run_step(s)['state'],'accepted')

    def test_finite_auth_domain_requires_qualified_exit(self):
        s=step(family='accounts');s.pop('signature');s['human_acceptance']=dict(mode='inside_finite_domain',receipt=str(self.root/'actual.json'))
        check=dict(id='q',categories=sorted(p.FAMILIES['accounts']))
        with self.assertRaisesRegex(p.Stop,'finite_account_exit'):
            p.validate(dict(format=p.FORMAT,environment='local',steps=[s],checks=[check]))

    def test_domain_observation_never_creates_a_missing_database(self):
        import publication_domains as domain
        missing=self.root/'missing.sqlite'
        request=dict(payload=dict(source=str(self.root),database=str(missing),targets=[str(missing)],environment='local'))
        with self.assertRaisesRegex(p.Stop,'existing_exact_domain_database'):
            domain.content('observe',request,self.root)
        self.assertFalse(missing.exists())

    def test_unrelated_long_procedure_does_not_impose_its_window(self):
        host=step('host','/hosts/1');host['seconds']['apply']=1000
        website=step('website','/vercel/other')
        plan=dict(steps=[host,website]);states=dict(host='unknown')
        self.assertEqual(p.remaining_procedure(plan,website,states),p.envelope(website))
        website['needs']=['host']
        self.assertEqual(p.remaining_procedure(plan,website,states),p.envelope(website)+p.envelope(host))

    def test_production_worker_binds_the_exact_approved_payload(self):
        payload=self.files_payload();payload['environment']='production'
        request=self.worker_request(payload,'observe');clock=datetime.now(timezone.utc)
        path=self.root/'authority.json';p.once(path,dict(status='granted',human_decision_reference='synthetic fixture decision',
            plan_sha256=request['plan_sha256'],signatures=[request['signature']],resources=payload['resources'],
            payload_sha256={request['signature']:p.digest(p.encoded(payload))},
            not_before=(clock-timedelta(seconds=1)).isoformat(),expires_at=(clock+timedelta(seconds=100)).isoformat()))
        if os.name!='nt':path.chmod(0o600)
        with patch.dict(os.environ,WAHOJOBS_PUBLICATION_AUTHORITY=str(path)):
            w.validate(request)
            payload['targets']=[str(self.root/'another-target')]
            with self.assertRaisesRegex(p.Stop,'payload_not_approved'):w.validate(request)


if __name__ == '__main__':
    unittest.main()
