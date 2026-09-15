"""Fresh generalist beta rehearsal using only the accepted disposable composition."""
from contextlib import closing, contextmanager
from datetime import datetime
from html import escape
import json
from pathlib import Path
import shutil
import sqlite3
from unittest.mock import patch

from tests.first_time_candidate_support import new_candidate_state, _SyntheticCandidateNotice
from tests.private_beta_matching_support import seed_inventory

BACKGROUND = ('I live in Brazil. My name is Alex. I have two years of customer support experience. '
    'I answer customer questions, review written responses, check information against instructions, '
    'and organize spreadsheet records. I speak Portuguese at native level and English fluently. '
    'I have completed high school and do not have a university degree. '
    'My skills include customer support, data entry, writing, attention to detail and Python. '
    'I want remote AI evaluation, annotation, language review and customer support work. '
    'I am interested in reviewing AI-generated responses. I prefer part-time work.')


@contextmanager
def beta_state(*, port=None):
    with new_candidate_state(port=port) as state:
        with closing(sqlite3.connect(state.database_path)) as connection, connection:
            connection.row_factory=sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('UPDATE jobs SET is_active=0')
            connection.execute('UPDATE canonical_opportunities SET is_active=0')
            sources=seed_inventory(connection,state.clock())
        marker_path=state.directory/'first-time-candidate.json'
        marker=json.loads(marker_path.read_text())
        marker['background']=BACKGROUND
        marker['allow_catalog_fallback']=True
        marker_path.write_text(json.dumps(marker),encoding='utf-8')
        (state.directory/'private-beta-demo.json').write_text(json.dumps(dict(
            synthetic_private_beta_v1=True, now=marker['now'], source_count=len(sources),
            source_authority='preserved public wording and labelled synthetic controls; simulated acceptance dates',
            background=BACKGROUND, invitation=marker['invitation'])),encoding='utf-8')
        yield state


class BetaNotice(_SyntheticCandidateNotice):
    def __init__(self, delegate, directory):
        super().__init__(delegate,offline_extraction=False)
        self.marker=json.loads((Path(directory)/'private-beta-demo.json').read_text())

    def handle(self,*args):
        from scripts.local_recovery_login import _ResponseView
        response=super().handle(*args)
        if not response.body or not any(k.lower()=='content-type' and 'text/html' in v for k,v in response.headers):
            return response
        body=response.body.replace(b'Document extraction is disabled. Manual creation is available.',
            b'Document extraction is disabled. Sources are replayed snapshots and practice examples, not current vacancies.')
        if args[0]=='GET' and args[1].split('?',1)[0]=='/login' and response.status==200:
            # A visible, labelled synthetic invitation is passed through the actual
            # normal invitation form. Account/profile creation still happens there.
            body=body.replace(b"name='invitation'", ("name='invitation' value='"+escape(self.marker['invitation'],quote=True)+"'").encode())
            facts=("<section aria-label='Practice candidate facts' class='synthetic-facts' style='max-width:960px;margin:16px auto;padding:20px;border:1px solid #dbc98d;border-radius:12px;background:#fff9e9;font:16px/1.5 system-ui'>"
                "<h2>Private beta rehearsal</h2><p>Your practice invitation is filled in. You will create Alex’s profile yourself.</p>"
                "<details open><summary>Sample facts to use when creating the profile</summary><p>"+escape(self.marker['background'])+"</p></details>"
                "<p>Try a generalist opportunity and compare its requirements with a language or specialist role. Saved jobs and reminders stay in this rehearsal.</p></section>")
            body=body.replace(b'</body>',facts.encode()+b'</body>')
        return _ResponseView(response,body=body,headers=tuple(
            (k,str(len(body)) if k.lower()=='content-length' else v) for k,v in response.headers))


@contextmanager
def beta_application(state, *, diagnostics=None):
    from scripts.local_recovery_login import controlled_local_product
    from tests.durable_google_login_browser_test_support import _running_https_browser_handler
    from tests.candidate_decision_support import publish_demo_certificate
    from wahojobs.durable_product_browser_handler import make_durable_product_browser_handler
    with patch.dict('os.environ',{'WAHOJOBS_PROFILE_INTAKE_OPENAI_ENABLED':'0','WAHOJOBS_OPENAI_ENRICHMENT':'0'}), \
         patch('wahojobs.profile_intake.openai_adapter.configured_openai_profile_adapter',return_value=None), \
         controlled_local_product(state,allow_invited=True) as (configuration,integration), \
         publish_demo_certificate(state.directory), \
         _running_https_browser_handler(configuration,make_durable_product_browser_handler(
             BetaNotice(integration,state.directory),diagnostics=diagnostics)):
        yield integration


def preserve_fresh_fixture(destination, *, port):
    """Create-only synthetic storage; never reads any real configured database."""
    destination=Path(destination)
    if not destination.is_absolute() or destination.exists() or destination.resolve()!=destination:
        raise ValueError('new_absolute_demo_directory_required')
    if port in (8802,8846,8847,8850) or not 1024 <= port <= 65535:
        raise ValueError('reserved_or_invalid_demo_port')
    with beta_state(port=port) as state:
        shutil.copytree(state.directory,destination)
        document=json.loads((destination/'runtime.json').read_text())
        def relocate(value):
            if isinstance(value,dict): return {k:relocate(v) for k,v in value.items()}
            if isinstance(value,list): return [relocate(v) for v in value]
            if isinstance(value,str) and value.startswith(str(state.directory)):
                return str(destination)+value[len(str(state.directory)):]
            return value
        (destination/'runtime.json').write_text(json.dumps(relocate(document)),encoding='utf-8')
    return reopen_fixture(destination)


def reopen_fixture(directory):
    from scripts.local_recovery_login import RealtimeClock
    from tests.durable_google_login_browser_test_support import TemporaryBrowserLoginState,FIXTURE_SUBJECT
    directory=Path(directory).resolve(strict=True)
    marker=json.loads((directory/'private-beta-demo.json').read_text())
    if marker.get('synthetic_private_beta_v1') is not True:
        raise ValueError('synthetic_private_beta_fixture_required')
    document=json.loads((directory/'runtime.json').read_text())
    database=Path(document['database_path'])
    if database.parent!=directory or document['bind_port'] in (8802,8846,8847,8850):
        raise ValueError('invalid_beta_fixture_boundary')
    return TemporaryBrowserLoginState(directory=directory,database_path=database,
        configuration_path=directory/'runtime.json',public_origin=document['public_origin'],
        redirect_uri=document['google_redirect_uri'],subject=FIXTURE_SUBJECT,account_id='',
        principal_id='',profile_id='',clock=RealtimeClock())
