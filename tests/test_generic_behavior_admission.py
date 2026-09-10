"""Offline source labels test integration, not automatic semantic quality."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3
import unittest

from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from tests import test_accepted_task_matching as task_tests
from tests.test_confirmed_activity_matching import candidate, v2
from wahojobs import authenticated_profile_matches as browser
from wahojobs.authenticated_card_evidence import load_card_sources, prepare_card_evidence
from wahojobs.authenticated_variant_details import variant_detail_url
from wahojobs.crawler.types import JobCandidate
from wahojobs.db.repository import upsert_job_source_content
from wahojobs.source_capture import SourceCaptureContext
from wahojobs import opportunity_enrichment as oe
from wahojobs.opportunity_llm import DEFAULT_MODEL, PROMPT_VERSION, StructuredEnrichmentResult, structured_output_schema
from wahojobs.profiles.canonical import field_sources_for_profile
from wahojobs.source_clause_materiality import FIELD, clause_catalog, accept_annotations, generic_annotation, attach_current_annotations

BEHAVIOR = 'Exceptionally detail-oriented with a patient, methodical approach to work'
RELIABLE = 'Self-motivated and reliable when working independently'
WHO = [
    'Native Brazilian Portuguese speaker with an excellent command of written Portuguese',
    'Strong listening skills — you can parse spoken language accurately, even when audio quality varies',
    BEHAVIOR,
    'Comfortable working at the word level — you care about every accent, comma, and timestamp',
    'Able to follow detailed style guides and formatting rules with precision and consistency',
    RELIABLE,
    'Comfortable using web-based tools and audio playback interfaces',
]


class LabelledSemanticOutput:
    """Transparent test output; never an implemented automatic classifier."""
    provider = 'openai'
    model = DEFAULT_MODEL
    prompt_version = PROMPT_VERSION

    def __init__(self, labels):
        self.labels, self.calls = labels, []

    def enrich(self, packet):
        self.calls.append(deepcopy(packet))
        payload = oe.blank_llm_payload()
        payload[FIELD] = [dict(clause_id=c['clause_id'], classification=self.labels[c['quote']])
                          for c in packet['qualification_clauses'] if c['quote'] in self.labels]
        return StructuredEnrichmentResult(payload, 'synthetic-offline-output', 0, 0, 0, None)


class GenericBehaviorAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.f = SyntheticMatcherFixture()
        self.addCleanup(self.f.close)
        self.f.profile = v2(candidate(['Model output evaluation']))
        task_tests.AcceptedTaskMatchingTests.role(self, 'AI Generalist', 'Remote - Brazil')
        self.f.update_inventory("UPDATE canonical_opportunities SET source_category='AI'")

    def source(self, clauses, heading='Who You Are', extra='', job_id=7003, provider_outcome='success'):
        # Long, explicit source tasks satisfy the existing enrichment input floor.
        body = ("## Scope of Work\n\nEvaluate AI outputs. Compare responses against a provided rubric "
                "and document the reasons for each judgment. Review model outputs for accuracy, "
                "consistency and relevance to the supplied prompts. Record the evaluation results "
                "using the supplied project instructions. The project provides example responses "
                "and reference rubrics for each task. Review the examples, compare the outputs, "
                "and record the reasons for the chosen evaluation.\n\n## " + heading + '\n\n'
                + '\n'.join('* ' + q for q in clauses) + extra)
        with closing(sqlite3.connect(self.f.path)) as c, c:
            c.row_factory = sqlite3.Row
            row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            job = JobCandidate(external_id=row['external_id'], title=row['title'], location=row['location'],
                               url=row['url'], source_hash=row['source_hash'], department=row['department'],
                               expertise=row['expertise'], commitment=row['commitment'], source_body=body,
                               source_body_format='text/markdown', source_metadata={})
            upsert_job_source_content(c, job_id, 'configured-production', 'fixture', job,
                self.f.now.isoformat(), capture_context=SourceCaptureContext(
                    crawl_run_id=None, provider_outcome=provider_outcome, used_sample_data=False,
                    snapshot_complete=True, pagination_complete=True, empty_snapshot_validated=False,
                    raw_record_count=1, normalized_record_count=1, candidate_count=1, rejected_record_count=0,
                    payload_shape='synthetic-fixture', schema_fingerprint='synthetic-fixture'))
        return body

    def enrich(self, labels, canonical=7002):
        client = LabelledSemanticOutput(labels)
        with closing(sqlite3.connect(self.f.path)) as c, c:
            c.row_factory = sqlite3.Row
            result = oe.enrich_canonical_opportunity(c, canonical, llm_client=client,
                                                     ensure_schema=False, now=self.f.now.isoformat())
        self.assertEqual(result['llm']['outcome'], 'succeeded')
        self.assertEqual(len(client.calls), 1)
        return result, client

    def current(self, old=None):
        response = self.f.get('/find-matches' + ('?run=' + old.match_run_id if old else ''))
        self.assertEqual(response.status, 200)
        run = self.f.last_run()
        ctx = run.recommendation_context
        match = next(m for rows in ctx['matches'].values() for m in rows if m['job_id'] == 7003)
        return response, run, ctx, match

    def placement(self, ctx):
        return ([m['job_id'] for m in browser._primary_presentation_matches(ctx)],
                [m['job_id'] for m in browser._conditional_presentation_matches(ctx)])

    def test_enrichment_to_main_preserves_questions_scores_profile_and_details(self):
        body = self.source([BEHAVIOR, RELIABLE])
        profile = deepcopy(self.f.profile)
        _, old, ctx, before = self.current()
        self.assertIn(7003, self.placement(ctx)[1])
        _, client = self.enrich({BEHAVIOR: 'generic_behavior_only', RELIABLE: 'generic_behavior_only'})
        response, run, ctx, after = self.current(old)
        self.assertIn(7003, self.placement(ctx)[0]); self.assertNotIn(7003, self.placement(ctx)[1])
        self.assertEqual(after['score_components'], before['score_components'])
        self.assertEqual(self.f.profile, profile)
        questions = after['non_decisive_source_questions']
        self.assertEqual(len(questions), 2)
        self.assertTrue(all(q['status']=='unresolved' and q['modality']=='unspecified'
                            and not q['admission_decisive'] for q in questions))
        for q in questions:
            self.assertEqual(q['materiality']['basis'], 'llm_source_evidence')
            self.assertEqual(q['materiality']['provenance']['prompt_version'], PROMPT_VERSION)
        for field in ('location_eligibility_status', 'opportunity_trust_status', 'actionability_cap_reasons'):
            self.assertEqual(after[field], before[field])
        for text in (BEHAVIOR, RELIABLE, 'Not assessed against your profile'):
            self.assertIn(text, response.body.decode())
        detail = self.f.get(variant_detail_url(after, run_id=run.match_run_id))
        self.assertEqual(detail.status, 200)
        self.assertIn(BEHAVIOR, detail.body.decode())
        self.assertNotEqual(old.match_run_id, run.match_run_id)
        schema = structured_output_schema([], clause_ids=[c['clause_id'] for c in client.calls[0]['qualification_clauses']])
        self.assertIn(FIELD, schema['properties'])

    def test_no_task_support_is_not_manufactured(self):
        self.f.profile = v2(candidate(['Interested in model output evaluation']))
        self.source([BEHAVIOR]); _, _, ctx, before = self.current()
        self.enrich({BEHAVIOR:'generic_behavior_only'}); _, _, ctx, after = self.current()
        self.assertIsNone(after['accepted_task_fit'])
        self.assertEqual(self.placement(ctx)[0], [])
        self.assertEqual(after['score_components'], before['score_components'])
        self.assertNotIn('non_decisive_source_questions', after)

    def test_alignerr_public_clause_wording_retains_five_specific_questions(self):
        p = candidate(['Model output evaluation', 'Annotation and labeling'])
        for language in p['languages']:
            if language['language']=='Portuguese': language['proficiency']='native'
        p['provenance']['field_sources'] = field_sources_for_profile(p, 'user_confirmation', explicit=True)
        self.f.profile = v2(p)
        self.source(WHO, extra='\n\n## Nice to Have\n\n* Experience as a transcriptionist, court reporter, or stenographer')
        _, _, _, before = self.current()
        self.enrich({q:'generic_behavior_only' if q in (BEHAVIOR, RELIABLE) else 'specific_or_mixed' for q in WHO})
        _, _, ctx, after = self.current()
        self.assertIn(7003, self.placement(ctx)[1]); self.assertNotIn(7003, self.placement(ctx)[0])
        self.assertEqual(len(after['source_task_fit']['conditions']), 5)
        self.assertEqual(after['score_components'], before['score_components'])
        self.assertTrue(any(c['status']=='supported' for c in after['source_language_checks']))

    def test_required_material_unknown_and_explicit_conflict_survive(self):
        for no_degree in (False, True):
            with self.subTest(no_degree=no_degree):
                p = candidate(['Model output evaluation'])
                if no_degree: p['education']['education_level']='no_degree'
                p['provenance']['field_sources']=field_sources_for_profile(p,'user_confirmation',explicit=True)
                self.f.profile=v2(p)
                self.source([BEHAVIOR], extra="\n\n## Requirements\n\n* Bachelor's in Biology.")
                if not no_degree:self.enrich({BEHAVIOR:'generic_behavior_only'})
                _, _, ctx, m = self.current()
                self.assertNotIn(7003,self.placement(ctx)[0])
                self.assertEqual(7003 in self.placement(ctx)[1],not no_degree)
                self.assertEqual(m['affirmative_fit_status'],'conflicting' if no_degree else 'uncertain')

    def test_modality_stays_separate(self):
        for heading,mode in [('Requirements','required'),('Who You Are','unspecified'),('Nice to Have','preferred')]:
            with self.subTest(heading=heading):
                self.source([BEHAVIOR],heading)
                self.enrich({BEHAVIOR:'generic_behavior_only'})
                _,_,ctx,m=self.current();self.assertIn(7003,self.placement(ctx)[0])
                with self.f.provider() as c:s=load_card_sources(c,[m])[7003]
                row=prepare_card_evidence(m,s,self.f.profile)['comparisons'][0]
                self.assertEqual(row['modality'],mode);self.assertEqual(row['status'],'unresolved')

    def test_missing_ambiguous_and_mixed_annotations_do_not_exempt(self):
        for label in (None,'ambiguous','specific_or_mixed'):
            with self.subTest(label=label):
                if label is not None:
                    self.setUp()  # separate source inventories, not quality retries
                self.source([BEHAVIOR + ' and edit audio timestamps precisely.'])
                if label:self.enrich({BEHAVIOR + ' and edit audio timestamps precisely.':label})
                _,_,ctx,m=self.current();self.assertIn(7003,self.placement(ctx)[1])
                self.assertNotIn('non_decisive_source_questions',m)

    def test_same_source_change_and_variant_isolation(self):
        self.source([BEHAVIOR]);self.enrich({BEHAVIOR:'generic_behavior_only'})
        _,old,ctx,_=self.current();self.assertIn(7003,self.placement(ctx)[0])
        with self.f.provider() as c:
            sources=load_card_sources(c,[{'job_id':7003},{'job_id':7006}])
        self.assertIn(FIELD,sources[7003]);self.assertNotIn(FIELD,sources[7006])
        self.source([BEHAVIOR + ' while checking every audio timestamp.'])
        _,_,ctx,m=self.current(old);self.assertIn(7003,self.placement(ctx)[1])
        self.assertNotIn('non_decisive_source_questions',m)

    def test_mixed_canonical_links_preserve_sources_and_current_annotation(self):
        self.source([BEHAVIOR])
        self.f.update_inventory('UPDATE jobs SET canonical_opportunity_id=NULL WHERE id=7006')
        matches = [{'job_id': 7003}, {'job_id': 7006}]
        with self.f.provider() as c:
            original = load_card_sources(c, matches)
        self.enrich({BEHAVIOR: 'generic_behavior_only'})
        with self.f.provider() as c:
            loaded = load_card_sources(c, matches)
            from scripts.profile_to_matches_preview import query_preview_rows
            projected = {r['job_id']: r for r in query_preview_rows(c)}
        self.assertEqual(set(loaded), {7003, 7006})
        self.assertIsNone(loaded[7006]['canonical_opportunity_id'])
        self.assertNotIn(FIELD, loaded[7006])
        self.assertEqual(loaded[7006], original[7006])
        self.assertEqual({k: v for k, v in loaded[7003].items() if k != FIELD}, original[7003])
        annotation = loaded[7003][FIELD][0]
        self.assertEqual(annotation['classification'], 'generic_behavior_only')
        self.assertEqual(annotation['provenance']['prompt_version'], PROMPT_VERSION)
        self.assertEqual(annotation['source']['variant_ref'], 'source_hash:' + loaded[7003]['source_hash'])
        self.assertIn(7003, projected)
        self.assertIn(7006, projected)
        self.assertIsNone(projected[7006]['canonical_opportunity_id'])
        row = prepare_card_evidence(projected[7003], loaded[7003], self.f.profile)['comparisons'][0]
        self.assertIsNotNone(generic_annotation(row, loaded[7003]))

    def test_all_unlinked_sources_remain_without_canonical_lookup(self):
        self.source([BEHAVIOR])
        self.enrich({BEHAVIOR: 'generic_behavior_only'})
        self.f.update_inventory('UPDATE jobs SET canonical_opportunity_id=NULL WHERE id IN (7003,7006)')
        with self.f.provider() as c:
            statements = []
            c.set_trace_callback(statements.append)
            loaded = load_card_sources(c, [{'job_id': 7003}, {'job_id': 7006}])
            before = deepcopy(loaded)
            self.assertIs(attach_current_annotations(c, loaded), loaded)
        self.assertEqual(set(loaded), {7003, 7006})
        self.assertEqual(loaded, before)
        self.assertTrue(all(s['canonical_opportunity_id'] is None and FIELD not in s
                            for s in loaded.values()))
        self.assertFalse(any('opportunity_enrichments' in s.lower() for s in statements))

    def test_empty_sources_do_not_query_annotations(self):
        with self.f.provider() as c:
            statements = []
            c.set_trace_callback(statements.append)
            sources = {}
            self.assertIs(attach_current_annotations(c, sources), sources)
            self.assertEqual(load_card_sources(c, []), {})
        self.assertEqual(statements, [])

    def test_invalid_alias_duplicate_and_partial_clause_rejected_at_ingestion(self):
        self.source([BEHAVIOR + ' and demonstrate excellent written Portuguese.'])
        with self.f.provider() as c:catalog=clause_catalog(oe.load_semantic_input(c,7002))
        alias=next(iter(catalog));item=dict(clause_id=alias,classification='generic_behavior_only')
        for invalid in ([dict(item,clause_id='invented')],[item,item],[dict(item,quote=BEHAVIOR)],
                        [dict(item,classification='supported')]):
            with self.assertRaises(oe.EnrichmentValidationError):accept_annotations(invalid,catalog)
        self.assertEqual(len(catalog),1)
        self.assertIn('excellent written Portuguese',catalog[alias]['quote'])

    def test_annotation_never_overrides_recognized_condition_or_bad_provenance(self):
        self.source([BEHAVIOR]);self.enrich({BEHAVIOR:'generic_behavior_only'})
        _,_,_,m=self.current()
        with self.f.provider() as c:source=load_card_sources(c,[m])[7003]
        row=prepare_card_evidence(m,source,self.f.profile)['comparisons'][0]
        self.assertIsNotNone(generic_annotation(row,source))
        for update in ({'status':'contradicted'},{'kind':'education'},{'modality':'conflicting'}):
            self.assertIsNone(generic_annotation(dict(row,**update),source))
        bad=deepcopy(source);bad[FIELD][0].pop('provenance')
        self.assertIsNone(generic_annotation(row,bad))
        for key in ('url','source_slug','external_id','source_hash','material_content_sha256'):
            bad=deepcopy(source);bad[key]='other'
            self.assertIsNone(generic_annotation(row,bad))

    def test_legacy_document_and_annotation_absence_keep_conservative_behavior(self):
        oe.validate_enrichment_document(oe.blank_document())
        self.source([BEHAVIOR]);self.enrich({})
        _,_,ctx,m=self.current();self.assertIn(7003,self.placement(ctx)[1])
        self.assertNotIn('non_decisive_source_questions',m)

    def test_failed_classification_keeps_conservative_content_and_question(self):
        self.source([BEHAVIOR])
        client=LabelledSemanticOutput({BEHAVIOR:'invented-classification'})
        with closing(sqlite3.connect(self.f.path)) as c, c:
            c.row_factory=sqlite3.Row
            result=oe.enrich_canonical_opportunity(c,7002,llm_client=client,
                ensure_schema=False,now=self.f.now.isoformat())
        self.assertEqual(result['llm']['outcome'],'failed')
        _,_,ctx,m=self.current()
        self.assertIn(7003,self.placement(ctx)[1])
        self.assertNotIn('non_decisive_source_questions',m)

    def test_stale_recipe_and_invalid_annotation_fail_closed_at_source_boundary(self):
        self.source([BEHAVIOR]);self.enrich({BEHAVIOR:'generic_behavior_only'})
        _,_,_,m=self.current()
        with closing(sqlite3.connect(self.f.path)) as c, c:
            c.row_factory=sqlite3.Row
            row=c.execute('SELECT * FROM opportunity_enrichments WHERE canonical_opportunity_id=7002').fetchone()
            original=row['automatic_document_json']
            c.execute("UPDATE opportunity_enrichments SET derivation_fingerprint='stale' WHERE canonical_opportunity_id=7002")
            self.assertNotIn(FIELD,load_card_sources(c,[m])[7003])
            c.execute('UPDATE opportunity_enrichments SET derivation_fingerprint=? WHERE canonical_opportunity_id=7002',
                      (row['derivation_fingerprint'],))
            for value in ('[]',json.dumps(dict(json.loads(original),clause_materiality=[{}]))):
                c.execute('UPDATE opportunity_enrichments SET automatic_document_json=? WHERE canonical_opportunity_id=7002',(value,))
                self.assertNotIn(FIELD,load_card_sources(c,[m])[7003])

    def test_explicit_language_conflict_still_blocks_a_generic_exception(self):
        p=candidate(['Model output evaluation'])
        p['languages'].append(dict(language='German',proficiency='basic',locale='',confidence='high'))
        p['provenance']['field_sources']=field_sources_for_profile(p,'user_confirmation',explicit=True)
        self.f.profile=v2(p)
        self.source([BEHAVIOR],extra='\n\n## Requirements\n\n* Native German required.')
        self.enrich({BEHAVIOR:'generic_behavior_only'})
        _,_,ctx,m=self.current()
        self.assertNotIn(7003,self.placement(ctx)[0]+self.placement(ctx)[1])
        self.assertTrue(any(q['status']=='contradicted' for q in m['source_language_checks']))

    def test_retained_annotation_cannot_bypass_geography_or_expired_availability(self):
        self.source([BEHAVIOR]);self.enrich({BEHAVIOR:'generic_behavior_only'})
        _,_,ctx,_=self.current();self.assertIn(7003,self.placement(ctx)[0])
        self.f.advance(24*8)
        _,_,ctx,_=self.current();self.assertNotIn(7003,self.placement(ctx)[0]+self.placement(ctx)[1])
        self.setUp()
        self.f.update_inventory("UPDATE jobs SET location='Remote - United States only' WHERE id=7003")
        self.source([BEHAVIOR]);self.enrich({BEHAVIOR:'generic_behavior_only'})
        _,_,ctx,m=self.current()
        self.assertEqual(m['location_eligibility_status'],'incompatible')
        self.assertNotIn(7003,self.placement(ctx)[0]+self.placement(ctx)[1])

    def test_recognized_material_condition_stays_decisive_even_if_model_mislabels_it(self):
        quote="Bachelor's in Biology."
        self.source([quote],heading='Requirements')
        # An erroneous semantic label must not override an existing recognized
        # comparison. This does not prove semantic safety for unrecognized prose.
        self.enrich({quote:'generic_behavior_only'})
        _,_,ctx,m=self.current()
        self.assertIn(7003,self.placement(ctx)[1]);self.assertNotIn(7003,self.placement(ctx)[0])
        self.assertNotIn('non_decisive_source_questions',m)

    def ordinary_enrichment(self, client):
        with closing(sqlite3.connect(self.f.path)) as c, c:
            c.row_factory = sqlite3.Row
            return oe.enrich_canonical_opportunity(c, 7002, llm_client=client,
                ensure_schema=False, now=self.f.now.isoformat())

    def binding_state(self):
        with self.f.provider() as c:
            semantic = oe.load_semantic_input(c, 7002)
            stored = c.execute('SELECT * FROM opportunity_enrichments WHERE canonical_opportunity_id=7002').fetchone()
            source = load_card_sources(c, [{'job_id':7003}])[7003]
            accepted = c.execute('SELECT accepted_capture_id FROM job_source_content_acceptances WHERE job_id=7003').fetchone()[0]
            return (accepted, semantic, oe.classify_enrichment_freshness(semantic, stored),
                    source.get(FIELD), json.loads(stored['automatic_document_json']))

    def test_identical_accepted_recapture_regenerates_once_through_ordinary_entry(self):
        self.source([BEHAVIOR])
        client = LabelledSemanticOutput({BEHAVIOR:'generic_behavior_only'})
        first = self.ordinary_enrichment(client)
        self.assertEqual(first['llm']['outcome'], 'succeeded')
        a, semantic_a, freshness_a, usable_a, _ = self.binding_state()
        ref_a = usable_a[0]['source']['accepted_capture_ref']
        self.assertEqual(freshness_a['clause_binding_status'], 'current')
        _, old, ctx, before = self.current()
        self.assertEqual(self.placement(ctx), ([7003], []))
        repeated_a = self.ordinary_enrichment(client)
        self.assertEqual(repeated_a['llm']['outcome'], 'already_enriched')
        self.assertFalse(repeated_a['llm']['called']); self.assertEqual(len(client.calls), 1)

        self.f.advance(1)
        self.source([BEHAVIOR])  # actual healthy, identical-content ingestion
        b, semantic_b, freshness_b, usable_b, stored_a = self.binding_state()
        self.assertNotEqual(a, b)
        ref_b = next(iter(clause_catalog(semantic_b).values()))['accepted_capture_ref']
        self.assertNotEqual(ref_a, ref_b)
        self.assertEqual(stored_a[FIELD][0]['source']['accepted_capture_ref'], ref_a)
        self.assertEqual(oe.semantic_input_sha256(semantic_a), oe.semantic_input_sha256(semantic_b))
        self.assertEqual(freshness_b['source_input_status'], 'current')
        self.assertEqual(freshness_b['derivation_status'], 'current')
        self.assertEqual(freshness_b['clause_binding_status'], 'stale')
        self.assertIn('clause_evidence_binding_changed', freshness_b['stale_reasons'])
        self.assertIsNone(usable_b)
        _, stale_run, ctx, stale = self.current(old)
        self.assertEqual(self.placement(ctx), ([], [7003]))
        self.assertEqual(stale['score_components'], before['score_components'])
        # A deterministic-only invocation preserves A; it cannot relink it to B.
        self.assertEqual(self.ordinary_enrichment(None)['llm']['outcome'], 'not_requested')
        repaired = self.ordinary_enrichment(client)
        self.assertTrue(repaired['llm']['eligible']); self.assertTrue(repaired['llm']['called'])
        self.assertEqual(repaired['llm']['outcome'], 'succeeded'); self.assertEqual(len(client.calls), 2)
        _, _, current_b, usable_b, _ = self.binding_state()
        self.assertEqual(current_b['clause_binding_status'], 'current')
        self.assertEqual(usable_b[0]['source']['accepted_capture_ref'], ref_b)
        self.assertNotEqual(usable_a[0]['clause_id'], usable_b[0]['clause_id'])
        _, _, ctx, after = self.current(stale_run)
        self.assertEqual(self.placement(ctx), ([7003], []))
        self.assertEqual(after['score_components'], before['score_components'])
        self.assertEqual(after['non_decisive_source_questions'][0]['status'], 'unresolved')
        repeated_b = self.ordinary_enrichment(client)
        self.assertEqual(repeated_b['llm']['outcome'], 'already_enriched')
        self.assertFalse(repeated_b['llm']['called']); self.assertEqual(len(client.calls), 2)

    def test_held_observation_does_not_regenerate_unchanged_accepted_binding(self):
        self.source([BEHAVIOR]); client = LabelledSemanticOutput({BEHAVIOR:'generic_behavior_only'})
        self.ordinary_enrichment(client)
        a, _, _, usable_a, _ = self.binding_state()
        with self.f.provider() as c:
            count_a = c.execute('SELECT count(*) FROM job_source_content_captures WHERE job_id=7003').fetchone()[0]
        self.f.advance(1); self.source([BEHAVIOR], provider_outcome='anomalous')
        b, _, fresh, usable_b, _ = self.binding_state()
        with self.f.provider() as c:
            count_b = c.execute('SELECT count(*) FROM job_source_content_captures WHERE job_id=7003').fetchone()[0]
            decision = c.execute('SELECT promotion_decision FROM job_source_content_captures WHERE job_id=7003 ORDER BY id DESC LIMIT 1').fetchone()[0]
        self.assertGreater(count_b, count_a); self.assertEqual(a, b)
        self.assertEqual(decision, 'held_non_authoritative')
        self.assertEqual(usable_a, usable_b); self.assertEqual(fresh['clause_binding_status'], 'current')
        self.assertEqual(self.ordinary_enrichment(client)['llm']['outcome'], 'already_enriched')
        self.assertEqual(len(client.calls), 1)

    def test_stale_replacement_is_failed_not_repaired_and_same_binding_attempt_is_not_repeated(self):
        self.source([BEHAVIOR]); client = LabelledSemanticOutput({BEHAVIOR:'generic_behavior_only'})
        self.ordinary_enrichment(client)
        a, _, _, usable_a, _ = self.binding_state()
        stale_alias = usable_a[0]['clause_id']
        class StaleOutput(LabelledSemanticOutput):
            def enrich(self, packet):
                self.calls.append(deepcopy(packet))
                payload = oe.blank_llm_payload()
                payload[FIELD] = [dict(clause_id=stale_alias, classification='generic_behavior_only')]
                return StructuredEnrichmentResult(payload, 'synthetic-stale-output', 0, 0, 0, None)
        stale_client = StaleOutput({})
        self.f.advance(1); self.source([BEHAVIOR])
        result = self.ordinary_enrichment(stale_client)
        self.assertEqual(result['llm']['outcome'], 'failed')
        self.assertTrue(result['llm']['preserved_previous_success'])
        b, _, freshness, usable, stored = self.binding_state()
        self.assertNotEqual(a, b); self.assertIsNone(usable)
        self.assertEqual(freshness['clause_binding_status'], 'stale')
        self.assertEqual(stored[FIELD][0]['clause_id'], stale_alias)
        _, _, ctx, _ = self.current(); self.assertEqual(self.placement(ctx), ([], [7003]))
        repeated = self.ordinary_enrichment(stale_client)
        self.assertEqual(repeated['llm']['outcome'], 'already_attempted')
        self.assertFalse(repeated['llm']['called']); self.assertEqual(len(stale_client.calls), 1)
        with self.f.provider() as c:
            outcomes = [r[0] for r in c.execute('SELECT outcome FROM opportunity_enrichment_runs WHERE canonical_opportunity_id=7002 ORDER BY id')]
        self.assertEqual(outcomes, ['succeeded', 'failed'])
        # A later accepted binding is a new repair input, not an automatic retry at B.
        self.f.advance(1); self.source([BEHAVIOR])
        self.assertEqual(self.ordinary_enrichment(client)['llm']['outcome'], 'succeeded')
        self.assertEqual(len(client.calls), 2)
        _, _, ctx, _ = self.current(); self.assertEqual(self.placement(ctx), ([7003], []))

    def test_unannotated_legacy_and_deterministic_documents_keep_existing_skip_behavior(self):
        self.source([BEHAVIOR]); client = LabelledSemanticOutput({})
        self.ordinary_enrichment(client)
        # Remove only the additive empty field to represent a legacy document.
        with closing(sqlite3.connect(self.f.path)) as c, c:
            document = json.loads(c.execute('SELECT automatic_document_json FROM opportunity_enrichments WHERE canonical_opportunity_id=7002').fetchone()[0])
            document.pop(FIELD)
            c.execute('UPDATE opportunity_enrichments SET automatic_document_json=? WHERE canonical_opportunity_id=7002', (json.dumps(document),))
        self.f.advance(1); self.source([BEHAVIOR])
        self.assertEqual(self.binding_state()[2]['clause_binding_status'], 'absent')
        self.assertEqual(self.ordinary_enrichment(client)['llm']['outcome'], 'already_enriched')
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.ordinary_enrichment(None)['llm']['outcome'], 'not_requested')
        _, _, ctx, _ = self.current(); self.assertEqual(self.placement(ctx), ([], [7003]))
