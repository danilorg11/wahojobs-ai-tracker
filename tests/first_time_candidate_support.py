"""New invited candidate + existing product, entirely in disposable storage."""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from unittest.mock import patch

from tests.candidate_continuity_support import reserve_port, capture
from tests.durable_google_login_browser_test_support import temporary_browser_login_state, _running_https_browser_handler
from tests.test_authenticated_profile_matches import _seed_configured_inventory
from tests.accounts_test_support import INVITATION_KEY

BACKGROUND = ('I live in Brazil. I am a Python backend software engineer with 5 years of experience. '
              'I develop Python software, build software tests, and evaluate AI code responses. '
              'My skills include Python, software engineering, backend development and software testing. '
              'I speak English fluently. I want remote software engineering and AI coding evaluation work.')


@contextmanager
def new_candidate_state(*, port=None, now=None):
    with temporary_browser_login_state(port=port or reserve_port(), seed_existing_identity=False,
            seed_existing_profile=False, enable_invited_provisioning=True,
            mutate_configuration=lambda d: d.update(environment='private_beta')) as state:
        from tests.google_oidc_gateway_test_support import ManualClock
        from scripts.workos_authkit_provider_migration import apply_workos_authkit_provider_migration
        from scripts.public_job_identity_migration import apply_public_job_identity_migration
        from scripts.ai_profile_import_migration import apply_ai_profile_import_migration
        from scripts.resumable_ai_profile_intake_migration import apply_resumable_ai_profile_intake_migration
        from wahojobs import accounts
        state.clock = ManualClock(now or datetime.now(timezone.utc).replace(microsecond=0))
        now = state.clock()
        with closing(sqlite3.connect(state.database_path)) as connection:
            connection.execute('PRAGMA foreign_keys=ON')
            for migrate in (apply_workos_authkit_provider_migration, apply_public_job_identity_migration,
                            apply_ai_profile_import_migration, apply_resumable_ai_profile_intake_migration):
                migrate(connection)
            connection.row_factory = sqlite3.Row
            invitation = accounts.create_invitation(connection, email='new-candidate@example.test',
                lookup_key=INVITATION_KEY, expires_at=now+timedelta(days=7),
                created_by='synthetic_first_time_candidate_fixture',
                idempotency_key='new-candidate-fixture-invitation', now=now)
            assert connection.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0
            assert connection.execute('SELECT COUNT(*) FROM product_profiles').fetchone()[0] == 0
        _seed_configured_inventory(state.database_path, observed_at=now)
        with closing(sqlite3.connect(state.database_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute("UPDATE companies SET name='Synthetic Demo Jobs' WHERE id=7001")
            connection.execute("UPDATE jobs SET title='Python Backend AI Coding Evaluator', "
                "department='Software Engineering',expertise='Software Engineering',commitment='Full-time', "
                "location='Remote - Brazil',url='https://jobs.example.test/PostingA' WHERE id=7003")
            connection.execute("UPDATE canonical_opportunities SET canonical_title='Python Backend AI Coding Evaluator', "
                "source_category='Software Engineering' WHERE id=7002")
            capture(connection, 7003, now.isoformat())
        (state.directory/'first-time-candidate.json').write_text(json.dumps({
            'synthetic_first_time_candidate_v1': True, 'now': now.isoformat(),
            'invitation': invitation.invitation_token, 'background': BACKGROUND}), encoding='utf-8')
        yield state


@contextmanager
def candidate_application(state, *, adapter=None):
    from scripts.local_recovery_login import controlled_local_product
    from wahojobs.durable_product_browser_handler import make_durable_product_browser_handler
    # This overrides only adapter configuration, with an explicitly labelled
    # existing offline fixture. No real OpenAI client is constructed or invoked.
    with patch.dict('os.environ', {'WAHOJOBS_PROFILE_INTAKE_OPENAI_ENABLED':'0', 'WAHOJOBS_OPENAI_ENRICHMENT':'0'}), \
         patch('wahojobs.profile_intake.openai_adapter.configured_openai_profile_adapter', return_value=adapter), \
         controlled_local_product(state, allow_invited=True) as (configuration, integration):
        with _running_https_browser_handler(configuration, make_durable_product_browser_handler(
                _SyntheticCandidateNotice(integration, offline_extraction=adapter is not None))):
            yield integration


class _SyntheticCandidateNotice:
    """Label this fixture composition without replacing any product route."""
    def __init__(self, delegate, *, offline_extraction):
        self.delegate = delegate
        self.offline_extraction = offline_extraction

    def handle(self, *args):
        from scripts.local_recovery_login import _ResponseView
        response = self.delegate.handle(*args)
        if response.body and any(name.lower() == 'content-type' and 'text/html' in value
                                 for name, value in response.headers):
            notice = ('Synthetic candidate demonstration. Controlled local identity; no external sign-in or email delivery. '
                + ('Extraction uses a labelled offline fixture, not a real model interpretation.'
                   if self.offline_extraction else 'Document extraction is disabled. Manual creation is available.'))
            import re
            banner = ('<aside role="note" aria-label="Synthetic fixture" style="padding:12px 20px;background:#fff2ce;color:#413519;font:14px/1.5 system-ui">'+notice+'</aside>').encode()
            body = re.sub(br'<body(?:\s[^>]*)?>', lambda match: match.group(0)+banner,
                          response.body, count=1, flags=re.IGNORECASE)
            return _ResponseView(response, body=body, headers=tuple(
                (name, str(len(body)) if name.lower() == 'content-length' else value)
                for name, value in response.headers))
        return response
