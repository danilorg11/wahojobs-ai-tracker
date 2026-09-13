"""Exact posting continuity; synthetic fixtures only."""
import unittest
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
import json
import base64
import hashlib
import sqlite3
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch
from scripts import local_product_app as local
from tests.test_local_product_ranked_matches import make_match
from wahojobs import authenticated_profile_matches as browser
from tests.candidate_continuity_support import synthetic_state, running_process, running_application, BrowserClient, Page, capture


def tracked_record(match, **changes):
    record = dict(source=match['source'], title=match['display_title'], url=match['url'],
                  pipeline_item_id='synthetic-item', profile_id='synthetic-owner',
                  status='not_interested', workflow_status='applied', visibility='hidden',
                  state_version=4, reminder_date='2026-10-01', next_action='',
                  workflow_status_provenance='known', unresolved_workflow=False)
    record.update(changes)
    record['match_key'] = local.preview_pipeline_match_key(record)
    return record


class ContinuityReproductionTests(unittest.TestCase):
    def test_hidden_candidate_does_not_consume_final_display_slot(self):
        matches = [dict(make_match(f'Exact role {i}'), job_id=i+1,
                        canonical_opportunity_id=i+1) for i in range(11)]
        record = tracked_record(matches[0])
        context = {'matches': {'do_these_first': matches}}
        page = browser._render_match_results(context, inventory_count=11,
            tracked=local.demo.build_tracked_index([record]))
        self.assertNotIn("id='opportunity-1'", page)
        self.assertIn("id='opportunity-11'", page)
        self.assertEqual(page.count("class='match-card'"), 10)

    def test_same_title_and_canonical_do_not_inherit_state(self):
        first = dict(make_match('Same title'), job_id=101)
        second = dict(first, job_id=102, url='https://example.test/PostingCaseB')
        record = tracked_record(first)
        tracked = local.demo.build_tracked_index([record])
        self.assertIs(local.demo.tracked_record_for_match(first, tracked), record)
        self.assertIsNone(local.demo.tracked_record_for_match(second, tracked))

    def test_my_jobs_has_local_history_return_without_old_run_authority(self):
        record = tracked_record(dict(make_match('Exact role'), job_id=101))
        page = local.render_my_jobs_card(record, 'old-run')
        self.assertIn('/tracker/item?', page)
        self.assertIn('View job details', page)


