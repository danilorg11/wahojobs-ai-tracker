import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.private_beta_demo_support import beta_state,beta_application,preserve_fresh_fixture,reopen_fixture
from tests.candidate_decision_support import verified_https_request
from tests.test_first_time_candidate import client,observe


class PrivateBetaDemoTests(unittest.TestCase):
    def test_fresh_generalist_native_forms_confirm_save_correct_and_return(self):
        with beta_state() as state, patch('tests.test_candidate_continuity_client.https_request',verified_https_request):
            self.assertEqual(observe(state)['profiles'],[])
            with beta_application(state): client(state,'manual')
            before=observe(state)
            state.close_harnesses()
            with beta_application(state): client(state,'candidate-return')
            self.assertEqual(observe(state)['revisions'],before['revisions'])

    def test_fresh_restricted_candidate_has_honest_empty_outcome(self):
        with beta_state() as state, patch('tests.test_candidate_continuity_client.https_request',verified_https_request):
            with beta_application(state): client(state,'empty')
            self.assertEqual(observe(state)['items'],[])

    def test_demo_restart_reuses_create_only_state_without_preconfirmed_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)/'demo'
            from tests.candidate_continuity_support import reserve_port
            state=preserve_fresh_fixture(directory,port=reserve_port())
            self.assertEqual(observe(state)['profiles'],[])
            reopened=reopen_fixture(directory)
            self.assertEqual(reopened.database_path,state.database_path)
            self.assertEqual(json.loads((directory/'private-beta-demo.json').read_text())['source_count'],16)
            with self.assertRaises(ValueError): preserve_fresh_fixture(directory,port=8861)
