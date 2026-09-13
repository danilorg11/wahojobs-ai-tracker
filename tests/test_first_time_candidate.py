from contextlib import closing
import json
import base64
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.first_time_candidate_support import new_candidate_state, candidate_application
from tests.test_candidate_continuity_client import run_client, persisted_state


def observe(state):
    result = persisted_state(state)
    with closing(sqlite3.connect(state.database_path)) as connection:
        connection.row_factory = sqlite3.Row
        for key, table in [('users','users'), ('profiles','product_profiles'),
                           ('revisions','product_profile_revisions'), ('sources','product_profile_sources'),
                           ('attempts','ai_profile_import_attempts')]:
            result[key] = [dict(row) for row in connection.execute('SELECT * FROM '+table)]
    return result


def client(state, mode):
    fixture = json.loads((state.directory/'first-time-candidate.json').read_text(encoding='utf-8'))
    if mode.startswith('import'):
        from tests.test_profile_intake_foundation import _docx_bytes
        fixture['document'] = base64.b64encode(_docx_bytes(paragraphs=('Synthetic Candidate','Platform engineer in Lisbon','Python software development'))).decode('ascii')
    return run_client(state, mode, script=Path(__file__).with_name('first_time_candidate_client.cjs'),
                      observe=observe, fixture=fixture)


class FirstTimeCandidateTests(unittest.TestCase):
    def test_manual_checkpoints_separate_owners_reject_stale_and_accept_exact_retry(self):
        from wahojobs import manual_profile_drafts as drafts
        with tempfile.TemporaryDirectory() as directory, closing(sqlite3.connect(Path(directory)/'account.sqlite')) as connection:
            first = drafts.save(connection, 'owner-a', '', {'raw_input':'Sao Paulo — synthetic'})
            self.assertEqual(drafts.load(connection, 'owner-b'), None)
            self.assertEqual(drafts.save(connection, 'owner-a', '', {'raw_input':'Sao Paulo — synthetic'}), first)
            second = drafts.save(connection, 'owner-a', first, {'raw_input':'Recife'})
            with self.assertRaises(drafts.StaleManualDraft):
                drafts.save(connection, 'owner-a', first, {'raw_input':'Stale'})
            self.assertEqual(drafts.load(connection, 'owner-a'), (second, {'raw_input':'Recife'}))
            drafts.save(connection, 'owner-b', '', {'raw_input':'Independent'})
            self.assertEqual(drafts.load(connection, 'owner-a'), (second, {'raw_input':'Recife'}))

    def test_new_invited_candidate_manual_to_saved_job(self):
        with new_candidate_state() as state:
            with candidate_application(state):
                client(state, 'manual')
            after = observe(state)
            self.assertEqual(len(after['profiles']),1)
            self.assertEqual(len(after['attempts']),0)
            state.close_harnesses()
            with candidate_application(state):
                client(state, 'candidate-return')
            self.assertEqual(observe(state)['revisions'],after['revisions'])

    def test_labelled_offline_import_through_served_client(self):
        from tests.test_profile_intake_final_save import _FinalSaveAdapter
        with new_candidate_state() as state:
            adapter = _FinalSaveAdapter(state.database_path, (), include_country=True)
            with candidate_application(state, adapter=adapter):
                client(state, 'import')
            self.assertEqual(len(adapter.calls),1)

    def test_unconfirmed_review_survives_runtime_restart_and_new_session(self):
        with new_candidate_state() as state:
            with candidate_application(state):
                client(state, 'draft')
            state.close_harnesses()
            self.assertEqual(observe(state)['profiles'],[])
            with candidate_application(state):
                client(state, 'resume')

    def test_saved_import_review_resumes_when_extraction_is_disabled(self):
        from tests.test_profile_intake_final_save import _FinalSaveAdapter
        with new_candidate_state() as state:
            adapter = _FinalSaveAdapter(state.database_path, (), include_country=True)
            with candidate_application(state, adapter=adapter):
                client(state, 'import-draft')
            state.close_harnesses()
            self.assertEqual(observe(state)['profiles'], [])
            with candidate_application(state):
                client(state, 'import-resume')
            self.assertEqual(len(adapter.calls), 1)

    def test_interruption_after_review_confirmation_can_resume_before_profile_creation(self):
        with new_candidate_state() as state:
            with candidate_application(state):
                client(state, 'confirmation-draft')
            state.close_harnesses()
            self.assertEqual(observe(state)['profiles'], [])
            with candidate_application(state):
                client(state, 'resume')

    def test_completed_profile_with_no_eligible_posting_is_honest(self):
        with new_candidate_state() as state, candidate_application(state):
            client(state, 'empty')


if __name__ == '__main__': unittest.main()
