"""Exact-variant offline preparation; no new source or renderer inference."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from wahojobs import opportunity_enrichment as enrich
from wahojobs import public_job_page as detail, public_jobs_catalog as catalog
from tests import test_browse_filter_correction as fixtures
from tests.test_public_jobs_catalog import NOW


def semantic(variants):
    return dict(variants=variants, rich_content=[dict(variant_ref=v['variant_ref'],
        semantic_material_sha256='material-' + v['variant_ref'],
        authority=dict(accepted_capture_ref='source_capture:' + str(i + 1)))
        for i, v in enumerate(variants)])


class VariantRoleRecoveryTests(unittest.TestCase):
    def test_forward_preparation_keeps_seniority_and_specialization_provenance(self):
        from tests import test_opportunity_enrichment_v2 as fixture
        case=fixture.OpportunityEnrichmentV2Tests('runTest')
        case.setUp()
        try:
            _,cid=case.fallback_enriched_job()
            data=enrich.load_semantic_input(case.conn,cid)
            doc=enrich.extract_deterministic_document(data)
            self.assertEqual(doc['attributes']['role']['seniority'],'senior')
            self.assertTrue(any(e['field_path']=='attributes.role.seniority' for e in doc['field_evidence']))
            if doc['attributes']['role']['specializations']:
                self.assertTrue(any(e['field_path']=='attributes.role.specializations' for e in doc['field_evidence']))
        finally:case.tearDown()

    def test_exact_listing_scope_does_not_borrow_canonical_or_sibling_fields(self):
        data=semantic([dict(variant_ref='source_hash:a', title='Financial Analyst', department='Finance'),
                       dict(variant_ref='source_hash:b', title='Audio Transcriptionist', department='Audio')])
        data['canonical']=dict(canonical_title='Financial Analyst | Audio Transcriptionist', source_category='Finance')
        before=deepcopy(data); doc=enrich.blank_document()
        enrich.recover_variant_role_facts(doc,data)
        finance=[f for f in doc['variant_facts'] if f['value']=='finance']
        audio=[f for f in doc['variant_facts'] if f['value']=='audio_speech']
        self.assertEqual(finance[0]['variant_refs'],['source_hash:a'])
        self.assertEqual(audio[0]['variant_refs'],['source_hash:b'])
        self.assertEqual(finance[0]['evidence'][0]['source_refs'],['source_hash:a'])
        self.assertIn('source_capture:1',finance[0]['evidence'][0]['authority_refs'])
        self.assertEqual(doc['attributes']['role']['professional_domains'],[])
        self.assertEqual(data,before)
        once=deepcopy(doc);enrich.recover_variant_role_facts(doc,data);self.assertEqual(doc,once)

    def test_unknown_sibling_is_not_known_empty_or_canonical_proof(self):
        doc=enrich.blank_document()
        enrich.recover_variant_role_facts(doc,semantic([
            dict(variant_ref='source_hash:a',title='Financial Analyst'),
            dict(variant_ref='source_hash:b',title='Contributor')]))
        self.assertEqual(doc['attributes']['role']['professional_domains'],[])
        self.assertFalse(any('source_hash:b' in f['variant_refs'] for f in doc['variant_facts']))

    def test_existing_empty_conflicting_or_semantic_fact_is_preserved(self):
        data=semantic([dict(variant_ref='source_hash:a',title='Financial Analyst')])
        path='attributes.role.professional_domains'
        for state,value in [('known_empty',None),('known_value','biology')]:
            with self.subTest(state=state):
                doc=enrich.blank_document();fact=enrich.make_variant_fact(path,value,['source_hash:a'],[],knowledge_state=state)
                doc['variant_facts']=[fact];enrich.recover_variant_role_facts(doc,data)
                self.assertEqual([f for f in doc['variant_facts'] if f['field_path']==path],[fact])

    def test_incidental_categories_and_research_participants_do_not_gain_work(self):
        facts=enrich.extract_variant_role_facts(semantic([
            dict(variant_ref='source_hash:a',title='Coding Expert',department='Creator (Writer)'),
            dict(variant_ref='source_hash:b',title='Research Study Participant')]))
        a={f['value'] for f in facts if f['field_path'].endswith('work_activities') and 'source_hash:a' in f['variant_refs']}
        b={f['value'] for f in facts if f['field_path'].endswith('work_activities') and 'source_hash:b' in f['variant_refs']}
        self.assertNotIn('writing_editing',a);self.assertNotIn('research_analysis',b)

    def test_required_body_language_is_scoped_with_quote_and_capture(self):
        data=semantic([dict(variant_ref='source_hash:a',title='Contributor'),
                       dict(variant_ref='source_hash:b',title='Contributor')])
        data['canonical']={}
        data['rich_content'][0].update(source_ref='source_hash:a',provider='alignerr',
            external_id='a',source_url='https://example.test/a',body_format='text/markdown',
            body='# Who You Are\n\n* Fluent in English\n\n# Nice to Have\n\n* Fluent in French',metadata={})
        facts=enrich.extract_deterministic_objective_facts(data)
        langs=[f for f in facts if f['field_path']=='attributes.requirements.languages']
        self.assertEqual([f['value']['language'] for f in langs],['english'])
        self.assertEqual(langs[0]['variant_refs'],['source_hash:a'])
        self.assertEqual(langs[0]['evidence'][0]['evidence_text'],'Fluent in English')
        self.assertIn('source_capture:1',langs[0]['evidence'][0]['authority_refs'])

    def test_alternative_and_negated_languages_are_not_independent_required_facts(self):
        for body in ['# Who You Are\n\n* Fluent in English or French',
                     '# Who You Are\n\n* Fluent in English\n* English is not required']:
            data=semantic([dict(variant_ref='source_hash:a',title='Contributor')]);data['canonical']={}
            data['rich_content'][0].update(source_ref='source_hash:a',body=body,body_format='text/markdown',metadata={})
            facts=enrich.extract_deterministic_objective_facts(data)
            self.assertFalse([f for f in facts if f['field_path']=='attributes.requirements.languages'])


class PreparedConsumerTests(unittest.TestCase):
    setUp=fixtures.BrowseFilterCorrectionTests.setUp
    role_evidence=fixtures.BrowseFilterCorrectionTests.role_evidence

    def test_serving_uses_saved_facts_and_keeps_variant_filter_conjunction(self):
        evidence=self.role_evidence();doc=evidence['effective']['document']
        data=semantic([dict(variant_ref='source_hash:'+r['source_hash'], title=r['source_title'],
                           department=r['source_department'],expertise=r['source_expertise']) for r in evidence['rows']])
        enrich.recover_variant_role_facts(doc,data)
        with patch.object(enrich,'extract_variant_role_facts',side_effect=AssertionError('request-time normalization')):
            jobs=detail.prepare_public_job_variants(evidence,now=NOW)
            for job in jobs:catalog.prepare_catalog_presentation(job)
            self.assertEqual(jobs[0]['_catalog_filter_values']['field'],{'finance'})
            self.assertEqual(jobs[1]['_catalog_filter_values']['field'],{'physics'})
            self.assertEqual(catalog.build_catalog(jobs,{'field':'Finance','location':'Portugal'})['result_count'],0)
            self.assertEqual(catalog.build_catalog(jobs,{'field':'Physics','location':'Portugal'})['result_count'],1)

if __name__=='__main__':unittest.main()
