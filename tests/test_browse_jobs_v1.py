"""Browse regressions use disposable inventory and the existing authenticated routes."""
from copy import deepcopy
from contextlib import closing
from html import unescape
import json
import re
import sqlite3
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from wahojobs import public_jobs_catalog as catalog, public_job_page
from tests import test_public_jobs_catalog as catalog_tests
from tests.test_public_jobs_catalog import NOW, ORIGIN
from tests.test_public_job_page import seed_public_job, JOB_PATH, OBSERVED_AT
from tests import test_authenticated_candidate_workflow as workflow_tests


def copy_row(connection, table, source, **changes):
    row = dict(source)
    row.update(changes)
    connection.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))


class BrowseCatalogTests(unittest.TestCase):
    setUp = catalog_tests.PublicJobsCatalogTests.setUp
    load = catalog_tests.PublicJobsCatalogTests.load
    integration = catalog_tests.PublicJobsCatalogTests.integration

    def variants(self):
        with closing(sqlite3.connect(self.path)) as c, c:
            c.row_factory = sqlite3.Row
            original = c.execute('SELECT * FROM jobs WHERE id=9003').fetchone()
            copy_row(c, 'jobs', original, id=9005, external_id='portugal-rust', title='Rust evaluator',
                     location='Remote — Portugal', department='', expertise='Rust',
                     url='https://apply.example.test/portugal-rust', source_hash='portugal-rust')
        return self.load()

    def test_filter_before_representative_and_no_cross_variant_facts(self):
        jobs = self.variants()
        pt = catalog.build_catalog(jobs, {'location': 'Portugal'})
        self.assertEqual(pt['result_count'], 1)
        self.assertEqual(pt['jobs'][0]['job_id'], 9005)
        self.assertEqual(pt['jobs'][0]['catalog_location'], 'Eligible in Portugal')
        # Shared canonical Brazil, English and Python do not decorate Portugal.
        for filters in ({'location': 'Portugal', 'q': 'Python'},
                        {'location': 'Portugal', 'language': 'English'}):
            self.assertEqual(catalog.build_catalog(jobs, filters)['result_count'], 0)
        self.assertEqual(catalog.build_catalog(jobs, {'location': 'Portugal', 'q': 'Rust'})['result_count'], 1)
        self.assertEqual(catalog.build_catalog(jobs)['result_count'], 1)
        self.assertTrue(all(option['count'] == 1 for option in pt['facets']['location']))
        for signed_in in (False, True):
            page = catalog.render_public_jobs_page(pt, public_origin=ORIGIN, authenticated=signed_in)
            self.assertIn('variant=9005', page)
            self.assertIn('return_to=%2Fjobs%3Flocation%3DPortugal', page)
        detail = self.integration().handle('GET', JOB_PATH + '?' + urlencode({'variant': 9005, 'return_to': '/jobs?location=Portugal'}), (('Host', 'app.test'),))
        self.assertEqual(detail.status, 200)
        self.assertIn('Rust evaluator', detail.body.decode())

    def test_scoped_facts_conjunctions_and_shared_document_preservation(self):
        from wahojobs.opportunity_enrichment import make_variant_fact, normalize_variant_facts, scoped_fact_evidence
        self.variants()
        facts = []
        for reference, language, domain in [('source_hash:source-hash-9003','English','technical'),
                                             ('source_hash:portugal-rust','Portuguese','biology')]:
            evidence = scoped_fact_evidence(source_ref=reference, kind='source_body', label='fixture',
                evidence_text=language+' '+domain, basis='deterministic_parse', confidence='high')
            for path, value in [('attributes.requirements.languages', dict(language=language,locale=None,requirement_mode='single')),
                                ('attributes.role.professional_domains', domain)]:
                facts.append(make_variant_fact(path,value,[reference],[evidence]))
        with closing(sqlite3.connect(self.path)) as c, c:
            document = json.loads(c.execute('SELECT automatic_document_json FROM opportunity_enrichments').fetchone()[0])
            document['variant_facts'] = normalize_variant_facts(facts)
            c.execute('UPDATE opportunity_enrichments SET automatic_document_json=?',(json.dumps(document),))
        jobs = self.load()
        for key, positive, negative in [('q','Rust','Python'),('language','Portuguese','English'),('field','Biology','Software engineering')]:
            result = catalog.build_catalog(jobs,{'location':'Portugal',key:positive})
            self.assertEqual(result['result_count'],1)
            self.assertEqual(result['jobs'][0]['job_id'],9005)
            self.assertEqual(catalog.build_catalog(jobs,{'location':'Portugal',key:negative})['result_count'],0)
        portuguese = catalog.build_catalog(jobs,{'location':'Portugal'})
        self.assertEqual([(x['label'],x['count']) for x in portuguese['facets']['language']],[('Portuguese',1)])
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            evidence = public_job_page.load_public_job_evidence(c, JOB_PATH)
        original = deepcopy(evidence)
        for posting in (9003,9005):
            catalog.prepare_catalog_presentation(public_job_page.prepare_public_job(evidence,selected_job_id=posting,now=NOW))
        self.assertEqual(evidence,original)

    def test_inactive_variant_cannot_complete_filter_conjunction(self):
        self.variants()
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute('UPDATE jobs SET is_active=0 WHERE id=9005')
        self.assertEqual(catalog.build_catalog(self.load(), {'location': 'Portugal'})['result_count'], 0)
        self.assertEqual(catalog.build_catalog(self.load())['result_count'], 1)

    def test_batch_projection_matches_details_and_scales_by_distinct_facts(self):
        from wahojobs.opportunity_enrichment import (
            make_variant_fact, project_variant_facts, scoped_fact_evidence,
        )
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            evidence = public_job_page.load_public_job_evidence(c, JOB_PATH)
        template = evidence['rows'][0]
        evidence['rows'] = [dict(template, job_id=10000+i, source_hash=f'batch-{i}') for i in range(120)]
        references = ['source_hash:' + row['source_hash'] for row in evidence['rows']]
        proof = scoped_fact_evidence(source_ref=references[0], kind='source_body', label='fixture',
            evidence_text='Explicit requirements', basis='deterministic_parse', confidence='high')
        facts = [
            make_variant_fact('attributes.requirements.skills_required', 'Python', references[:119], [proof]),
            make_variant_fact('attributes.work_arrangement.workplace_mode', 'remote', references[:119], [proof]),
            # Scalar conflict, known-empty conflict, and mixed language modes
            # must keep exactly the standalone projection's behavior.
            make_variant_fact('attributes.work_arrangement.workplace_mode', 'onsite', references[60:119], [proof]),
            make_variant_fact('attributes.requirements.skills_required', None, references[60:119], [proof], knowledge_state='known_empty'),
        ]
        for mode in ('single', 'all_required'):
            facts.append(make_variant_fact('attributes.requirements.languages',
                dict(language='English', locale=None, requirement_mode=mode), references[60:119], [proof]))
        evidence['effective']['document']['variant_facts'] = facts
        original = deepcopy(evidence)
        expected = [public_job_page.prepare_public_job(evidence, selected_job_id=row['job_id'], now=NOW)
                    for row in evidence['rows']]
        for job in expected:
            job['enrichment']['field_evidence'] = []
            for fact in job['enrichment']['variant_facts']:
                fact['evidence'] = []
        with patch('wahojobs.opportunity_enrichment.project_variant_facts', wraps=project_variant_facts) as project:
            actual = public_job_page.prepare_public_job_variants(evidence, now=NOW)
        self.assertEqual(actual, expected)
        self.assertEqual(project.call_count, 3)  # 120 variants, three exact fact sets (including unknown).
        self.assertEqual(evidence, original)
        self.assertIs(actual[0]['enrichment'], actual[59]['enrichment'])
        self.assertIsNot(actual[0]['enrichment'], actual[60]['enrichment'])
        # A later snapshot must not reuse a projection from the prior load.
        evidence['effective']['document']['variant_facts'] = []
        refreshed = public_job_page.prepare_public_job_variants(evidence, now=NOW)
        self.assertEqual(refreshed[0]['enrichment']['attributes']['requirements']['skills_required'], [])

    def test_country_worldwide_region_unknown_and_remote_are_distinct(self):
        base = self.load()[0]
        jobs = []
        for number, (place, scope, countries, regions) in enumerate([
            ('Brazil', 'remote_restricted', ['Brazil'], []),
            ('Worldwide', 'remote_worldwide', [], []),
            ('Americas', 'remote_restricted', [], ['Americas']),
            ('Remote', 'unknown', [], []),
            ('Portugal', 'remote_restricted', ['Portugal'], []),
        ], 100):
            job = deepcopy(base)
            job.update(job_id=number, canonical_opportunity_id=number, source_location=place)
            job['enrichment']['attributes']['work_arrangement'].update(
                workplace_mode='remote', location_scope=scope, eligible_countries=countries,
                eligible_regions=regions, eligible_locations=[])
            catalog.prepare_catalog_presentation(job)
            jobs.append(job)
        for place, count in [('Brazil', 3), ('Americas', 3), ('Portugal', 2), ('EMEA', 2), ('Worldwide', 1), ('Remote', 0)]:
            self.assertEqual(catalog.build_catalog(jobs, {'location': place})['result_count'], count, place)
        self.assertEqual(catalog.build_catalog(jobs)['result_count'], 5)
        self.assertIn('unconfirmed', jobs[3]['catalog_location'])

    def test_facet_predicate_work_does_not_multiply_by_number_of_options(self):
        jobs = self.variants()[0]['_catalog_variants']
        for job in jobs:
            job['_catalog_filter_labels']['work'] = {f'task-{i}': f'Task {i}' for i in range(100)}
            job['_catalog_filter_values']['work'] = set(job['_catalog_filter_labels']['work'])
        with patch.object(catalog, 'catalog_job_matches', wraps=catalog.catalog_job_matches) as matches:
            facets = catalog.catalog_facets(jobs, filters={'location': 'Portugal'},
                                           resolved_filters={'location': 'portugal'})
        self.assertEqual(len(facets['work']), 100)
        self.assertEqual({option['count'] for option in facets['work']}, {1})
        self.assertLessEqual(matches.call_count, len(jobs) * len(catalog.CATALOG_FACET_KEYS))

    def test_search_includes_explicit_skills_and_bound_description(self):
        with closing(sqlite3.connect(self.path)) as c, c:
            raw = c.execute('SELECT automatic_document_json FROM opportunity_enrichments').fetchone()[0]
            doc = json.loads(raw)
            doc['attributes']['requirements']['skills_required'] = sorted([*doc['attributes']['requirements']['skills_required'], 'Kubernetes'], key=str.casefold)
            doc['attributes']['requirements']['skills_preferred'].append('Tableau')
            c.execute('UPDATE opportunity_enrichments SET automatic_document_json=?', (json.dumps(doc),))
            c.execute("UPDATE job_source_contents SET body='Use Blender for visual work.'")
        for term in ('Kubernetes', 'Tableau', 'Blender', 'Acme'):
            self.assertEqual(catalog.build_catalog(self.load(), {'q': term})['result_count'], 1, term)
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE job_source_contents SET external_id='unrelated'")
        self.assertEqual(catalog.build_catalog(self.load(), {'q': 'Blender'})['result_count'], 0)

    def test_pagination_counts_distinct_canonicals_and_recovers_without_losing_filters(self):
        base = self.load()[0]
        jobs = []
        for number in range(65):
            job = deepcopy(base)
            job.update(job_id=10000+number, canonical_opportunity_id=10000+number)
            catalog.prepare_catalog_presentation(job)
            duplicate = deepcopy(job)
            duplicate['job_id'] += 100
            job['_catalog_variants'] = (dict(job), duplicate)
            jobs.append(job)
        pages = [catalog.build_catalog(jobs, {'q': 'Python', 'location': 'Brazil', 'page': n}) for n in (1,2,3)]
        ids = [job['canonical_opportunity_id'] for page in pages for job in page['jobs']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 65)
        self.assertEqual([page['page_result_count'] for page in pages], [30,30,5])
        high = catalog.build_catalog(jobs, {'q': 'Python', 'location': 'Brazil', 'page': 99})
        self.assertEqual(high['page'], 3)
        self.assertEqual(high['normalized_target'], '/jobs?q=Python&location=Brazil&page=3')
        html = catalog.render_public_jobs_page(pages[1], public_origin=ORIGIN, authenticated=True)
        self.assertNotIn("name='page'", html)
        self.assertIn('return_to=%2Fjobs%3Fq%3DPython%26location%3DBrazil%26page%3D2', html)

    def test_active_filters_clear_one_and_errors_are_not_empty_results(self):
        integration = self.integration()
        target = '/jobs?q=missing&location=Brazil&arrangement=Contract'
        response = integration.handle('GET', target, (('Host', 'app.test'),))
        body = response.body.decode()
        self.assertEqual(response.status, 200)
        self.assertIn('No jobs match these filters', body)
        self.assertIn("name='arrangement'", body)
        self.assertIn("class='secondary-filters' open", body)
        self.assertIn("href='/jobs?q=missing&amp;arrangement=Contract'", body)
        self.assertIn("href='/jobs'>Clear filters", body)
        with patch.object(type(integration), '_load_public_jobs_inventory', side_effect=sqlite3.OperationalError):
            failed = integration.handle('GET', '/jobs', (('Host', 'app.test'),))
        self.assertEqual(failed.status, 503)
        self.assertNotIn('No jobs match', failed.body.decode())


class BrowseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.h = workflow_tests.AuthenticatedCandidateWorkflowTests('test_public_job_page_reuses_authenticated_pipeline_and_returns_to_job')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        seed_public_job(self.h.connection)
        self.h.connection.execute("UPDATE job_source_contents SET provider='acme-ai'")
        self.h.connection.commit()

    def test_catalog_details_save_return_and_account_isolation_without_matching(self):
        h = self.h
        second = h._seed_account('42')
        target = JOB_PATH + '?' + urlencode({'variant': 9003, 'return_to': '/jobs?q=Python&location=Brazil'})
        with patch('wahojobs.authenticated_variant_details.resolve_scoped_variant', side_effect=AssertionError('no matching for Browse')):
            page = h.integration.handle('GET', target, h._headers(h.first))
        self.assertEqual(page.status, 200, page.body)
        body = page.body.decode()
        self.assertIn('Back to Browse jobs', body)
        self.assertIn("href='/jobs?q=Python&amp;location=Brazil'", body)
        self.assertIn('Build evaluation systems', body)
        self.assertNotIn('Personalize your Wahojobs recommendations</h2>', body)
        self.assertNotIn('Why this may fit', body)
        self.assertEqual(h.connection.execute('SELECT COUNT(*) FROM user_pipeline_items').fetchone()[0], 0)
        form_html = re.search(r'<form[^>]+action-form-save[^>]*>(.*?)</form>', body, re.S).group(1)
        form = {k: unescape(v) for k,v in re.findall(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', form_html)}
        rejected = h._post(second, second, form, accept_json=False)
        self.assertGreaterEqual(rejected.status, 400)
        saved = h._post(h.first, h.first, form, accept_json=False)
        self.assertEqual(saved.status, 303, saved.body)
        self.assertEqual(dict(saved.headers)['Location'], target)
        self.assertEqual(h.connection.execute('SELECT COUNT(*) FROM user_pipeline_items').fetchone()[0], 1)
        reloaded = h.integration.handle('GET', target, h._headers(h.first)).body.decode()
        self.assertIn('Current status: Saved', reloaded)
        applied_html = re.search(r'<form[^>]+action-form-applied[^>]*>(.*?)</form>', reloaded, re.S).group(1)
        applied_form = {k: unescape(v) for k,v in re.findall(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', applied_html)}
        applied = h._post(h.first, h.first, applied_form, accept_json=False)
        self.assertEqual(applied.status, 303)
        self.assertEqual(dict(applied.headers)['Location'], target)
        browse = h.integration.handle('GET', '/jobs', h._headers(h.first)).body.decode()
        self.assertIn("class='catalog-saved'>Applied", browse)
        other = h.integration.handle('GET', '/jobs', h._headers(second)).body.decode()
        self.assertNotIn("class='catalog-saved'>Applied", other)
        tracker = h.integration.handle('GET', '/tracker', h._headers(h.first)).body.decode()
        self.assertIn('Applied AI Engineer', tracker)
        h.integration.handle('GET', JOB_PATH, h._headers(h.first))
        self.assertEqual(h.connection.execute('SELECT COUNT(*) FROM user_pipeline_items').fetchone()[0], 1)


    def test_signed_in_empty_profile_can_browse_and_read_source_details(self):
        from datetime import timedelta
        from wahojobs import accounts
        from tests.persistent_profiles_test_support import add_account_principal
        h = self.h
        principal = add_account_principal(h.connection, '49', environment='private_beta')
        account = h.connection.execute('SELECT user_id FROM principal_account_bindings WHERE principal_id=?', (principal,)).fetchone()[0]
        session = accounts.create_session(h.connection, user_id=account,
            idle_ttl=timedelta(hours=2), absolute_ttl=timedelta(hours=8), idempotency_key='browse-no-profile', now=h.clock[0])
        state = dict(session_token=session.session_token, csrf=session.csrf_secret)
        with patch('wahojobs.authenticated_variant_details.resolve_scoped_variant', side_effect=AssertionError('must not match')):
            for target in ['/jobs', JOB_PATH + '?' + urlencode({'variant': 9003, 'return_to': '/jobs'}), '/company/acme-ai', JOB_PATH]:
                response = h.integration.handle('GET', target, h._headers(state))
                self.assertEqual(response.status, 200, target)
                self.assertEqual(dict(response.headers)['Cache-Control'], 'no-store')
                body = response.body.decode()
                self.assertIn('Sign out', body)
                self.assertNotIn('>Sign in</a>', body)
                if target.startswith('/job/'):
                    self.assertIn("href='/jobs'>← Back to Browse jobs", body)
                    self.assertIn('Build evaluation systems', body)
                    self.assertNotIn('action-form-save', body)
        self.assertEqual(h.connection.execute('SELECT COUNT(*) FROM user_pipeline_items').fetchone()[0], 0)



class BrowseHTTPClientTests(unittest.TestCase):
    def test_rendered_get_forms_pagination_detail_back_and_shipped_save_script(self):
        from pathlib import Path
        from tests.candidate_decision_support import decision_state, observe, verified_https_request
        from tests.candidate_continuity_support import running_process, _insert_copy
        from tests.test_candidate_continuity_client import run_client
        with decision_state() as state, patch('tests.test_candidate_continuity_client.https_request', verified_https_request):
            with closing(sqlite3.connect(state.database_path)) as c, c:
                c.row_factory = sqlite3.Row
                canonical = dict(c.execute('SELECT * FROM canonical_opportunities WHERE id=9303').fetchone())
                posting = dict(c.execute('SELECT * FROM jobs WHERE id=9403').fetchone())
                source = dict(c.execute('SELECT * FROM job_source_contents WHERE job_id=9403').fetchone())
                for i in range(65):
                    identity = 15000+i
                    url = 'https://jobs.example.test/pagination-'+str(i)
                    external = 'pagination-'+str(i)
                    _insert_copy(c, 'canonical_opportunities', canonical, id=identity, canonical_key=external,
                        canonical_title='PaginationFixture '+str(i), normalized_title='paginationfixture '+str(i))
                    _insert_copy(c, 'jobs', posting, id=identity, canonical_opportunity_id=identity,
                        title='PaginationFixture '+str(i), external_id=external, url=url, source_hash=external,
                        location='Remote — Brazil')
                    _insert_copy(c, 'job_source_contents', source, job_id=identity, external_id=external, source_url=url)
            with running_process(state):
                self.assertTrue(run_client(state, 'browse', script=Path(__file__).with_name('browse_jobs_client.cjs'), observe=observe))

if __name__ == '__main__':
    unittest.main()
