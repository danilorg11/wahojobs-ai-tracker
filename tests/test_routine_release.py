"""Publication faults and durable coordination; no production or credentials."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]/'scripts/routine_release.py'
spec = importlib.util.spec_from_file_location('routine_release', SOURCE)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def config(root):
    return dict(project='prj_test', team='team_test', scope='fixture',
        candidate_commit='b'*40, accepted_commit='a'*40, accepted_deployment='dpl_base', candidate_deployment='dpl_next',
        aliases=['www.example.test', 'old.example.test'], release_seconds=900,
        probes=[dict(path='/company/mercor', status=200, contains=['Mercor', 'AI Training Jobs'])],
        accepted_probes=[dict(path='/jobs', status=200, contains=['AI Training Jobs'])],
        coordination_registry=str(root/'registry.json'),
        coordinator_directory=str(SOURCE.parent.parent/'outputs/publication-state'))


def plan(root):
    return dict(version=1, classification='routine', config=config(root),
        engine_sha256=r.digest(SOURCE.read_bytes()), tool_pins={}, qualifications=[],
        changed_files=['src/components/CompanyAIJobs/styles.module.css'])


class Cloud:
    """Models cloud state outside the publishing process (survives its crashes)."""
    def __init__(self, c):
        self.c = c
        self.selected = 'dpl_base'
        self.actions = []
        self.forward_fault = None
        self.return_fault = None
        self.live_failed = False
        self.preview_failed = False
        self.foreign_after_smoke = False
        self.metadata_fault = None
        self.pending = False

    def metadata(self, target):
        identifier = self.selected if target in self.c['aliases'] else target
        source = self.c['accepted_commit'] if identifier == 'dpl_base' else self.c['candidate_commit']
        row = dict(id=identifier, commit=source, project=self.c['project'], team=self.c['team'],
                   ready='READY', target='production', url=identifier+'.vercel.app')
        if self.metadata_fault and target == 'dpl_next':
            row.update(self.metadata_fault)
        return row

    def promote(self, identifier):
        self.actions.append(identifier)
        fault = self.forward_fault if identifier == 'dpl_next' else self.return_fault
        if fault == 'timeout_before':
            raise TimeoutError()
        self.selected = identifier
        if fault == 'timeout_after':
            raise TimeoutError()
        if fault == 'crash_after':
            raise KeyboardInterrupt()
        return '0'*64

    def smoke(self, host, probes):
        if host.endswith('vercel.app') and self.preview_failed:
            raise r.Stop('preview_bad')
        if host == self.c['aliases'][0] and self.selected == 'dpl_next' and self.live_failed:
            raise r.Stop('live_bad')
        if host == self.c['aliases'][0] and self.selected == 'dpl_next' and self.foreign_after_smoke:
            self.selected = 'dpl_foreign'
        return [dict(path=p['path'], status=200) for p in probes]

    def promotion(self, target=None):
        if target and self.pending:
            raise r.Stop('promotion_still_pending_observe_only')


class PublicationTests(unittest.TestCase):
    def setUp(self):
        scratch = SOURCE.parent.parent/'outputs/routine-release-tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.assertTrue(Path(self.temporary.name).resolve().is_relative_to(scratch.resolve()))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan = plan(self.root)
        self.p = self.root/'plan.json'
        r.once(self.p, self.plan)
        self.sha = r.digest(self.p.read_bytes())
        self.registry = self.root/'registry.json'
        r.once(self.registry, dict(historical=dict(actual_debit=None, protection='712820', ceiling='769600')))
        self.before = r.read(self.registry)
        self.run = self.root/'run'
        self.cloud = Cloud(self.plan['config'])
        self.patcher = patch.object(r, 'LEGACY_REGISTRY', self.registry)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def execute(self, **kw):
        return r.publish(self.p, 'dpl_next', self.run, kw.get('approval', self.sha), self.cloud)

    def test_success_and_terminal_resume_do_not_republish(self):
        self.assertEqual(self.execute()['state'], 'accepted')
        self.assertEqual(self.execute()['state'], 'accepted')
        self.assertEqual(self.cloud.actions, ['dpl_next'])
        self.assertEqual(r.read(self.registry), self.before)

    def test_live_failure_returns_exact_accepted_deployment_without_data_restore(self):
        self.cloud.live_failed = True
        self.assertEqual(self.execute()['state'], 'recovered')
        self.assertEqual(self.cloud.actions, ['dpl_next', 'dpl_base'])
        self.assertEqual(r.read(self.registry), self.before)

    def test_timeout_after_commit_is_observed_without_duplicate(self):
        self.cloud.forward_fault = 'timeout_after'
        self.assertEqual(self.execute()['state'], 'accepted')
        self.assertEqual(self.cloud.actions, ['dpl_next'])

    def test_alias_convergence_without_terminal_provider_job_keeps_hold(self):
        self.cloud.pending = True
        with self.assertRaisesRegex(r.Stop, 'still_pending'):
            self.execute()
        self.assertIn('routine_team_test_prj_test', r.read(self.registry))
        self.cloud.pending = False
        self.assertEqual(self.execute()['state'], 'accepted')
        self.assertEqual(self.cloud.actions, ['dpl_next'])

    def test_crash_after_commit_resumes_in_new_publisher_instance(self):
        self.cloud.forward_fault = 'crash_after'
        with self.assertRaises(KeyboardInterrupt):
            self.execute()
        self.assertIn('routine_team_test_prj_test', r.read(self.registry))
        self.cloud.forward_fault = None
        self.assertEqual(self.execute()['state'], 'accepted')
        self.assertEqual(self.cloud.actions, ['dpl_next'])

    def test_unknown_with_old_aliases_keeps_reservation_and_never_retries(self):
        self.cloud.forward_fault = 'timeout_before'
        for _ in range(2):
            with self.assertRaisesRegex(r.Stop, 'unknown'):
                self.execute()
        self.assertEqual(self.cloud.actions, ['dpl_next'])
        self.assertIn('routine_team_test_prj_test', r.read(self.registry))
        other = self.root/'other'
        with self.assertRaisesRegex(r.Stop, 'reservation_changed'):
            r.publish(self.p, 'dpl_next', other, self.sha, self.cloud)

    def test_unknown_return_is_observed_no_second_return(self):
        self.cloud.live_failed = True
        self.cloud.return_fault = 'timeout_before'
        for _ in range(2):
            with self.assertRaisesRegex(r.Stop, 'unknown'):
                self.execute()
        self.cloud.selected = 'dpl_base'  # Existing cloud operation eventually completes.
        self.assertEqual(self.execute()['state'], 'recovered')
        self.assertEqual(self.cloud.actions, ['dpl_next', 'dpl_base'])

    def test_return_timeout_after_commit_finishes_recovered(self):
        self.cloud.live_failed = True
        self.cloud.return_fault = 'timeout_after'
        self.assertEqual(self.execute()['state'], 'recovered')
        self.assertEqual(self.cloud.actions, ['dpl_next', 'dpl_base'])

    def test_crash_after_return_resumes_recovery(self):
        self.cloud.live_failed = True
        self.cloud.return_fault = 'crash_after'
        with self.assertRaises(KeyboardInterrupt):
            self.execute()
        self.assertEqual(self.execute()['state'], 'recovered')
        self.assertEqual(self.cloud.actions, ['dpl_next', 'dpl_base'])

    def test_foreign_successor_is_never_overwritten(self):
        self.cloud.foreign_after_smoke = True
        with self.assertRaisesRegex(r.Stop, 'foreign_successor'):
            self.execute()
        self.assertEqual(self.cloud.actions, ['dpl_next'])

    def test_preexisting_foreign_or_mixed_aliases_do_not_mutate(self):
        self.cloud.selected = 'dpl_foreign'
        with self.assertRaisesRegex(r.Stop, 'foreign_successor'):
            self.execute()
        self.assertEqual(self.cloud.actions, [])
        self.assertEqual(r.read(self.registry), self.before)

    def test_preview_failure_keeps_accepted_and_releases_reservation(self):
        self.cloud.preview_failed = True
        with self.assertRaisesRegex(r.Stop, 'preview_bad'):
            self.execute()
        self.assertEqual(self.cloud.actions, [])
        self.assertEqual(r.read(self.registry), self.before)
        self.assertEqual(r.read(self.run/'preflight-stopped.json')['state'], 'unchanged')

    def test_wrong_project_team_commit_target_or_readiness_fail_before_mutation(self):
        for fault in ({'project': 'prj_foreign'}, {'team': 'team_foreign'}, {'commit': 'c'*40},
                      {'ready': 'BUILDING'}, {'target': 'preview'}):
            self.cloud.metadata_fault = fault
            with self.assertRaises(r.Stop):
                r.Publisher(self.plan, self.sha, self.root/str(len(str(fault))), self.cloud).execute('dpl_next')
        self.assertEqual(self.cloud.actions, [])

    def test_legacy_global_hold_blocks_without_touching_history(self):
        self.registry.write_bytes(r.encoded(dict(self.before, other=dict(hold_active=True))))
        before = self.registry.read_bytes()
        with self.assertRaisesRegex(r.Stop, 'custodian'):
            self.execute()
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.cloud.actions, [])

    def test_unanswered_or_changed_plan_is_not_an_approval(self):
        with self.assertRaisesRegex(r.Stop, 'approved_plan'):
            self.execute(approval='0'*64)
        self.assertEqual(self.cloud.actions, [])

    def test_entire_recovery_must_fit_before_forward_mutation(self):
        publisher = r.Publisher(self.plan, self.sha, self.run, self.cloud)
        publisher.deadline = r.time.monotonic()+100
        with self.assertRaisesRegex(r.Stop, 'does_not_fit'):
            publisher.execute('dpl_next')
        self.assertEqual(self.cloud.actions, [])

    def test_os_lock_fences_second_process(self):
        lock = self.root/'writer.lock'
        with r.file_lock(lock):
            program = "import sys;sys.path.insert(0,sys.argv[1]);import routine_release as r\nwith r.file_lock(sys.argv[2]): pass"
            p = subprocess.run([sys.executable, '-B', '-c', program, str(SOURCE.parent), str(lock)], capture_output=True)
            self.assertNotEqual(p.returncode, 0)

    def test_full_serial_path_includes_all_probes_aliases_and_recovery(self):
        c = self.plan['config']
        self.assertEqual(r.bounds(c), dict(preflight=129, forward=147, recovery=177, complete=483))
        c['probes'] *= 6; c['accepted_probes'] *= 6
        c['aliases'] += ['third.example.test', 'fourth.example.test']
        self.assertEqual(r.bounds(c)['complete'], 903)


class ClassificationTests(unittest.TestCase):
    def test_content_and_styles_use_routine_lane(self):
        for path in ['src/config/aiCompanyProfiles.json', 'public/assets/companies/mercor.png',
                     'src/components/CompanyAIJobs/styles.module.css']:
            self.assertEqual(r.classify([path]), 'routine')

    def test_api_accounts_infrastructure_and_unknown_never_use_fast_lane(self):
        for path in ['src/lib/companyInventory.mjs', 'src/services/CompanyService.ts',
                     'src/components/Login/styles.module.css', 'src/components/account/styles.css',
                     'wahojobs/public_catalog_reader.py', 'package.json', 'vercel.json', 'unknown.txt']:
            self.assertEqual(r.classify([path]), 'coordinated')


class QualificationTests(unittest.TestCase):
    def setUp(self):
        scratch = SOURCE.parent.parent/'outputs/routine-release-tests'
        scratch.mkdir(parents=True, exist_ok=True)
        # Retain this small fixture, including its Git history, for inspection.
        self.root = Path(tempfile.mkdtemp(dir=scratch))
        self.repo = self.root/'source'; self.repo.mkdir()
        r.git(self.repo, 'init', '-q')
        (self.repo/'src').mkdir()
        (self.repo/'src/check.txt').write_text('valid\n')
        (self.repo/'yarn.lock').write_text('locked\n')
        self.commit()
        self.config = dict(repository=str(self.repo), candidate_commit=self.head(),
            checks=[dict(name=name, inputs=['src/*'], command=[sys.executable, '-B', '-c', 'print("qualification")'], timeout_seconds=10)
                    for name in ('regression', 'build')], tool_files=[sys.executable])

    def head(self):
        return r.git(self.repo, 'rev-parse', 'HEAD').decode().strip()

    def commit(self):
        r.git(self.repo, 'add', '.')
        r.git(self.repo, '-c', 'user.name=Offline fixture', '-c', 'user.email=fixture@example.invalid',
              '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')

    def test_reuses_unchanged_inputs_preserving_original_timestamp(self):
        first = r.qualify(self.config, self.root/'cache')
        original = Path(first[0]['original']).read_bytes()
        second = r.qualify(self.config, self.root/'cache')
        self.assertTrue(all(row['reused'] for row in second))
        self.assertEqual(Path(second[0]['original']).read_bytes(), original)
        self.assertEqual(first[0]['sha256'], second[0]['sha256'])

    def test_lockfile_change_invalidates_cache_without_expanding_input_globs(self):
        first = r.qualify(self.config, self.root/'cache')
        (self.repo/'yarn.lock').write_text('new lock\n'); self.commit()
        self.config['candidate_commit'] = self.head()
        second = r.qualify(self.config, self.root/'cache')
        self.assertFalse(second[0]['reused'])
        self.assertNotEqual(first[0]['input_sha256'], second[0]['input_sha256'])

    def test_rejects_changed_logs_and_dirty_sources(self):
        first = r.qualify(self.config, self.root/'cache')
        Path(first[0]['original']).with_suffix('.log').write_bytes(b'changed')
        with self.assertRaisesRegex(r.Stop, 'log_changed'):
            r.qualify(self.config, self.root/'cache')
        (self.repo/'src/check.txt').write_text('dirty')
        with self.assertRaisesRegex(r.Stop, 'clean_checkout'):
            r.qualify(self.config, self.root/'cache')

    def test_unrelated_commit_does_not_expire_valid_check(self):
        first = r.qualify(self.config, self.root/'cache')
        (self.repo/'notes.md').write_text('independent documentation'); self.commit()
        self.config['candidate_commit'] = self.head()
        second = r.qualify(self.config, self.root/'cache')
        self.assertTrue(second[0]['reused'])
        self.assertEqual(first[0]['sha256'], second[0]['sha256'])

    def test_qualification_timeout_preserves_failure_and_allows_known_new_attempt(self):
        self.config['checks'][0].update(command=[sys.executable, '-c', 'import time;time.sleep(5)'], timeout_seconds=.05)
        for _ in range(2):
            with self.assertRaisesRegex(r.Stop, 'qualification_failed'):
                r.qualify(self.config, self.root/'cache')
        self.assertEqual(len(list((self.root/'cache').glob('*.failed-*.json'))), 2)

    def test_configuration_and_command_changes_invalidate_cache(self):
        first = r.qualify(self.config, self.root/'cache')
        self.config['check_environment'] = {'LOCAL_NON_SECRET_SETTING': 'changed'}
        second = r.qualify(self.config, self.root/'cache')
        self.assertNotEqual(first[0]['input_sha256'], second[0]['input_sha256'])


if __name__ == '__main__':
    unittest.main()
