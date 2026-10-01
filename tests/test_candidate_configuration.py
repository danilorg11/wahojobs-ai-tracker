from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from wahojobs import candidate_configuration as config
from wahojobs.workos_authkit_staging import WorkOSAuthKitStagingError
from scripts import daily_inventory as native


class CandidateConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'candidate-v1.json'
        self.document=dict(enabled=False,public_controls=False,scope='candidate_my_jobs_v1',workos_environment='production',
            workos_client_id='client_0123456789abcdef',workos_api_key='sk_OFFLINE_ONLY_0123456789abcdef',gateway_key='b'*64)
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.document));self.path.chmod(0o600)

    def test_disabled_and_configuration_checks_never_construct_provider(self):
        with patch('wahojobs.workos_authkit.create_workos_sdk_boundary') as sdk:
            self.assertEqual(config.build_candidate_integration(str(self.path),Mock(),Mock()),(None,None))
            sdk.assert_not_called()
        for key,value in [('workos_environment','staging'),('scope','whole_private_beta'),('workos_client_id','invalid'),
                          ('gateway_key','a'),('enabled','true'),('public_controls','true'),('workos_api_key','REPLACE')]:
            original=self.document[key];self.document[key]=value;self.write()
            with self.subTest(key=key),self.assertRaises(ValueError):config.load_configuration(str(self.path))
            self.document[key]=original
        raw=json.dumps(self.document)[:-1]+',"enabled":false}'
        self.path.write_text(raw)
        with self.assertRaises(ValueError):config.load_configuration(str(self.path))

    def test_native_command_pin_accepts_only_exact_catalog_plus_candidate_paths(self):
        normal=['/opt/wahojobs-beta/current/.venv/bin/python','-B','scripts/private_beta_app.py',
                '--config','/run/wahojobs-beta/runtime.json','--logs','/var/log/wahojobs-beta']
        catalog=normal+['--public-catalog-config','/run/wahojobs-beta/public-catalog-v1.json']
        enabled=catalog+['--candidate-config',config.OPERATING_CONFIGURATION_PATH]
        with patch('wahojobs.public_catalog_configuration.load_configuration',return_value={}) as public,patch.object(config,'load_configuration',return_value={}) as candidate:
            native.verify_beta_process_command(enabled)
            candidate.assert_called_once_with(config.OPERATING_CONFIGURATION_PATH)
            for command in [normal+enabled[-2:],enabled+['--debug'],catalog+['--candidate-config','/tmp/other.json'],enabled+enabled[-2:]]:
                with self.subTest(command=command),self.assertRaisesRegex(ValueError,'beta_process_mismatch'):
                    native.verify_beta_process_command(command)

    def test_native_credential_requires_valid_source_and_identical_effective_copy(self):
        effective=Path(self.tmp.name)/'effective.json'
        effective.write_bytes(self.path.read_bytes());effective.chmod(0o600)
        with patch.object(config,'SOURCE_CONFIGURATION_PATH',str(self.path)),patch.object(
                config,'OPERATING_CONFIGURATION_PATH',str(effective)):
            self.assertTrue(native.candidate_credential_selected())
            effective.write_text(json.dumps(dict(self.document,public_controls=True)))
            with self.assertRaisesRegex(ValueError,'candidate_effective_mismatch'):
                native.candidate_credential_selected()
            effective.unlink()
            self.assertTrue(native.candidate_credential_selected(require_effective=False))
            with self.assertRaises(WorkOSAuthKitStagingError):native.candidate_credential_selected()
            self.path.write_text('{"enabled":false}')
            with self.assertRaises(ValueError):native.candidate_credential_selected(require_effective=False)
            self.path.unlink()
            self.assertFalse(native.candidate_credential_selected())

    def test_auth_initialization_outage_keeps_anonymous_reader_running(self):
        from scripts.private_beta_app import run
        configuration=Mock();configuration.proxy_secret='a'*64
        runtime=Mock();runtime.bind_address=('127.0.0.1',0)
        reader=Mock();reader.candidate_enabled=False
        server=Mock()
        errors=io.StringIO()
        with patch('scripts.private_beta_app.load_workos_authkit_staging_configuration',return_value=configuration),patch(
                'scripts.private_beta_app.prepare_runtime',return_value=reader),patch(
                'wahojobs.public_catalog_configuration.load_configuration',return_value={'gateway_key':'a'*64}),patch.object(
                config,'build_candidate_integration',side_effect=RuntimeError('private synthetic detail')),patch(
                'scripts.private_beta_app.make_remote_handler') as handler,patch('scripts.private_beta_app.signal.signal'),patch(
                'scripts.private_beta_app.signal.getsignal'),redirect_stderr(errors):
            run('synthetic-config',runtime_builder=lambda unused:runtime,server_factory=lambda *args:server,
                public_catalog_configuration_path='synthetic-catalog',candidate_configuration_path='synthetic-candidate')
            self.assertIsNone(handler.call_args.kwargs['candidate'])
            self.assertFalse(reader.candidate_enabled)
            server.serve_forever.assert_called_once()
        self.assertEqual(errors.getvalue(),'candidate_account_unavailable\n')

    def test_operating_dropin_retains_authoritative_config_and_new_flags_are_explicit(self):
        text=(Path(__file__).parents[1]/'deploy/private-beta/80-candidate-my-jobs.conf').read_text()
        self.assertIn('LoadCredential=candidate-v1.json:'+config.SOURCE_CONFIGURATION_PATH,text)
        self.assertIn('--candidate-config '+config.OPERATING_CONFIGURATION_PATH,text)
        self.assertIn('--config /run/wahojobs-beta/runtime.json',text)
        self.assertIn('--public-catalog-config /run/wahojobs-beta/public-catalog-v1.json',text)
        self.assertNotIn('Environment=OPENAI',text)