class AuthenticatedContinuityTests(unittest.TestCase):
    def read_state(self, state):
        with closing(sqlite3.connect(state.database_path)) as connection:
            connection.row_factory=sqlite3.Row
            return [dict(r) for r in connection.execute('SELECT i.*, s.workflow_status, s.visibility, s.reminder_at, s.version '
                'FROM user_pipeline_items i JOIN user_pipeline_state s USING(pipeline_item_id) ORDER BY i.id')]

    def test_save_apply_hide_restore_and_return_across_real_processes(self):
        with synthetic_state(competitors=10) as state:
            client=BrowserClient(state)
            with running_process(state) as first_process:
                client.login()
                matches=client.request('GET','/find-matches')
                self.assertEqual(matches.status,200,matches.body)
                self.assertEqual(matches.body.count(b"class='match-card'"),10)
                script=local.render_inline_action_script().split('<script>',1)[1].split('</script>',1)[0]
                digest=base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
                csp=matches.header_values('Content-Security-Policy')[0]
                self.assertIn("script-src 'sha256-"+digest+"'",csp)
                self.assertIn("connect-src 'self'",csp)
                first_link=next(h for h in Page(matches.body).links if h.startswith('/job/opportunity-7002?'))
                job_id=int(parse_qs(urlsplit(first_link).query)['variant'][0])
                detail=client.request('GET',first_link)
                self.assertEqual(detail.status,200,detail.body)
                save=Page(detail.body).action('save')
                saved=client.submit(save,json_response=True)
                self.assertEqual(saved.status,200,saved.body)
                saved=json.loads(saved.body)
                item_id=saved['pipeline_item_id']
                self.assertTrue(item_id.startswith('pipeline::posting-v1::'+str(job_id)+'::'))
                replay=client.submit(save,json_response=True)
                self.assertTrue(json.loads(replay.body)['replayed'])
                # Another current form rendered before Save cannot overwrite it.
                stale=deepcopy(save);stale['fields']['idempotency_key']+='-conflict'
                self.assertEqual(client.submit(stale).status,409)
                tracker=client.request('GET','/tracker')
                item_link=next(h for h in Page(tracker.body).links if h.startswith('/tracker/item?'))
                detail=client.request('GET',item_link)
                self.assertIn(b'Original listing',detail.body)
                reminded=client.submit(Page(detail.body).action('remind_later'),json_response=True)
                self.assertEqual(reminded.status,200,reminded.body)
                detail=client.request('GET',item_link)
                applied=client.submit(Page(detail.body).action('applied'),json_response=True)
                self.assertEqual(applied.status,200,applied.body)
                self.assertIn('Applied',json.loads(applied.body)['workflow_history_html'])
                before=self.read_state(state)[0]
                self.assertEqual(before['workflow_status'],'applied')
                self.assertTrue(before['reminder_at'])
                old_run=parse_qs(urlsplit(first_link).query)['run'][0]
                pid=first_process.pid
            with running_process(state) as second_process:
                self.assertNotEqual(pid,second_process.pid)
                # Real delivered session survives the process; no fabricated cookie/session.
                tracker=client.request('GET','/tracker?run='+old_run)
                self.assertEqual(tracker.status,200,tracker.body)
                self.assertIn(b'Applied',tracker.body)
                self.assertIn(b'Reminder set',tracker.body)
                detail=client.request('GET',item_link)
                hide=Page(detail.body).action('not_interested')
                hidden=client.submit(hide,json_response=True)
                self.assertEqual(hidden.status,200,hidden.body)
                matches=client.request('GET','/find-matches')
                self.assertEqual(matches.status,200,matches.body)
                self.assertNotIn(f"id='opportunity-{job_id}'".encode(),matches.body)
                other=7006 if job_id==7003 else 7003
                self.assertIn(f"id='opportunity-{other}'".encode(),matches.body)
                self.assertEqual(matches.body.count(b"class='match-card'"),10)
                current_run=parse_qs(urlsplit(next(h for h in Page(matches.body).links if h.startswith('/job/'))).query)['run'][0]
                reused=client.request('GET','/find-matches?run='+current_run)
                self.assertNotIn(f"id='opportunity-{job_id}'".encode(),reused.body)
                unchanged=self.read_state(state)[0]
                self.assertEqual(unchanged['workflow_status'],'applied')
                self.assertEqual(unchanged['reminder_at'],before['reminder_at'])
                self.assertEqual(client.submit(hide,json_response=True).status,200)
            with running_process(state):
                current=client.request('GET','/find-matches')
                self.assertNotIn(f"id='opportunity-{job_id}'".encode(),current.body)
                tracker=client.request('GET','/tracker?view=hidden')
                self.assertIn(b'Applied',tracker.body)
                detail=client.request('GET',item_link)
                self.assertIn(b'Application history',detail.body)
                shown=client.submit(Page(detail.body).action('show_again'),json_response=True)
                self.assertEqual(shown.status,200,shown.body)
                restored=self.read_state(state)[0]
                self.assertEqual((restored['workflow_status'],restored['visibility'],restored['reminder_at']),
                                 ('applied','visible',before['reminder_at']))
                other_detail=client.request('GET',f'/job/opportunity-7002?variant={other}')
                self.assertIn(b'>Save</button>',other_detail.body)
                self.assertNotIn(b"Current status: Applied",other_detail.body)
                saved_other=client.submit(Page(other_detail.body).action('save'),json_response=True)
                self.assertEqual(saved_other.status,200,saved_other.body)
                records=self.read_state(state)
                self.assertEqual(len(records),2)
                self.assertEqual([r['workflow_status'] for r in records],['applied','saved'])
                self.assertNotEqual(records[0]['pipeline_item_id'],records[1]['pipeline_item_id'])

    def test_current_source_controls_conflicts_and_capture_independent_history(self):
        with synthetic_state() as state:
            client=BrowserClient(state)
            with running_process(state):
                client.login()
                url='/job/opportunity-7002?variant=7003'
                page=client.request('GET',url)
                save=Page(page.body).action('save')
                no_csrf=client.submit(save,csrf=False,json_response=True)
                self.assertEqual(no_csrf.status,403)
                self.assertEqual(self.read_state(state),[])
                wrong_return=deepcopy(save);wrong_return['fields']['return_to']='/job/opportunity-7002?variant=7006'
                self.assertEqual(client.submit(wrong_return,json_response=True).status,400)
                response=client.submit(save,json_response=True)
                self.assertEqual(response.status,200,response.body)
                item_id=json.loads(response.body)['pipeline_item_id']
                item_url='/tracker/item?'+__import__('urllib.parse',fromlist=['urlencode']).urlencode({'item':item_id})
                page=client.request('GET',item_url)
                applied_form=Page(page.body).action('applied')
                reminder_form=Page(page.body).action('remind_later')
                response=client.submit(applied_form,json_response=True)
                self.assertEqual(response.status,200,response.body)
                self.assertEqual(client.submit(reminder_form,json_response=True).status,409)
                self.assertEqual(client.submit(applied_form,json_response=True).status,200)
                self.assertTrue(json.loads(client.submit(applied_form,json_response=True).body)['replayed'])
                old_card=client.request('GET','/find-matches')
                before=self.read_state(state)[0]
                with closing(sqlite3.connect(state.database_path)) as c, c:
                    c.row_factory=sqlite3.Row
                    old_capture=c.execute('SELECT accepted_capture_id FROM job_source_content_acceptances WHERE job_id=7003').fetchone()[0]
                    result=capture(c,7003,(state.clock()+timedelta(seconds=1)).isoformat())
                    new_capture=c.execute('SELECT accepted_capture_id FROM job_source_content_acceptances WHERE job_id=7003').fetchone()[0]
                    self.assertNotEqual(old_capture,new_capture)
                after=self.read_state(state)[0]
                self.assertEqual(before,after)
                # Any old source-bound control now fails; the history key survives.
                self.assertEqual(client.submit(save,json_response=True).status,409)
                detail=client.request('GET',url)
                self.assertIn(b'Current status: Applied',detail.body)
                with closing(sqlite3.connect(state.database_path)) as c, c:
                    c.row_factory=sqlite3.Row
                    capture(c,7003,(state.clock()+timedelta(seconds=2)).isoformat(),
                        title='Updated Python Evaluation Role',url='https://jobs.example.test/UpdatedPostingA')
                detail=client.request('GET',item_url)
                self.assertEqual(detail.status,200,detail.body)
                self.assertIn(b'Updated Python Evaluation Role',detail.body)
                self.assertIn(b'Current status: Applied',detail.body)
                with closing(sqlite3.connect(state.database_path)) as c, c:
                    c.execute('UPDATE jobs SET is_active=0,removed_at=? WHERE id=7003',(state.clock().isoformat(),))
                detail=client.request('GET',item_url)
                self.assertEqual(detail.status,200,detail.body)
                self.assertIn(b'Listing marked inactive',detail.body)
                self.assertIn(b'Current status: Applied',detail.body)
                self.assertIn(b'Application history',detail.body)
                self.assertNotIn(b'Apply on company site</a>',detail.body)
                hidden=client.submit(Page(detail.body).action('not_interested'),json_response=True)
                self.assertEqual(hidden.status,200,hidden.body)
                detail=client.request('GET',item_url)
                self.assertEqual(client.submit(Page(detail.body).action('show_again'),json_response=True).status,200)
                self.assertNotIn(b"id='opportunity-7003'",client.request('GET','/find-matches').body)
                with closing(sqlite3.connect(state.database_path)) as c, c:
                    c.execute('UPDATE jobs SET is_active=1,removed_at=NULL WHERE id=7003')
                detail=client.request('GET',item_url)
                self.assertIn(b'Current status: Applied',detail.body)
                self.assertNotIn(b'Listing marked inactive',detail.body)
                self.assertEqual(self.read_state(state)[0]['pipeline_item_id'],item_id)

    def test_another_authenticated_owner_cannot_read_or_mutate_private_history(self):
        with synthetic_state() as state:
            first=BrowserClient(state)
            with running_process(state):
                first.login()
                page=first.request('GET','/job/opportunity-7002?variant=7003')
                self.assertEqual(first.submit(Page(page.body).action('applied'),json_response=True).status,200)
                record=self.read_state(state)[0]
                item_url=local.tracker_item_url(dict(pipeline_item_id=record['pipeline_item_id']))
                page=first.request('GET',item_url)
                first_form=Page(page.body).action('not_interested')
            second=BrowserClient(state)
            with running_process(state,owner='second'):
                second.login()
                denied=second.request('GET',item_url)
                self.assertEqual(denied.status,404)
                self.assertNotIn(b'Applied',denied.body)
                self.assertNotIn(b'Python Backend',second.request('GET','/tracker').body)
                detail=second.request('GET','/job/opportunity-7002?variant=7003')
                self.assertIn(b'>Save</button>',detail.body)
                form=Page(detail.body).action('save')
                form['fields'].update(pipeline_item_id=record['pipeline_item_id'],expected_version=str(record['version']))
                denied=second.submit(form,json_response=True)
                self.assertEqual(denied.status,403,denied.body)
                self.assertEqual(second.submit(first_form,json_response=True).status,410)
                self.assertEqual(self.read_state(state),[record])

    def test_conditional_card_actions_hide_and_restore_without_primary_promotion(self):
        with synthetic_state(conditional=True) as state:
            client=BrowserClient(state)
            with running_process(state):
                client.login()
                response=client.request('GET','/find-matches')
                self.assertEqual(response.status,200,response.body)
                self.assertNotIn(b"class='match-card'",response.body)
                self.assertIn(b'Possibilities with conditions',response.body)
                page=Page(response.body)
                link=next(h for h in page.links if h.startswith('/job/opportunity-7002?'))
                job_id=int(parse_qs(urlsplit(link).query)['variant'][0])
                hidden=client.submit(page.action('not_interested'),json_response=True)
                self.assertEqual(hidden.status,200,hidden.body)
                self.assertEqual(json.loads(hidden.body)['matches_refresh_url'],'/find-matches')
                item_id=json.loads(hidden.body)['pipeline_item_id']
                current=client.request('GET','/find-matches')
                self.assertNotIn(f"id='opportunity-{job_id}'".encode(),current.body)
                self.assertIn(b'Possibilities with conditions',current.body)
                run=parse_qs(urlsplit(next(h for h in Page(current.body).links if h.startswith('/job/'))).query)['run'][0]
                self.assertNotIn(f"id='opportunity-{job_id}'".encode(),client.request('GET','/find-matches?run='+run).body)
                detail=client.request('GET',local.tracker_item_url({'pipeline_item_id':item_id}))
                shown=client.submit(Page(detail.body).action('show_again'),json_response=True)
                self.assertEqual(shown.status,200,shown.body)
                current=client.request('GET','/find-matches')
                self.assertNotIn(b"class='match-card'",current.body)
                self.assertIn(b'Possibilities with conditions',current.body)

    def test_legacy_ambiguous_and_unlinked_history_have_bounded_returns(self):
        from wahojobs import pipeline_actions
        with synthetic_state() as state:
            # Supported legacy orchestration creates preserved historical rows;
            # no updates/deletes of transition or normalized history.
            with closing(sqlite3.connect(state.database_path)) as c, c:
                c.row_factory=sqlite3.Row
                c.execute("INSERT INTO user_profiles (user_id,profile_id,display_name,is_sample) VALUES (?,?,'Synthetic legacy owner',0)",
                          (state.account_id,state.profile_id))
                for index,url in enumerate(('https://jobs.example.test/PostingA','https://jobs.example.test/shared','')):
                    pipeline_actions.perform_pipeline_action(c,action='applied',owner_profile_id=state.profile_id,
                        idempotency_key=f'synthetic-legacy-{index}',match_run_id='synthetic-legacy-setup',source='Continuity Demo Jobs',
                        title=f'Preserved legacy history {index}',url=url,canonical_id=7002,
                        opportunity_external_id='configured-distinctive-7003' if index==0 else '')
                for job_id in (7003,7006):
                    capture(c,job_id,(state.clock()+timedelta(seconds=1)).isoformat(),url='https://jobs.example.test/shared')
            before=self.read_state(state)
            client=BrowserClient(state)
            with running_process(state):
                client.login()
                tracker=client.request('GET','/tracker')
                links=[h for h in Page(tracker.body).links if h.startswith('/tracker/item?')]
                self.assertEqual(len(links),3)
                rendered=[client.request('GET',h) for h in links]
                self.assertTrue(all(r.status==200 for r in rendered),[(r.status,r.body[-1500:]) for r in rendered])
                self.assertEqual(sum(b'cannot be linked safely' in r.body for r in rendered),2)
                self.assertTrue(all(b'Applied' in r.body for r in rendered))
                self.assertEqual(self.read_state(state),before)

    def test_expired_source_and_lost_local_link_preserve_private_history(self):
        from tests.google_oidc_gateway_test_support import ManualClock
        with synthetic_state() as state:
            client=BrowserClient(state)
            with running_process(state):
                client.login()
                page=client.request('GET','/job/opportunity-7002?variant=7003')
                self.assertEqual(client.submit(Page(page.body).action('applied'),json_response=True).status,200)
                record=self.read_state(state)[0]
            marker_path=state.directory/'continuity-demo.json'
            marker=json.loads(marker_path.read_text())
            state.clock=ManualClock(state.clock()+timedelta(days=45))
            marker['now']=state.clock().isoformat()
            marker_path.write_text(json.dumps(marker),encoding='utf-8')
            client=BrowserClient(state)
            with running_process(state):
                client.login()
                item_url=local.tracker_item_url(record)
                page=client.request('GET',item_url)
                self.assertEqual(page.status,200,page.body)
                self.assertIn(b'Applied',page.body)
                self.assertNotIn(b'Apply on company site</a>',page.body)
                self.assertNotIn(b"id='opportunity-7003'",client.request('GET','/find-matches').body)
                with closing(sqlite3.connect(state.database_path)) as c, c:
                    # A posting can outlive its local canonical membership.
                    c.execute('UPDATE jobs SET canonical_opportunity_id=NULL WHERE id=7003')
                page=client.request('GET',item_url)
                self.assertEqual(page.status,200,page.body)
                self.assertIn(b'cannot be linked safely',page.body)
                self.assertIn(b'Application history',page.body)
                self.assertEqual(self.read_state(state),[record])
                self.assertEqual(client.submit(Page(page.body).action('not_interested'),json_response=True).status,200)
                page=client.request('GET',item_url)
                self.assertEqual(client.submit(Page(page.body).action('show_again'),json_response=True).status,200)
                self.assertEqual(self.read_state(state)[0]['workflow_status'],'applied')

    def test_legacy_duplicate_histories_and_opaque_identity_remain_separate(self):
        from wahojobs import pipeline_actions, pipeline_postings
        with synthetic_state() as state:
            with closing(sqlite3.connect(state.database_path)) as c, c:
                c.row_factory=sqlite3.Row
                c.execute("INSERT INTO user_profiles (user_id,profile_id,display_name,is_sample) VALUES (?,?,'Synthetic legacy owner',0)",
                          (state.account_id,state.profile_id))
                for index in range(2):
                    pipeline_actions.perform_pipeline_action(c,action='applied',owner_profile_id=state.profile_id,
                        idempotency_key=f'legacy-separate-{index}',match_run_id='synthetic-legacy-setup',
                        source='Continuity Demo Jobs',title=f'Legacy record {index}',
                        url=f'https://jobs.example.test/historical-{index}',opportunity_external_id='PostingB')
                posting=pipeline_postings.load_posting(c,7006)
                key=pipeline_postings.item_id(state.profile_id,posting)
                self.assertEqual(key,pipeline_postings.item_id(state.profile_id,dict(posting,
                    title='New title',url='https://jobs.example.test/new',canonical_opportunity_id=None)))
                self.assertNotEqual(key,pipeline_postings.item_id(state.profile_id,dict(posting,source_hash='Synthetic-posting-B')))
                self.assertNotEqual(key,pipeline_postings.item_id('another-owner',posting))
                wrong_case=dict(pipeline_item_id='legacy-unknown',profile_id=state.profile_id,
                    source='Continuity Demo Jobs',external_id='postingb',url=posting['url'])
                self.assertEqual(pipeline_postings.resolve_record(c,wrong_case),(None,'unresolved_legacy'))
            before=self.read_state(state)
            client=BrowserClient(state)
            with running_process(state):
                client.login()
                page=client.request('GET','/job/opportunity-7002?variant=7006')
                self.assertIn(b'Separate histories',page.body)
                self.assertNotIn(b'>Save</button>',page.body)
                for record in before:
                    page=client.request('GET',local.tracker_item_url(record))
                    self.assertIn(b'Each history is kept separately',page.body)
                    self.assertIn(b'Applied',page.body)
                self.assertEqual(self.read_state(state),before)
                page=client.request('GET',local.tracker_item_url(before[0]))
                self.assertEqual(client.submit(Page(page.body).action('not_interested'),json_response=True).status,200)
                self.assertNotIn(b"id='opportunity-7006'",client.request('GET','/find-matches').body)
                self.assertEqual(self.read_state(state)[1],before[1])

    def test_private_exact_return_survives_enabled_public_redirect_and_gone_routes(self):
        from scripts.workos_authkit_provider_migration import apply_workos_authkit_provider_migration
        from scripts.public_job_identity_migration import apply_public_job_identity_migration
        from wahojobs.public_job_canary import PublicJobCanaryRoutingGate
        from wahojobs.public_job_identity import PublicJobIdAllocator, allocate_public_job, mark_public_job_gone
        with synthetic_state() as state:
            client=BrowserClient(state)
            with running_application(state):
                client.login()
                page=client.request('GET','/job/opportunity-7002?variant=7006')
                saved=client.submit(Page(page.body).action('applied'),json_response=True)
                self.assertEqual(saved.status,200,saved.body)
                item_url=local.tracker_item_url(json.loads(saved.body))
            with closing(sqlite3.connect(state.database_path)) as c, c:
                c.row_factory=sqlite3.Row
                c.execute('PRAGMA foreign_keys=ON')
                apply_workos_authkit_provider_migration(c)
                apply_public_job_identity_migration(c)
                allocation=allocate_public_job(c,allocator=PublicJobIdAllocator('continuity-synthetic'),
                    company_slug='configured-production',canonical_title='Python Backend AI Coding Evaluator',
                    canonical_opportunity_id=7002,primary_path='/job/continuity-synthetic-public',now=state.clock())
            gate=PublicJobCanaryRoutingGate((allocation.public_job_id,))
            with running_application(state,canary_gate=gate):
                redirect=client.request('GET','/job/opportunity-7002')
                self.assertEqual(redirect.status,301)
                exact=client.request('GET','/job/opportunity-7002?variant=7006')
                self.assertEqual(exact.status,200,exact.body)
                self.assertIn(b'https://jobs.example.test/PostingB',exact.body)
                self.assertNotIn(b'https://jobs.example.test/PostingA',exact.body)
                page=client.request('GET',item_url)
                self.assertEqual(page.status,200,page.body)
                self.assertEqual(page.header_values('Location'),())
                self.assertIn(b'https://jobs.example.test/PostingB',page.body)
                self.assertNotIn(b'https://jobs.example.test/PostingA',page.body)
                self.assertIn(b'Applied',page.body)
                with closing(sqlite3.connect(state.database_path)) as c, c:
                    c.row_factory=sqlite3.Row
                    c.execute('PRAGMA foreign_keys=ON')
                    mark_public_job_gone(c,allocation.public_job_id,now=state.clock())
                page=client.request('GET',item_url)
                self.assertEqual(page.status,200,page.body)
                self.assertIn(b'Applied',page.body)
                self.assertIn(b'https://jobs.example.test/PostingB',page.body)

    def test_hide_between_workflow_read_and_source_selection_cannot_receive_current_proof(self):
        with synthetic_state(competitors=10) as state, running_application(state) as integration:
            client=BrowserClient(state)
            client.login()
            page=client.request('GET','/job/opportunity-7002?variant=7003')
            saved=client.submit(Page(page.body).action('save'),json_response=True)
            self.assertEqual(saved.status,200,saved.body)
            item_url=local.tracker_item_url(json.loads(saved.body))
            for mode in ('fresh','reused'):
                with self.subTest(mode=mode):
                    current=client.request('GET','/find-matches')
                    link=next(h for h in Page(current.body).links if h.startswith('/job/'))
                    run=parse_qs(urlsplit(link).query)['run'][0]
                    hide=Page(client.request('GET',item_url).body).action('not_interested')
                    original=integration._load_pipeline_records
                    committed=[]
                    def read_then_hide(authority):
                        records=original(authority)
                        if not committed:
                            committed.append(True)
                            mutation=client.submit(hide,json_response=True)
                            self.assertEqual(mutation.status,200,mutation.body)
                        return records
                    with patch.object(type(integration),'_load_pipeline_records',side_effect=read_then_hide):
                        raced=client.request('GET','/find-matches'+('?run='+run if mode=='reused' else ''))
                    self.assertEqual(raced.status,503,raced.body)
                    self.assertNotIn(b"id='opportunity-7003'",raced.body)
                    current=client.request('GET','/find-matches')
                    self.assertEqual(current.status,200,current.body)
                    self.assertNotIn(b"id='opportunity-7003'",current.body)
                    page=client.request('GET',item_url)
                    self.assertEqual(client.submit(Page(page.body).action('show_again'),json_response=True).status,200)



if __name__ == '__main__':
    unittest.main()
