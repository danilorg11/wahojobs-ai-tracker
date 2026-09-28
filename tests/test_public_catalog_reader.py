"""Anonymous launch boundary and prepared-generation lifecycle, entirely offline."""
from copy import deepcopy
from datetime import datetime, timedelta
from email.message import Message
from io import BytesIO
from http.client import HTTPConnection
from http.server import HTTPServer
import json
from pathlib import Path
import shlex
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from tests.test_public_job_page import seed_public_job, OBSERVED_AT
from wahojobs import public_jobs_catalog as catalog
from wahojobs.public_catalog_reader import PublicCatalogReader, publication_quality, preparation_metadata, opportunity_label
from wahojobs.remote_beta import RemoteBetaIntegration, make_remote_handler, PROXY_HEADER
from wahojobs import public_catalog_configuration as public_config
from scripts import daily_inventory as daily_cli

NOW = datetime.fromisoformat(OBSERVED_AT)
BODY = ('Evaluate model responses and build reliable evaluation systems.\n\n'
        '## Requirements\n- Experience writing Python.\n- Work from Brazil.\n\n'
        '## Compensation\nThe employer offers USD 35 per hour for accepted work.\n\n'
        'Read all employer terms and complete its application process.')


class PublicCatalogReaderTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        seed_public_job(self.db)
        self.db.execute("UPDATE job_source_contents SET provider='acme-ai', body=?", (BODY,))
        self.db.commit()
        self.jobs = catalog.load_public_jobs(self.db, now=NOW)
        self.now = NOW
        self.metadata, self.generation = preparation_metadata(self.db)
        self.reader = self.make_reader()

    def make_reader(self, jobs=None, **kwargs):
        return PublicCatalogReader(jobs if jobs is not None else self.jobs,
            clock=lambda: self.now, metadata=self.metadata, generation=self.generation, **kwargs)

    def get(self, path, *, reader=None, cookie=''):
        return (reader or self.reader).handle('GET', path,
            (('Host', 'www.wahojobs.com'), ('Cookie', cookie)))

    def test_anonymous_search_detail_return_and_destination_reuse_source_fields(self):
        result = self.get('/jobs?q=Python&location=Brazil')
        self.assertEqual(result.status, 200)
        self.assertIn(b'/jobs/opportunity-9002?', result.body)
        target = '/jobs/opportunity-9002?' + urlencode({'variant': 9003, 'return_to': '/jobs?q=Python&location=Brazil'})
        page = self.get(target)
        self.assertEqual(page.status, 200)
        self.assertIn(b'Evaluate model responses', page.body)
        self.assertIn(b'https://apply.example.test/acme-9003', page.body)
        self.assertIn(b'/jobs?q=Python&amp;location=Brazil', page.body)
        for marker in (b'href=\'/login', b'Make it personal', b'Create a profile', b'Save job', b'Matches'):
            self.assertNotIn(marker, page.body)

    def test_no_account_content_cookie_or_source_read_on_cold_warm_and_expiry_navigation(self):
        with (patch('wahojobs.public_jobs_catalog.load_public_jobs', side_effect=AssertionError('no DB preparation')),
              patch('socket.create_connection', side_effect=AssertionError('no employer/model requests'))):
            first = self.get('/jobs/opportunity-9002', cookie='account=secret-A')
            other = self.get('/jobs/opportunity-9002', cookie='account=secret-B')
            self.assertEqual(first.body, other.body)
            self.now += timedelta(seconds=301)
            expired_cache = self.get('/jobs/opportunity-9002')
            self.assertEqual(first.body, expired_cache.body)
            self.assertNotIn('Set-Cookie', dict(first.headers))
            self.assertEqual(dict(first.headers)['Cache-Control'], 'no-store')

    def test_expired_verification_removes_catalog_and_sitemap_without_claiming_employer_closed(self):
        self.now += timedelta(hours=73)
        self.assertNotIn(b'opportunity-9002', self.get('/jobs').body)
        self.assertNotIn(b'opportunity-9002', self.get('/jobs/sitemap.xml').body)
        page = self.get('/jobs/opportunity-9002')
        self.assertEqual(page.status, 200)
        self.assertIn(b'Availability needs rechecking', page.body)
        self.assertIn(b'not confirmed closed', page.body)
        self.assertNotIn(b'apply.example.test', page.body)
        self.assertEqual(dict(page.headers)['X-Robots-Tag'], 'noindex,follow')

    def test_new_generation_restart_adds_changes_and_closures_failure_does_not_serve_old_data(self):
        self.db.execute("UPDATE job_source_contents SET body=body || ' Additional employer requirement.'")
        self.db.commit()
        newer = self.make_reader(catalog.load_public_jobs(self.db, now=NOW))
        self.assertIn(b'Additional employer requirement', self.get('/jobs/opportunity-9002', reader=newer).body)
        unavailable = self.make_reader()
        unavailable._available = Mock(side_effect=ValueError('lease retired'))
        self.assertEqual(self.get('/jobs', reader=unavailable).status, 503)
        self.db.execute('UPDATE canonical_opportunities SET is_active=0')
        self.db.commit()
        self.metadata, self.generation = preparation_metadata(self.db)
        closed = self.make_reader(catalog.load_public_jobs(self.db, now=NOW))
        self.assertEqual(self.get('/jobs/opportunity-9002', reader=closed).status, 410)
        self.assertNotIn(b'opportunity-9002', self.get('/jobs/sitemap.xml', reader=closed).body)

    def test_failed_public_preparation_prevents_readiness(self):
        with self.assertRaisesRegex(ValueError, 'public_catalog_not_ready'):
            self.make_reader(available=Mock(side_effect=ValueError('lease retired')))

    def test_daily_process_pin_accepts_only_the_fixed_protected_public_configuration(self):
        normal = ['/opt/wahojobs-beta/current/.venv/bin/python', '-B', 'scripts/private_beta_app.py',
            '--config', '/run/wahojobs-beta/runtime.json', '--logs', '/var/log/wahojobs-beta']
        enabled = normal + ['--public-catalog-config', public_config.OPERATING_CONFIGURATION_PATH]
        with patch.object(public_config, 'load_configuration', return_value={'gateway_key': 'b'*64}) as load:
            daily_cli.verify_beta_process_command(normal)
            load.assert_not_called()
            daily_cli.verify_beta_process_command(enabled)
            load.assert_called_once_with(public_config.OPERATING_CONFIGURATION_PATH)
            for command in (enabled + ['--debug'], normal + ['--public-catalog-config', '/tmp/other.json'],
                            enabled + enabled[-2:], normal[:-1] + ['/tmp/other-logs'],
                            normal[:4] + ['/tmp/other-runtime.json'] + normal[5:]):
                with self.subTest(command=command), self.assertRaisesRegex(ValueError, 'beta_process_mismatch'):
                    daily_cli.verify_beta_process_command(command)
        with patch.object(public_config, 'load_configuration', side_effect=ValueError('invalid_configuration')):
            with self.assertRaisesRegex(ValueError, 'invalid_configuration'):
                daily_cli.verify_beta_process_command(enabled)

    def test_public_configuration_requires_exact_origin_fields_and_private_file(self):
        with tempfile.TemporaryDirectory(prefix='public-reader-config-') as directory:
            path = Path(directory) / 'catalog.json'
            document = dict(version=1, public_origin='https://www.wahojobs.com',
                            gateway_key='b'*64, indexable=False)
            path.write_text(json.dumps(document), encoding='utf-8')
            path.chmod(0o600)
            self.assertEqual(public_config.load_configuration(str(path)), document)
            for changes in ({'public_origin': 'https://beta.wahojobs.com'}, {'gateway_key': 'short'},
                            {'indexable': 1}, {'version': True}, {'unexpected': 'value'}):
                path.write_text(json.dumps(dict(document, **changes)), encoding='utf-8')
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    public_config.load_configuration(str(path))
            path.write_text(json.dumps(document)[:-1] + ',"indexable":true}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'duplicate_configuration_field'):
                public_config.load_configuration(str(path))

    def test_activation_template_matches_the_daily_process_pin(self):
        template = Path(__file__).resolve().parents[1] / 'deploy/private-beta/70-public-catalog.conf'
        commands = [line.partition('=')[2] for line in template.read_text().splitlines()
                    if line.startswith('ExecStart=') and line != 'ExecStart=']
        self.assertEqual(len(commands), 1)
        with patch.object(public_config, 'load_configuration', return_value={}) as load:
            daily_cli.verify_beta_process_command(shlex.split(commands[0]))
            load.assert_called_once_with(public_config.OPERATING_CONFIGURATION_PATH)

    def test_private_routes_wrong_host_method_and_malformed_targets_are_denied(self):
        for path in ('/login', '/account/profile', '/find-matches', '/tracker', '/api/profile',
                     '/_ops/ready', '/jobs/../account/profile', '/jobs%2f..%2faccount',
                     '/jobs/export.sqlite3', '/jobs/opportunity-0', '/jobs/opportunity-999999'):
            self.assertEqual(self.get(path).status, 404, path)
        for path in ('/jobs?debug=1', '/jobs?q=x&q=y', '/jobs?page=-1',
                     '/jobs/opportunity-9002?variant=0', '/jobs/opportunity-9002?return_to=https://evil.test',
                     '/jobs/opportunity-9002?variant=9003&variant=9003', '/jobs?q=%zz'):
            self.assertEqual(self.get(path).status, 400, path)
        self.assertEqual(self.reader.handle('POST', '/jobs').status, 405)
        self.assertEqual(self.reader.handle('GET', '/jobs', (('Host', 'beta.wahojobs.com'),)).status, 400)

    def test_escaping_does_not_allow_source_html_or_filter_script_execution(self):
        jobs = deepcopy(self.jobs)
        variant = jobs[0]['_catalog_variants'][0]
        variant['source_title'] = '<img src=x onerror=alert(1)>'
        variant['rich_body'] = BODY + '\n\n<script>alert(1)</script>'
        # A corrupt script document is withheld, not made a complete job page.
        held = self.make_reader(jobs)
        self.assertEqual(self.get('/jobs/opportunity-9002', reader=held).status, 404)
        response = self.get('/jobs?' + urlencode({'q': '<script>alert(1)</script>'}))
        self.assertNotIn(b'<script>alert(1)</script>', response.body)
        self.assertIn(b'&lt;script&gt;', response.body)

    def test_limited_description_is_useful_but_not_indexable(self):
        jobs = deepcopy(self.jobs)
        jobs[0]['_catalog_variants'][0]['rich_body'] = ''
        reader = self.make_reader(jobs, indexable=True)
        page = self.get('/jobs/opportunity-9002', reader=reader)
        self.assertEqual(page.status, 200)
        self.assertIn(b'Limited source information', page.body)
        self.assertEqual(dict(page.headers)['X-Robots-Tag'], 'noindex,follow')
        self.assertNotIn(b'opportunity-9002', self.get('/jobs/sitemap.xml', reader=reader).body)

    def test_short_source_teaser_has_visible_limited_notice(self):
        jobs = deepcopy(self.jobs)
        jobs[0]['_catalog_variants'][0]['rich_body'] = 'Short retained employer teaser.'
        reader = self.make_reader(jobs, indexable=True)
        page = self.get('/jobs/opportunity-9002', reader=reader)
        self.assertIn(b'Limited source information', page.body)
        self.assertIn(b'Short retained employer teaser.', page.body)
        self.assertNotIn(b'<h2>Employer description and requirements</h2>', page.body)
        self.assertEqual(dict(page.headers)['X-Robots-Tag'], 'noindex,follow')

    def test_known_language_conflict_is_held_until_source_corrects_it(self):
        variant = dict(self.jobs[0]['_catalog_variants'][0],
            company_slug='welocalize', rich_provider='welocalize',
            source_title='Talent Pool: Romansh Speakers (Switzerland)',
            rich_body=BODY+'\nTo Apply: Include a cover letter highlighting your Italian proficiency.')
        self.assertEqual(publication_quality(variant)[:2], ('withheld', 'known_language_body_conflict'))
        variant['rich_body'] = variant['rich_body'].replace('Italian', 'Romansh')
        self.assertEqual(publication_quality(variant)[0], 'indexable')

    def test_turing_jira_title_with_support_specialist_document_is_withheld(self):
        variant = dict(self.jobs[0]['_catalog_variants'][0],
            company_slug='turing', rich_provider='turing', source_title='Atlassian Jira Admin',
            rich_body='IT Support Specialist\n\n'+BODY)
        self.assertEqual(publication_quality(variant)[:2], ('withheld', 'known_title_body_conflict'))
        variant['rich_body'] = 'Atlassian Jira Admin\n\n'+BODY
        self.assertEqual(publication_quality(variant)[0], 'indexable')

    def test_source_identity_and_known_conflict_are_withheld(self):
        variant = self.jobs[0]['_catalog_variants'][0]
        for changes in ({'rich_external_id':'other'}, {'rich_source_url':'https://other.test'},
                        {'rich_provider':'other'}, {'company_slug':'dataforce','source_title':'Viola'},
                        {'rich_body':'Job title: Unrelated bartender\n\n' + BODY}):
            state, _, _ = publication_quality(dict(variant, **changes))
            self.assertEqual(state, 'withheld', changes)

    def test_meridial_suffix_alias_preserves_all_role_words(self):
        variant = dict(self.jobs[0]['_catalog_variants'][0], company_slug='meridial', rich_provider='meridial')
        for role in ('Afrikaans Language Specialist', 'Yoruba Language Specialist', 'Zulu Language Specialist'):
            job = dict(variant, source_title=role+' - Freelance AI Trainer Project',
                       rich_body='Job Title: '+role+' – AI Trainer\n\n'+BODY)
            self.assertEqual(publication_quality(job)[0], 'indexable')
        job = dict(variant, source_title='Graphic Designer Specialist - Freelance AI Trainer Project',
                   rich_body='Job Title: Graphic Designer – AI Trainer\n\n'+BODY)
        self.assertEqual(publication_quality(job)[:2], ('withheld', 'unresolved_title_variation'))
        job = dict(variant, source_title='Language Specialist - Freelance AI Trainer Project',
                   rich_body='Job Title: Master - Language Specialist – AI Trainer\n\n'+BODY)
        self.assertEqual(publication_quality(job)[:2], ('withheld', 'unresolved_title_variation'))

    def test_explicit_future_and_standing_applications_are_labeled_faithfully(self):
        job = dict(source_title='Future Opportunities: Join our global TrainAI talent pool')
        self.assertEqual(opportunity_label(job, 'Join Our Global TrainAI Talent Pool!'),
                         'Talent network — future consideration')
        for text in ('As part of this talent pool, you may be considered for future freelance projects.',
                     'This opportunity is for our Talent Pool. We consider future projects and clients.'):
            self.assertEqual(opportunity_label({}, text), 'Talent network — future consideration')
        self.assertEqual(opportunity_label({}, 'This is a standing listing, not a specific job opening.'),
                         'Ongoing application opportunity')
        self.assertEqual(opportunity_label({}, 'Help recruiters maintain their talent pool records.'),
                         'Advertised opportunity')

    def test_missing_description_never_bypasses_destination_or_source_identity(self):
        variant = self.jobs[0]['_catalog_variants'][0]
        for changes in ({'rich_source_url':'https://apply.example.test/unrelated-role',
                         'official_url':'https://apply.example.test/unrelated-role'},
                        {'rich_external_id':'other'}, {'rich_provider':'other'}):
            job = dict(variant, rich_body='', **changes)
            self.assertEqual(publication_quality(job)[0], 'withheld')
            self.assertEqual(self.get('/jobs/opportunity-9002', reader=self.make_reader([job])).status, 404)
        absent = dict(variant, rich_body=None, rich_provider=None, rich_external_id=None,
                      rich_source_url=None, has_rich_content=0)
        self.assertEqual(publication_quality(absent)[0], 'limited')

    def test_sibling_variant_and_full_population_facets_remain_bound(self):
        group = deepcopy(self.jobs[0])
        variant = deepcopy(group['_catalog_variants'][0])
        variant.update(job_id=9005, source_location='Remote — Canada', source_title='Canadian role',
                       official_url='https://apply.example.test/ca', listing_url='https://apply.example.test/ca',
                       rich_source_url='https://apply.example.test/ca', external_id='ca', rich_external_id='ca')
        variant['enrichment']['attributes']['work_arrangement']['eligible_countries'] = ['Canada']
        catalog.prepare_catalog_presentation(variant)
        group['_catalog_variants'] += (variant,)
        reader = self.make_reader([group])
        page = self.get('/jobs?location=Canada', reader=reader)
        self.assertIn(b'variant=9005', page.body)
        self.assertNotIn(b'variant=9003', page.body)
        detail = self.get('/jobs/opportunity-9002?variant=9005', reader=reader)
        self.assertIn(b'https://apply.example.test/ca', detail.body)
        self.assertNotIn(b'https://apply.example.test/acme-9003', detail.body)

    def test_pagination_canonical_is_distinct_and_sitemap_ignores_verification_clock(self):
        jobs = []
        for i in range(33):
            row = deepcopy(self.jobs[0]['_catalog_variants'][0])
            row['canonical_opportunity_id'] += i
            row['job_id'] += i
            jobs.append(row)
        reader = self.make_reader(jobs, indexable=True)
        page = self.get('/jobs?page=2', reader=reader)
        self.assertEqual(page.status, 200)
        self.assertIn(b"rel='canonical' href='https://www.wahojobs.com/jobs?page=2'", page.body)
        self.assertEqual(self.get('/jobs?page=3', reader=reader).status, 404)
        self.assertEqual(dict(self.get('/jobs?q=Python', reader=reader).headers)['X-Robots-Tag'], 'noindex,follow')
        sitemap = self.get('/jobs/sitemap.xml', reader=reader).body
        self.assertNotIn(b'lastmod', sitemap)
        self.assertNotIn(b'variant=', sitemap)
        self.assertNotIn(b'JobPosting', page.body)

    def test_coverage_counts_unverified_variants_without_inflating_canonical_opportunities(self):
        self.reader.metadata = {
            9002: dict(provider='acme-ai', is_active=1, active_variants=3),
            9004: dict(provider='acme-ai', is_active=1, active_variants=2),
            9005: dict(provider='acme-ai', is_active=0, active_variants=0),
            9006: dict(provider='micro1', is_active=1, active_variants=1),
        }
        coverage = self.reader.coverage()['providers']
        self.assertEqual(coverage['acme-ai']['canonical'], dict(indexable=1, limited=0, withheld=1))
        self.assertEqual(coverage['acme-ai']['variants'], dict(indexable=1, limited=0, withheld=4))
        self.assertEqual(coverage['acme-ai']['withheld_reasons'], dict(not_currently_source_verified=4))
        self.assertEqual(coverage['acme-ai']['inactive_canonical'], 1)
        self.assertEqual(coverage['micro1']['canonical']['withheld'], 1)
        self.assertEqual(coverage['micro1']['withheld_reasons'], dict(source_disabled=1))

    def test_remote_beta_public_branch_requires_separate_key_and_never_calls_account_integration(self):
        runtime = Mock()
        runtime.browser_integration.handle.side_effect = AssertionError('account path reached')
        beta = RemoteBetaIntegration(runtime, public_catalog=self.reader, catalog_key='b'*64)
        headers = Message()
        headers['Host'] = 'beta.wahojobs.com'
        headers['Cookie'] = 'private-user=one'
        self.assertEqual(beta.handle('GET', '/_catalog/jobs', headers).status, 404)
        headers['X-Wahojobs-Catalog-Key'] = 'b'*64
        self.assertEqual(beta.handle('GET', '/_catalog/jobs', headers).status, 200)
        self.assertEqual(beta.handle('GET', '/_catalog/account/profile', headers).status, 404)
        self.assertEqual(beta.handle('POST', '/_catalog/jobs', headers, BytesIO(b'private=payload')).status, 405)
        headers['X-Wahojobs-Catalog-Key'] = 'b'*64
        self.assertEqual(beta.handle('GET', '/_catalog/jobs', headers).status, 404)
        runtime.browser_integration.handle.assert_not_called()

    def test_real_http_ingress_retains_both_boundaries_and_head_contract(self):
        runtime = Mock(public_origin='https://beta.wahojobs.com')
        runtime.browser_integration.handle.side_effect = AssertionError('account path reached')
        handler = make_remote_handler(runtime, 'a'*64, public_catalog=self.reader, catalog_key='b'*64)
        server = HTTPServer(('127.0.0.1', 0), handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            def request(method='GET', **changes):
                headers = {'Host': 'beta.wahojobs.com', PROXY_HEADER: 'a'*64,
                           'X-Wahojobs-Catalog-Key': 'b'*64, 'Cookie': 'private-account=never-forwarded'}
                headers.update(changes)
                conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                try:
                    conn.request(method, '/_catalog/jobs', headers=headers)
                    response = conn.getresponse()
                    return response.status, dict(response.headers), response.read()
                finally:
                    conn.close()
            status, headers, body = request()
            self.assertEqual(status, 200)
            self.assertIn(b'Browse jobs', body)
            self.assertEqual(headers['X-Wahojobs-Catalog-Robots'], 'noindex,follow')
            self.assertNotIn('Set-Cookie', headers)
            repeat_status, repeat_headers, repeat_body = request()
            self.assertEqual((repeat_status, repeat_body), (status, body))
            self.assertEqual(repeat_headers['Content-Length'], str(len(body)))
            self.assertEqual(repeat_headers['X-Wahojobs-Catalog-Robots'], 'noindex,follow')
            status, head_headers, head_body = request('HEAD')
            self.assertEqual((status, head_body), (200, b''))
            self.assertEqual(head_headers['Content-Length'], str(len(body)))
            self.assertEqual(request(**{PROXY_HEADER: 'c'*64})[0], 403)
            self.assertEqual(request(**{'X-Wahojobs-Catalog-Key': ''})[0], 404)
            self.assertEqual(request(**{'X-Forwarded-Host': 'www.wahojobs.com'})[0], 403)
            self.assertEqual(request(Host='www.wahojobs.com')[0], 403)
            runtime.browser_integration.handle.assert_not_called()
        finally:
            server.shutdown()
            worker.join(timeout=5)
            server.server_close()


if __name__ == '__main__':
    unittest.main()
