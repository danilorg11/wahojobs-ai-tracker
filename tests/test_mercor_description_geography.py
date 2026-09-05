"""One captured description regression; grammar/negative cases are synthetic."""
from contextlib import closing
from copy import deepcopy
import hashlib
from html import escape
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_mercor_applicant_geography as support
from wahojobs import authenticated_profile_matches as browser
from wahojobs.crawler.providers import mercor
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import verify_job_source_acceptance_integrity
from wahojobs.matching.locations import location_eligibility
from wahojobs.matching import source_geography
from wahojobs.source_capture import normalize_source_body


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/mercor_description_geography.json').read_text(encoding='utf-8'))


class MercorDescriptionGeographyTests(unittest.TestCase):
    def setUp(self):
        self.support = support.MercorApplicantGeographyTests()
        self.support.setUp()
        self.addCleanup(self.support.doCleanups)
        self.fixture = self.support.fixture
        self.fixture.profile = deepcopy(FIXTURE['profile'])
        self.record = self.support.record = deepcopy(FIXTURE['record'])
        self.assertEqual(FIXTURE['observed_at'], support.FIXTURE['observed_at'])
        self.ingest = self.support.ingest
        self.render = self.support.render
        self.check = self.support.projected_check

    def synthetic(self, text, identity='synthetic-description', **fields):
        # Keep a healthy description; replace only the applicant details.
        body = self.record['description'].split('**Role Details**')[0]
        return dict(self.record, listingId=identity,
                    description=body + '\n**Eligibility**\n' + text, **fields)

    def test_captured_portugal_exclusion_has_accepted_description_provenance(self):
        self.ingest([self.record])
        response, matches = self.render()
        check, row = self.check()
        self.assertEqual(check.status, 'incompatible')
        self.assertIn('Portugal', check.reason)
        self.assertIn('U.S. only', check.reason)
        self.assertIn('description:line 42', check.reason)
        self.assertNotIn(escape(self.record['title']).encode(), response.body)
        match = next(m for m in matches if m['job_id'] == row['job_id'])
        self.assertFalse(match['primary_recommendation_eligible'])
        self.assertIn('incompatible_location', match['actionability_cap_reasons'])
        evidence = row['applicant_geography_evidence']
        self.assertEqual(evidence['observed_at'], FIXTURE['observed_at'])
        self.assertEqual(evidence['description']['body_sha256'],
                         hashlib.sha256(normalize_source_body(self.record['description']).encode()).hexdigest())
        with closing(get_connection(self.fixture.path)) as conn:
            verify_job_source_acceptance_integrity(conn, row['job_id'])

    def test_us_passes_geography_only_and_description_is_not_parsed_in_requests(self):
        self.fixture.profile['location'].update(country='United States', residence='United States')
        self.ingest([self.record])
        with patch.object(source_geography, 'prepare_mercor_description_geography', side_effect=AssertionError('request-time parse')):
            self.render()
            self.assertEqual(self.check()[0].status, 'eligible')
        # No assertion that assays, industry experience or other qualifications pass.

    def test_synthetic_country_alternatives_negation_context_and_uncertainty(self):
        cases = [
            ('Applicants must be based in the United States.', {}, 'incompatible'),
            ('Applicants must be based in Portugal or the United States.', {}, 'eligible'),
            ('Applicants must be based in Portugal and Canada.', {}, 'eligible'),
            ('Only applicants based in Canada or Portugal may apply.', {}, 'eligible'),
            ('Applicants must not be located in Portugal.', {}, 'incompatible'),
            ('Applicants residing in Portugal are not eligible.', {}, 'incompatible'),
            ('Applicants must not be based in the United States.', {}, 'unknown'),
            ('Applicants are not required to reside in the United States.', {}, 'unknown'),
            ('Applicants based in the United States are preferred.', {}, 'unknown'),
            ('**Nice to Have**\n- U.S. only', {}, 'unknown'),
            ('Based in France. This is a preference, not a requirement — applicants based elsewhere are welcome.', {}, 'unknown'),
            ('Our headquarters and customers are in the United States only.', {}, 'unknown'),
            ('U.S. citizens only. Applicants must have U.S. work authorization.', {}, 'unknown'),
            ('Applicants must work in the U.S. timezone.', {}, 'unknown'),
            ('**Company Overview**\n- U.S. only', {}, 'unknown'),
            ('> Applicants must be based in the United States.', {}, 'unknown'),
            ('```\nApplicants must be based in the United States.\n```', {}, 'unknown'),
            ('For one project, applicants must be based in the United States.', {}, 'unknown'),
            ('Applicants must be based in the United States unless approved otherwise.', {}, 'unknown'),
            ('Applicants must be based in the United States or approved territories.', {}, 'unknown'),
            ('Applicants must be based in Portugal or approved territories.', {}, 'eligible'),
            ('Applicants must be based in the United States.\nApplicants must be based in Portugal.', {}, 'unknown'),
            ('Applicants must be based in the United States.', {'eligibleLocation': ['PRT']}, 'unknown'),
            ('Applicants must be based in Portugal.', {'ineligibleLocation': ['PRT']}, 'unknown'),
            ('Applicants from all countries are eligible.', {'eligibleLocation': ['USA']}, 'unknown'),
            ('Applicants are not required to be based in the United States.', {'eligibleLocation': ['USA']}, 'unknown'),
            ('Applicants based in Portugal are eligible.', {'eligibleLocation': ['USA']}, 'unknown'),
            ('Applicants based in the United States are preferred.', {'eligibleLocation': ['PRT']}, 'eligible'),
            ('Applicants must be based in Portugal.', {'eligibleResidenceLocation': ['USA']}, 'incompatible'),
        ]
        records = [self.synthetic(text, f'grammar-{i}', **fields) for i, (text, fields, _) in enumerate(cases)]
        self.ingest(records)
        for i, (_, _, expected) in enumerate(cases):
            with self.subTest(case=i):
                self.assertEqual(self.check(f'grammar-{i}')[0].status, expected)

    def test_residence_unknown_location_and_authorization_are_distinct(self):
        self.ingest([self.synthetic('Applicants must reside in the United States.')])
        _, row = self.check('synthetic-description')
        for profile, expected in [
            ({'country': 'United States', 'residence': ''}, 'unknown'),
            ({'country': '', 'residence': '', 'work_authorization': 'US citizen'}, 'unknown'),
            ({'country': 'Portugal', 'residence': 'Portugal', 'work_authorization': 'US authorized'}, 'incompatible'),
            ({'country': 'Portugal', 'residence': 'United States'}, 'eligible'),
        ]:
            with self.subTest(profile=profile):
                self.assertEqual(location_eligibility(profile, row).status, expected)
        self.ingest([self.record])
        _, row = self.check()
        self.assertEqual(location_eligibility({'country': '', 'residence': 'United States'}, row).status, 'unknown')

    def test_remote_and_structured_restrictions_coexist_without_overrides(self):
        self.ingest([dict(self.record, location='Remote Worldwide', eligibleResidenceLocation=['PRT'])])
        self.assertEqual(self.check()[0].status, 'incompatible')
        self.ingest([dict(self.record, eligibleLocation=['USA'])])
        check, row = self.check()
        self.assertEqual(check.status, 'incompatible')
        self.assertEqual(row['applicant_geography_evidence']['description']['conflicting_dimensions'], [])
        self.assertEqual(len(row['applicant_country_requirements']), 2)

    def test_preferred_and_negated_evidence_is_retained_without_mandatory_gate(self):
        record = self.synthetic('Applicants based in the United States are preferred.\nApplicants need not reside in Canada.')
        parsed = mercor.parse_mercor_listing(record)
        packet = parsed.source_metadata[source_geography.DESCRIPTION_GEOGRAPHY_KEY]
        self.assertEqual([c['modality'] for c in packet['clauses']], ['preferred', 'not_required'])
        self.ingest([record])
        check, row = self.check('synthetic-description')
        self.assertEqual(check.status, 'unknown')
        self.assertNotIn('applicant_country_requirements', row)

    def test_same_payload_reprocessing_changes_evidence_not_clocks_or_safe_reuse(self):
        original = browser.local_product.secrets.token_urlsafe
        # Reuse the existing HTML-parity convention: action idempotency keys
        # intentionally rotate, so hold only these generated values fixed.
        keys = patch.object(browser.local_product.secrets, 'token_urlsafe',
                            side_effect=lambda n: 'synthetic-action-key' if n == 24 else original(n))
        keys.start()
        self.addCleanup(keys.stop)
        with patch.object(mercor, 'prepare_mercor_description_geography', return_value=None):
            self.ingest([self.record])  # Previous parser: the body is already stored.
        first, _ = self.render()
        old = self.fixture.last_run()
        self.assertIn(escape(self.record['title']).encode(), first.body)
        with patch.object(browser.profile_preview, 'query_preview_rows', side_effect=AssertionError('should reuse')):
            self.assertEqual(self.fixture.get('/find-matches?run=' + old.match_run_id).body, first.body)
        _, before = self.check()
        self.ingest([self.record])
        after = self.fixture.get('/find-matches?run=' + old.match_run_id)
        self.assertEqual(after.status, 200)
        self.assertNotEqual(old.match_run_id, self.fixture.last_run().match_run_id)
        self.assertNotIn(escape(self.record['title']).encode(), after.body)
        check, current = self.check()
        self.assertEqual(check.status, 'incompatible')
        for key in ('job_id', 'job_last_seen_at', 'latest_successful_source_run_at'):
            self.assertEqual(before[key], current[key])
        self.fixture.set_preferences('part_time')
        self.fixture.advance(73)
        fallback = self.fixture.get('/find-matches?run=' + old.match_run_id)
        self.assertNotIn(escape(self.record['title']).encode(), fallback.body)
        context = self.fixture.last_run().recommendation_context
        self.assertNotIn(f'canonical:{current["canonical_opportunity_id"]}', json.dumps(context.get('_typed_preference_enforcement', {})))

    def test_preexisting_workload_relaxation_cannot_resurrect_description_conflict(self):
        record = dict(self.record, commitment='Full-time', location='Remote - Portugal')
        self.fixture.set_preferences('part_time')
        with patch.object(mercor, 'prepare_mercor_description_geography', return_value=None):
            self.ingest([record])
        first, _ = self.render()
        old = self.fixture.last_run()
        self.assertIn(b'More opportunities if', first.body)
        self.assertIn(escape(record['title']).encode(), first.body)
        self.assertFalse(any(m['source_slug'] == 'mercor' for m in browser._primary_presentation_matches(old.recommendation_context)))
        self.ingest([record])
        self.assertNotIn(escape(record['title']).encode(), self.fixture.get('/find-matches?run=' + old.match_run_id).body)

    def test_variants_keep_own_accepted_description_and_eligible_representative(self):
        self.ingest([self.synthetic('Applicants must be based in the United States.', 'variant-us'),
                     self.synthetic('Applicants must be based in Portugal or Canada.', 'variant-pt')])
        with closing(get_connection(self.fixture.path)) as conn, conn:
            canonical = conn.execute("SELECT canonical_opportunity_id FROM jobs WHERE external_id='variant-us'").fetchone()[0]
            conn.execute("UPDATE jobs SET canonical_opportunity_id=? WHERE external_id='variant-pt'", (canonical,))
        self.render()
        us, us_row = self.check('variant-us')
        pt, pt_row = self.check('variant-pt')
        self.assertEqual((us.status, pt.status), ('incompatible', 'eligible'))
        self.assertNotEqual(us_row['applicant_geography_evidence']['capture_id'], pt_row['applicant_geography_evidence']['capture_id'])
        visible = browser._primary_presentation_matches(self.fixture.last_run().recommendation_context)
        self.assertIn(pt_row['job_id'], [m['job_id'] for m in visible])
        self.assertNotIn(us_row['job_id'], [m['job_id'] for m in visible])


if __name__ == '__main__':
    unittest.main()
