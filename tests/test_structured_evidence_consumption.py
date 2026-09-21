"""Synthetic contrasts for retained-source consumption; real-capture checks live in the audit."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch
from wahojobs import opportunity_enrichment as e, public_job_page as detail, public_jobs_catalog as catalog
from wahojobs.matching.languages import prepare_language_conditions, find_language_mentions
from wahojobs.matching.domains import detect_role_domains
from tests.test_source_evidence_recovery import semantic
from tests import test_browse_filter_correction as browse
from tests.test_public_jobs_catalog import NOW
from tests import test_opportunity_enrichment_v2 as durable


def body_input(body):
    data = semantic([dict(variant_ref='source_hash:a', title='Contributor'),
                     dict(variant_ref='source_hash:b', title='Contributor')])
    data['canonical'] = {}
    data['rich_content'][0].update(source_ref='source_hash:a', provider='alignerr',
        external_id='a', source_url='https://example.test/a', body_format='text/markdown',
        body=body, metadata={})
    return data


class LanguageAndTaxonomyTests(unittest.TestCase):
    def facts(self, body):
        return e.extract_deterministic_objective_facts(body_input(body))

    def test_required_communication_establishes_identity_without_invented_fluency(self):
        text = 'Clear written communication skills in English'
        self.assertEqual(prepare_language_conditions(text, 'required'), [])
        parsed = prepare_language_conditions(text, 'required', include_ungraded=True)
        self.assertEqual(parsed[0]['levels'], ['unspecified'])
        facts = self.facts('# Who You Are\n\n* ' + text)
        langs = [f for f in facts if f['field_path'].endswith('.languages')]
        self.assertEqual([f['value']['language'] for f in langs], ['english'])
        self.assertEqual(langs[0]['variant_refs'], ['source_hash:a'])
        self.assertEqual(langs[0]['evidence'][0]['evidence_text'], text)
        self.assertIn('source_capture:1', langs[0]['evidence'][0]['authority_refs'])

    def test_communication_preferences_alternatives_negations_and_incidental_mentions(self):
        for body in [
            '# Nice to Have\n\n* Clear written communication skills in English',
            '# About Us\n\n* Clear written communication skills in English',
            '# Who You Are\n\n* Clear written communication skills in English or French',
            '# Who You Are\n\n* Clear written communication skills in English or another language',
            '# Who You Are\n\n* Clear written communication skills in English or use of translation tools',
            '# Who You Are\n\n* Clear written communication skills in English are not required',
            '# Who You Are\n\n* Our clients have clear written communication skills in English',
            '# Who You Are\n\n* Clear written communication skills in English if assigned to that team',
            '# Who You Are\n\n* Communication with English customers',
            '# Who You Are\n\n* Fluent in Lean 4',
        ]:
            with self.subTest(body=body):
                self.assertFalse([f for f in self.facts(body) if f['field_path'].endswith('.languages')])

    def test_unrelated_experience_waiver_does_not_erase_adjacent_language_requirement(self):
        body = ('# Who You Are\n\n* Clear written communication skills in English\n'
                '* No prior AI, tech, or content experience required')
        langs = [f for f in self.facts(body) if f['field_path'].endswith('.languages')]
        self.assertEqual([f['value']['language'] for f in langs], ['english'])

    def test_explicit_locale_is_language_evidence_only(self):
        facts = self.facts('# Who You Are\n\n* Native Brazilian Portuguese speaker')
        langs = [f for f in facts if f['field_path'].endswith('.languages')]
        self.assertEqual(langs[0]['value']['locale'], 'Brazil')
        self.assertFalse([f for f in facts if 'eligible_countries' in f['field_path']])
        scoped = e.blank_document()
        e.project_variant_facts(scoped, facts, ['source_hash:a'])
        self.assertEqual(scoped['attributes']['work_arrangement']['eligible_countries'], [])

    def test_occupation_mapping_does_not_treat_tools_or_ai_umbrella_as_domains(self):
        self.assertEqual(detect_role_domains(dict(title='Mathematician – Formal Proof')), {'mathematics'})
        for title in ('Lean 4 contributor', 'AI Training', 'Python tool reviewer', 'Generalist'):
            self.assertEqual(detect_role_domains(dict(title=title)), set())
        self.assertEqual(find_language_mentions('Lean 4 and mathlib'), [])
        data = semantic([dict(variant_ref='source_hash:a', title='Mathematician'),
                         dict(variant_ref='source_hash:b', title='Lean 4 contributor')])
        domains = [f for f in e.extract_variant_role_facts(data) if f['field_path'].endswith('professional_domains')]
        self.assertEqual([(f['value'], f['variant_refs']) for f in domains], [('mathematics', ['source_hash:a'])])

    def test_specific_experience_waiver_is_not_general_zero(self):
        for quote in ('No prior AI experience needed', 'No prior AI or tech experience needed',
                      'No previous Python experience required'):
            self.assertIsNone(e.parse_required_years_experience(quote))
        self.assertEqual(e.parse_required_years_experience('No prior work experience required'), 0)


class ConsumerTests(unittest.TestCase):
    setUp = browse.BrowseFilterCorrectionTests.setUp
    role_evidence = browse.BrowseFilterCorrectionTests.role_evidence

    def test_new_language_reaches_filter_only_on_exact_variant(self):
        evidence = self.role_evidence()
        ref = 'source_hash:' + evidence['rows'][0]['source_hash']
        data = body_input('# Who You Are\n\n* Clear written communication skills in English')
        facts = e.extract_deterministic_objective_facts(data)
        for f in facts:
            f['variant_refs'] = [ref if r == 'source_hash:a' else r for r in f['variant_refs']]
        doc = e.blank_document()
        e.project_variant_facts(doc, facts, [ref, 'source_hash:other-source'])
        evidence['effective']['document'] = doc
        jobs = detail.prepare_public_job_variants(evidence, now=NOW)
        for job in jobs:
            catalog.prepare_catalog_presentation(job)
        self.assertEqual(jobs[0]['_catalog_filter_values']['language'], {'english'})
        self.assertEqual(jobs[1]['_catalog_filter_values']['language'], set())
        self.assertEqual(catalog.build_catalog(jobs, {'language': 'English'})['result_count'], 1)
        self.assertEqual(catalog.build_catalog(jobs, {'language': 'English', 'location': 'Portugal'})['result_count'], 0)

    def test_human_overrides_survive_cached_and_direct_variant_projection(self):
        cases = [('attributes.content.quick_take', 'set', 'Reviewed wording'),
                 ('attributes.role.professional_domains', 'set', []),
                 ('attributes.application.assessment_required', 'set', False),
                 ('attributes.requirements.years_experience_min', 'set', 0),
                 ('attributes.role.professional_domains', 'set_unknown', None)]
        for path, operation, value in cases:
            with self.subTest(path=path, operation=operation):
                evidence = self.role_evidence()
                effective = evidence['effective']
                e.apply_override_value(effective['document'], path, operation, value)
                effective.update(overridden_fields=[path], stale_override_fields=[path],
                                 field_sources={path: 'human_override'})
                before = deepcopy(evidence)
                direct = detail.prepare_public_job(evidence, selected_job_id=9003, now=NOW)
                cached = detail.prepare_public_job_variants(evidence, now=NOW)[0]
                expected = e.get_path(effective['document'], path)
                for job in (direct, cached):
                    self.assertEqual(e.get_path(job['enrichment'], path), expected)
                    self.assertEqual(job['stale_override_fields'], [path])
                self.assertEqual(evidence, before)


class DurableRefreshTests(unittest.TestCase):
    def setUp(self):
        self.fixture = durable.OpportunityEnrichmentV2Tests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.conn = self.fixture.conn
        self.jid, self.cid = self.fixture.fallback_enriched_job()
        self.fixture.persist_rich_source(self.jid, body='Flexible hours.\n\n# Who You Are\n\n* Native Portuguese')
        self.first = e.enrich_canonical_opportunity(self.conn, self.cid, llm_client=None)
        self.source = e.load_semantic_input(self.conn, self.cid)

    def changed_source(self):
        data = deepcopy(self.source)
        data['rich_content'][0]['body'] = '# Who You Are\n\n* Clear written communication skills in English'
        data['rich_content'][0]['material_content_sha256'] = 'new-material'
        return data

    def row(self):
        return dict(self.conn.execute('SELECT * FROM opportunity_enrichments WHERE canonical_opportunity_id=?', (self.cid,)).fetchone())

    def test_changed_content_is_consumed_history_preserved_and_second_pass_is_unchanged(self):
        old = deepcopy(self.first['document']['variant_facts'])
        data = self.changed_source()
        with patch.object(e, 'load_semantic_input', return_value=data):
            result = e.enrich_canonical_opportunity(self.conn, self.cid, llm_client=None, preserve_supported_facts=True)
            self.assertEqual(result['outcome'], 'updated')
            self.assertFalse(result['llm']['called'])
            self.assertEqual(e.classify_enrichment_freshness(data, self.row())['freshness'], 'current')
            self.assertEqual(e.enrich_canonical_opportunity(self.conn, self.cid, llm_client=None,
                             preserve_supported_facts=True)['outcome'], 'unchanged')
            # Current retained history is reusable; the envelope identifies its
            # policy instead of being indistinguishable from a stateless rebuild.
            self.assertTrue(self.row()['derivation_fingerprint'].startswith(e.RETAINED_FACTS_RECIPE_PREFIX))
            self.assertNotEqual(self.row()['derivation_fingerprint'], e.derivation_recipe_fingerprint())
            self.assertEqual(e.enrich_canonical_opportunity(self.conn, self.cid, llm_client=None)['outcome'], 'unchanged')
        doc = result['document']
        self.assertTrue(all(f in doc['variant_facts'] for f in old))
        self.assertEqual(doc['attributes']['work_arrangement']['schedule_type'], 'flexible')
        self.assertIn('english', [v['language'] for v in doc['attributes']['requirements']['languages']])
        self.assertEqual(self.first['document']['attributes']['compensation'], doc['attributes']['compensation'])

    def test_new_scalar_conflict_does_not_overwrite_old_observation(self):
        data = self.changed_source()
        data['rich_content'][0]['body'] += '\n\nHours: 30-40 hours per week'
        with patch.object(e, 'load_semantic_input', return_value=data):
            result = e.enrich_canonical_opportunity(self.conn, self.cid, preserve_supported_facts=True)
        self.assertIsNone(result['document']['attributes']['work_arrangement']['hours_per_week_min'])
        values = {f['value'] for f in result['document']['variant_facts']
                  if f['field_path'] == 'attributes.work_arrangement.hours_per_week_min'}
        self.assertEqual(values, {10, 30})

    def test_stale_model_envelope_is_not_relabelled_as_fresh_deterministic(self):
        for key in ('model_provider', 'model_name', 'prompt_version'):
            with self.subTest(key=key):
                self.conn.execute('UPDATE opportunity_enrichments SET model_provider=NULL, model_name=NULL, prompt_version=NULL')
                self.conn.execute('UPDATE opportunity_enrichments SET ' + key + "='retained-semantic-version'")
                before = self.row()
                with patch.object(e, 'load_semantic_input', return_value=self.changed_source()):
                    result = e.enrich_canonical_opportunity(self.conn, self.cid, llm_client=None, preserve_supported_facts=True)
                self.assertEqual(result['llm']['outcome'], 'semantic_refresh_required')
                self.assertFalse(result['llm']['called'])
                self.assertEqual(self.row(), before)


if __name__ == '__main__':
    unittest.main()
