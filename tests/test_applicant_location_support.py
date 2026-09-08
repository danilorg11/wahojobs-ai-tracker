"""Public recruitment wording, anonymous profiles and isolated inventories."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from tests import test_provider_detail_recovery as detail_tests
from tests import test_accepted_task_matching as task_tests
from tests.test_confirmed_activity_matching import candidate, v2
from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from wahojobs.matching.source_geography import prepare_applicant_location_support
from wahojobs.matching.locations import location_eligibility
from wahojobs.matching.accepted_tasks import _prepare_eligibility
from wahojobs.crawler import provider_details as details


QUOTE = "We're looking for detail-oriented Audio Transcript Editors based in São Paulo and across Brazil to review, clean up, and correct machine-generated audio transcripts in Brazilian Portuguese — ensuring every word, timestamp, and formatting detail is exactly right."
BODY = '# Role\n\n## About the Role\n\n' + QUOTE
FIELD = 'props.pageProps.job.longDescription'


def packet(body=BODY, provider='alignerr', external='example', url='https://www.alignerr.com/jobs/example'):
    return {details.DETAIL_KEY: dict(version=1, provider=provider, external_id=external, url=url,
            applicant_location_support=prepare_applicant_location_support(body, FIELD))}


class ApplicantLocationSupportTests(unittest.TestCase):
    def test_country_support_includes_another_region_but_is_not_exclusive(self):
        prepared = prepare_applicant_location_support(BODY, FIELD)
        clause = prepared['clauses'][0]
        self.assertEqual(clause['source_quote'], QUOTE)
        self.assertEqual(clause['countries'], ['Brazil'])
        self.assertEqual(clause['modality'], 'invitation')
        row = dict(location='Remote', applicant_country_requirements=[clause])
        for region in ['SP', 'Pernambuco']:
            self.assertEqual(location_eligibility(dict(country='Brazil', region=region), row).status, 'eligible')
        for profile in [{}, {'country': 'Portugal'}, {'work_authorization_countries': ['Brazil']}]:
            self.assertEqual(location_eligibility(profile, row).status, 'unknown')

    def test_incidental_preferred_negated_and_city_only_remain_unassessed(self):
        for sentence in [
            'Our headquarters are based in Brazil to serve customers.',
            "We're looking for editors based in São Paulo to review audio.",
            "We're looking for editors based in Brazil only to review audio.",
            "We're not looking for editors based in Brazil to review audio.",
            "We're looking for editors preferably based in Brazil to review audio.",
            "We're looking for editors ideally based in Brazil to review audio.",
            "We're looking for editors based in Brazil to serve our customers.",
            "We're looking for editors based in Brazil if authorized to work there to review audio.",
        ]:
            with self.subTest(sentence=sentence):
                self.assertIsNone(prepare_applicant_location_support('## About the Role\n'+sentence, FIELD))
        self.assertIsNone(prepare_applicant_location_support(BODY.replace('About the Role', 'Nice to Have'), FIELD))

    def test_alternatives_and_conflicting_mandatory_evidence(self):
        body = '## About the Role\nWe are recruiting reviewers based in Brazil or Portugal to review audio.'
        clauses = prepare_applicant_location_support(body, FIELD)['clauses']
        for country in ['Brazil', 'Portugal']:
            self.assertEqual(location_eligibility({'country': country}, dict(location='Remote', applicant_country_requirements=clauses)).status, 'eligible')
        for mode, countries in [('allow', ['United States']), ('exclude', ['Brazil'])]:
            rule = dict(dimension='location', mode=mode, countries=countries, unresolved=False, source_field='Required location')
            checked = location_eligibility({'country': 'Brazil'}, dict(location='Remote', applicant_country_requirements=clauses+[rule]))
            self.assertEqual(checked.status, 'unknown')
            self.assertIn('Conflicting source', checked.reason)
        conflict = prepare_applicant_location_support(BODY+'\nApplicants based in Brazil cannot apply.',FIELD)['clauses']
        self.assertEqual(location_eligibility({'country':'Brazil'},dict(location='Remote', applicant_country_requirements=conflict)).status,'unknown')

    def test_projection_requires_preparation_current_body_and_exact_identity(self):
        def project(metadata, body=BODY):
            return _prepare_eligibility('hash','alignerr','example','https://www.alignerr.com/jobs/example',body,'text/markdown',json.dumps(metadata))[1]
        self.assertEqual(project({}), ())  # not parsed in the request
        metadata = packet()
        self.assertEqual(project(metadata)[0]['countries'], ['Brazil'])
        self.assertEqual(project(metadata, BODY.replace('Brazil', 'Portugal')), ())
        for field, value in [('provider','other'), ('external_id','other'), ('url','https://www.alignerr.com/jobs/other')]:
            changed = deepcopy(metadata); changed[details.DETAIL_KEY][field] = value
            self.assertEqual(project(changed), ())


class AcceptedDetailRepreparationTests(unittest.TestCase):
    setUp = detail_tests.ProviderDetailPersistenceTests.setUp

    def test_accepted_reprocessing_keeps_capture_identity_body_and_lifecycle(self):
        case = self.case
        record = dict(id=case['external_id'], name=case['title'], isActive=True, longDescription=BODY)
        response = details.DetailResponse(case['url'], ('<script id="__NEXT_DATA__">'+json.dumps(dict(props=dict(pageProps=dict(job=record))))+'</script>').encode(), case['observed_at'])
        with patch.object(details, '_prepare_detail_geography', side_effect=lambda c:c), self.conn:
            details.reprocess_saved_detail(self.conn, self.job_id, response)
        before = dict(self.conn.execute('SELECT * FROM jobs WHERE id=?',(self.job_id,)).fetchone())
        source = dict(self.conn.execute('SELECT * FROM job_source_contents WHERE job_id=?',(self.job_id,)).fetchone())
        old = json.loads(source['metadata_json'])[details.DETAIL_KEY]
        runs = [tuple(r) for r in self.conn.execute('SELECT * FROM crawl_runs')]
        with patch.object(details, 'fetch_detail', side_effect=AssertionError('No HTTP')), self.conn:
            result = details.reprocess_saved_detail(self.conn,self.job_id)
        self.assertTrue(result.accepted)
        after = dict(self.conn.execute('SELECT * FROM jobs WHERE id=?',(self.job_id,)).fetchone())
        self.assertEqual(before, after)
        now = dict(self.conn.execute('SELECT * FROM job_source_contents WHERE job_id=?',(self.job_id,)).fetchone())
        for field in ['body','body_format','source_url','source_updated_at','first_captured_at','last_captured_at']:
            self.assertEqual(source[field], now[field])
        new = json.loads(now['metadata_json'])[details.DETAIL_KEY]
        self.assertEqual({k:v for k,v in new.items() if k!='applicant_location_support'},old)
        self.assertEqual(new['applicant_location_support']['clauses'][0]['countries'],['Brazil'])
        self.assertEqual(runs,[tuple(r) for r in self.conn.execute('SELECT * FROM crawl_runs')])
        detail_tests.verify_job_source_acceptance_integrity(self.conn,self.job_id)

    def test_no_accepted_detail_cannot_be_reprepared(self):
        before = '\n'.join(self.conn.iterdump())
        with self.assertRaises(ValueError): details.reprocess_saved_detail(self.conn,self.job_id)
        self.assertEqual(before, '\n'.join(self.conn.iterdump()))


class AuthenticatedLocationSupportTests(unittest.TestCase):
    role = task_tests.AcceptedTaskMatchingTests.role
    source = task_tests.AcceptedTaskMatchingTests.source
    current = task_tests.AcceptedTaskMatchingTests.current
    match = task_tests.AcceptedTaskMatchingTests.match

    def setUp(self):
        self.f = SyntheticMatcherFixture(); self.addCleanup(self.f.close)
        self.f.profile = v2(candidate(['Model output evaluation']))
        self.role('Portuguese AI Data Reviewer', 'Remote')

    def test_old_run_profile_changes_variant_isolation_and_unchanged_scores(self):
        body = BODY+'\n\n## What You\'ll Do\n\nEvaluate AI outputs.\n\n## Requirements\n\nOwn a Mac with an Apple Silicon chip.'
        self.source(body,body_format='text/markdown')
        _,old,ctx = self.current(); before = self.match(ctx)
        with self.f.provider() as c:
            external,url = c.execute('SELECT external_id,url FROM jobs WHERE id=7003').fetchone()
        metadata = packet(body, 'configured-production',external,url)
        self.source(body,body_format='text/markdown',metadata=metadata)
        response,_,ctx = self.current('/find-matches?run='+old.match_run_id)
        after=self.match(ctx)
        self.assertEqual(before['location_eligibility_status'],'unknown')
        self.assertEqual(after['location_eligibility_status'],'eligible')
        self.assertEqual(before['score_components'],after['score_components'])
        self.assertEqual(before['score'],after['score'])
        self.assertEqual(self.match(ctx,7006)['location_eligibility_status'],'unknown')
        self.assertIn(b'Mac',response.body)  # unrelated condition retained
        changed = candidate(['Model output evaluation'])
        changed['location']['country']='Portugal'
        changed['location']['residence']='Portugal'
        self.f.profile=v2(changed)
        _,_,ctx=self.current('/find-matches?run='+old.match_run_id)
        self.assertEqual(self.match(ctx)['location_eligibility_status'],'unknown')


if __name__ == '__main__': unittest.main()
