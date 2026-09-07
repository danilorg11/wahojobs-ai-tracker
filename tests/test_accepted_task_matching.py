"""Public duty excerpts and synthetic contrasts; no personal profile fixtures."""
from contextlib import closing
from hashlib import sha256
import json
import sqlite3
import unittest
from unittest.mock import patch

from tests.test_confirmed_activity_matching import candidate, v2
from tests.authenticated_recommendation_test_support import SyntheticMatcherFixture
from tests.test_source_task_fit import SOURCES
from wahojobs import authenticated_profile_matches as browser
from wahojobs.authenticated_card_evidence import _source_text
from wahojobs.authenticated_variant_details import variant_detail_url
from wahojobs.matching import accepted_tasks


VOICE = "Key Responsibilities\n\n- Rate the AI's responses\n\nIdeal Qualifications\n\n- MUST own a Mac with an Apple Silicon chip."
VIDEO = """### **Job Details:**

- **Evaluate Paired Video Outputs:** Watch two short video clips presented side by side and compare them against a provided rubric across dimensions such as prompt adherence, motion quality, visual artifacts, and physical plausibility.

- **Score and Justify Preferences:** Assign rubric scores to each clip, select an overall preference between the two, and document a brief written justification supporting the decision.

### **Minimum Qualifications:**

- BS or BA from a reputable institution completed or in progress.

### **Preferred Qualifications:**

- 2+ years of experience in teaching or research.
"""


class AcceptedTaskMatchingTests(unittest.TestCase):
    def setUp(self):
        self.f = SyntheticMatcherFixture()
        self.addCleanup(self.f.close)
        self.f.profile = v2(candidate(['Model output evaluation']))
        self.role('Portuguese AI Data Reviewer')

    def role(self, title, location='Remote - Brazil'):
        self.f.update_inventory("UPDATE jobs SET title=?,department='',expertise='',commitment='',location=?", (title, location))
        self.f.update_inventory("UPDATE canonical_opportunities SET canonical_title=?,source_category=''", (title,))

    def source(self, body, *, job_id=7003, source_url=None, body_format='text/plain', metadata=None):
        with closing(sqlite3.connect(self.f.path)) as c, c:
            external, url = c.execute('SELECT external_id,url FROM jobs WHERE id=?', (job_id,)).fetchone()
            c.execute('INSERT OR REPLACE INTO job_source_contents '
                      '(job_id,provider,source_type,source_url,external_id,body,body_format,metadata_json,'
                      'material_content_sha256,first_captured_at,last_captured_at) VALUES '
                      "(?,'configured-production','catalog',?,?,?,?, ?,?,?,?)",
                      (job_id, source_url or url, external, body, body_format,
                       json.dumps(metadata or {}),
                       sha256(body.encode()).hexdigest(), self.f.now.isoformat(), self.f.now.isoformat()))

    def current(self, url='/find-matches'):
        response = self.f.get(url)
        self.assertEqual(response.status, 200)
        run = self.f.last_run()
        return response, run, run.recommendation_context

    def match(self, context, job_id=7003):
        return next(m for rows in context['matches'].values() for m in rows if m['job_id'] == job_id)

    def test_description_only_work_reaches_scoring_and_same_substantive_evidence(self):
        _, old, before = self.current()
        self.assertEqual(self.match(before)['score'], 17)
        self.source(VOICE)
        _, _, after = self.current('/find-matches?run=' + old.match_run_id)
        m = self.match(after)
        self.assertEqual(m['score'], 25)
        self.assertEqual(m['score_components']['profile_signal_score'], 15)
        self.assertEqual(m['accepted_task_fit']['facts'][0]['quote'], "- Rate the AI's responses")
        self.assertEqual(m['accepted_task_fit']['source_reference']['job_id'], 7003)
        self.assertEqual(m['accepted_task_fit']['profile_facts'][0]['path'], 'experience.specialties[0]')
        self.assertIn('AI evaluation or annotation tasks', m['affirmative_fit']['satisfied_groups'])
        self.assertNotIn('Mac', str(m['affirmative_fit']['satisfied_groups']))
        self.assertEqual(self.match(after, 7006)['score'], 17)  # no cross-variant borrowing

    def test_supported_video_tasks_receive_conditional_admission_without_score_change(self):
        self.role('Video Evaluation Generalist')
        _, _, before = self.current()
        self.assertEqual(self.match(before)['score'], 2)
        self.assertEqual(self.match(before)['score_components']['quality_gate_penalty'], 10)
        self.source(VIDEO)
        _, _, after = self.current()
        m = self.match(after)
        self.assertEqual(m['score'], 12)
        self.assertEqual(m['score_components']['profile_signal_score'], 8)
        self.assertEqual(m['score_components']['quality_gate_penalty'], 0)
        self.assertEqual(m['affirmative_fit_status'], 'uncertain')
        self.assertEqual(m['raw_product_section'], 'explore_only')
        self.assertEqual(m['preview_section'], 'also_worth_reviewing')
        self.assertTrue(m['accepted_task_section_admission'])
        self.assertEqual(browser._primary_presentation_matches(after), [])
        self.assertEqual([x['job_id'] for x in browser._conditional_presentation_matches(after)], [7003])
        self.assertIn('AI evaluation or annotation tasks', m['affirmative_fit']['satisfied_groups'])
        self.assertNotIn('teaching', str(m['affirmative_fit']['required_groups']))

    def test_language_aspiration_and_negation_do_not_establish_professional_work(self):
        self.role('Video Evaluation Generalist')
        self.source(VIDEO)
        for activities in ([], ['Interested in AI evaluation'], ['No AI evaluation experience']):
            with self.subTest(activities=activities):
                self.f.profile = v2(candidate(activities))
                _, _, c = self.current()
                m = self.match(c)
                self.assertIsNone(m['accepted_task_fit'])
                self.assertEqual(m['score_components']['quality_gate_penalty'], 10)
                self.assertFalse(browser._primary_presentation_matches(c))

    def test_marketing_preference_and_negated_duties_do_not_become_task_facts(self):
        for body in ("Our company evaluates AI outputs for leading laboratories.",
                     "About us\n\nWe evaluate AI outputs.",
                     "Preferred Qualifications\n\nExperience evaluating AI outputs is preferred.",
                     "Key Responsibilities\n\nYou will not evaluate AI outputs.",
                     "Key Responsibilities\n\nYou will not be asked to evaluate AI outputs.",
                     "Role overview\n\nOur clients evaluate AI outputs.",
                     "Role overview\n\nWe build systems that evaluate AI outputs.",
                     "Role overview\n\nWe seek people interested in evaluating AI outputs."):
            with self.subTest(body=body):
                self.source(body)
                _, _, c = self.current()
                self.assertIsNone(self.match(c)['accepted_task_fit'])
                self.assertEqual(self.match(c)['score'], 17)

    def test_generic_evaluation_does_not_satisfy_specialist_domain(self):
        self.role('Biology Expert')
        self.source('Key Responsibilities\n\nYou will evaluate AI outputs.')
        _, _, c = self.current()
        m = self.match(c)
        self.assertGreaterEqual(m['score_components']['quality_gate_penalty'], 28)
        self.assertNotEqual(m['affirmative_fit_status'], 'supported')
        self.assertFalse(browser._primary_presentation_matches(c))
        self.source('Key Responsibilities\n\nYou will evaluate AI outputs for clinical accuracy.')
        _, _, c = self.current()
        self.assertIsNone(self.match(c)['accepted_task_fit'])

    def test_specialist_linguistic_uncertainty_stays_discoverable_not_qualified(self):
        self.role('Portuguese Language Data Contributor')
        self.source(VOICE + '\n\n' + _source_text(SOURCES[0]))
        _, _, c = self.current()
        self.assertEqual(browser._primary_presentation_matches(c), [])
        conditional = browser._conditional_presentation_matches(c)
        self.assertEqual([m['job_id'] for m in conditional], [7003])
        self.assertEqual(conditional[0]['affirmative_fit_status'], 'uncertain')
        self.assertFalse(conditional[0]['affirmative_fit']['conflicting_requirements'])

    def test_geographic_conflict_and_closure_are_not_overridden(self):
        self.source(VOICE)
        self.role('Bilingual Writer - Portuguese (Brazil)', 'Remote - United States only')
        _, run, c = self.current()
        self.assertEqual(self.match(c)['location_eligibility_status'], 'incompatible')
        self.assertFalse(browser._primary_presentation_matches(c))
        self.assertFalse(browser._conditional_presentation_matches(c))
        self.role('Bilingual Writer - Portuguese (Brazil)')
        self.f.update_inventory('UPDATE jobs SET is_active=0')
        _, _, c = self.current('/find-matches?run=' + run.match_run_id)
        self.assertFalse(browser._primary_presentation_matches(c))

    def test_signal_fires_once_when_title_and_many_description_clauses_match(self):
        self.role('Portuguese AI Evaluator - Data Annotation')
        _, _, before = self.current()
        self.source(VOICE + '\n\n' + VIDEO + '\n\n' + VOICE)
        _, _, after = self.current()
        self.assertEqual(self.match(before)['score_components']['profile_signal_score'], 15)
        self.assertEqual(self.match(after)['score_components']['profile_signal_score'], 15)

    def test_unchanged_material_reuses_preparation_changed_source_invalidates_old_run(self):
        accepted_tasks._prepare.cache_clear()
        self.source(VOICE)
        _, run, before = self.current()
        misses = accepted_tasks._prepare.cache_info().misses
        self.current()  # fresh calculation, not recommendation-result reuse
        self.assertEqual(accepted_tasks._prepare.cache_info().misses, misses)
        self.source('No task description available.')
        _, _, after = self.current('/find-matches?run=' + run.match_run_id)
        self.assertEqual(self.match(before)['score'], 25)
        self.assertEqual(self.match(after)['score'], 17)
        self.f.owner = 'b'
        self.assertEqual(self.f.get('/find-matches?run=' + run.match_run_id).status, 410)

    def test_exact_details_share_task_facts_and_do_not_score_the_catalog(self):
        self.source(VOICE)
        _, run, c = self.current()
        m = self.match(c)
        from wahojobs import authenticated_variant_details as details
        with (patch.object(details, 'prepare_variant_notice', wraps=details.prepare_variant_notice) as notice,
              patch.object(browser.profile_preview, 'query_preview_rows',
                           wraps=browser.profile_preview.query_preview_rows) as inventory_reads):
            r = self.f.get(variant_detail_url(m, run_id=run.match_run_id))
        self.assertEqual(r.status, 200)
        job = notice.call_args.args[0]
        local = job['_authenticated_local_checks']['match']
        self.assertEqual(local['job_id'], m['job_id'])
        self.assertEqual(local['accepted_task_fit'], m['accepted_task_fit'])
        self.assertEqual(local['score'], m['score'])
        self.assertIn(job['official_url'], r.body.decode())
        self.assertEqual(inventory_reads.call_count, 1)
        self.assertEqual(inventory_reads.call_args.kwargs['canonical_opportunity_id'], m['canonical_opportunity_id'])

    def test_cached_task_facts_do_not_renew_expired_availability(self):
        self.source(VOICE)
        _, run, c = self.current()
        self.assertTrue(browser._conditional_presentation_matches(c))
        self.f.advance(24 * 8)
        _, _, c = self.current('/find-matches?run=' + run.match_run_id)
        self.assertFalse(browser._primary_presentation_matches(c))
        self.assertFalse(browser._conditional_presentation_matches(c))

    def test_recent_cache_keeps_source_conditions_instead_of_restoring_main_fit(self):
        self.source(VOICE)
        _, run, context = self.current()
        self.assertTrue(browser._conditional_presentation_matches(context))
        self.f.advance(73)
        response, _, context = self.current('/find-matches?run=' + run.match_run_id)
        self.assertFalse(browser._primary_presentation_matches(context))
        conditional = browser._conditional_presentation_matches(context)
        self.assertEqual([m['job_id'] for m in conditional], [7003])
        self.assertEqual(conditional[0]['presentation_data_status'], 'recently_cached')
        self.assertIn('MUST own a Mac', response.body.decode())
        self.assertIn('Availability needs confirmation.', response.body.decode())
        self.source('Location: Remote (US Only)\n\n' + VOICE)
        _, _, context = self.current('/find-matches?run=' + run.match_run_id)
        self.assertFalse(browser._primary_presentation_matches(context))
        self.assertFalse(browser._conditional_presentation_matches(context))

    def test_source_identity_and_required_vs_preferred_blocks_are_preserved(self):
        self.source(VOICE, source_url='https://different.example.test/offer')
        _, _, c = self.current()
        self.assertIsNone(self.match(c)['accepted_task_fit'])
        self.source('<h2>Key Responsibilities</h2><p>Rate the AI\'s responses.</p>'
                    '<h2>Preferred Qualifications</h2><p>Two years of teaching.</p>', body_format='text/html')
        _, _, c = self.current()
        self.assertTrue(self.match(c)['accepted_task_fit'])
        self.assertNotIn('teaching', str(self.match(c)['accepted_task_fit']))

    def test_html_in_text_field_uses_existing_safe_paragraph_reader(self):
        self.source('<p>About our company</p><p>Our company evaluates AI outputs.</p>'
                    '<p><strong>Responsibilities:</strong></p>'
                    '<ul><li>Review and evaluate AI-generated responses for small business use cases.</li></ul>'
                    '<script>You will evaluate AI outputs for clinical accuracy.</script>'
                    '<p>Requirements:</p><p>Business owner or strong understanding of small business operations.</p>')
        _, _, c = self.current()
        task = self.match(c)['accepted_task_fit']
        self.assertEqual([f['quote'] for f in task['facts']],
                         ['Review and evaluate AI-generated responses for small business use cases.'])
        self.assertNotIn('business owner', str(self.match(c)['affirmative_fit']['satisfied_groups']).lower())

    def test_beginner_opportunity_does_not_acquire_a_professional_requirement(self):
        self.f.profile = v2(candidate())
        self.source('No experience is required.\n\nYou will rate the AI\'s responses.')
        _, _, c = self.current()
        m = self.match(c)
        self.assertIsNone(m['accepted_task_fit'])
        self.assertEqual(m['score'], 17)
        self.assertEqual(m['affirmative_fit_status'], 'supported')
        self.assertFalse(m['affirmative_fit']['missing_requirements'])

    def test_equivalent_duty_wording_reaches_both_consumers_once(self):
        self.role('AI Generalist')
        for duty in (
            'Verbally articulate comparisons and evaluate outputs from both models.',
            'Analyze, evaluate, and annotate a variety of data sets for model training.',
            'Participate in reviewing, refining, and improving model outputs.',
            'Create diverse prompts to challenge large language models.',
            'Develop scoring rubrics for AI responses.',
            'Review video footage and annotate events in the video.',
        ):
            with self.subTest(duty=duty):
                self.source('AI training project.\n\nScope of Work\n\n' + duty)
                _, _, context = self.current()
                m = self.match(context)
                self.assertEqual(m['score'], 12)
                self.assertEqual(m['score_components']['profile_signal_score'], 8)
                self.assertIn(duty, [f['quote'] for f in m['accepted_task_fit']['facts']])
                self.assertIn('AI evaluation or annotation tasks', m['affirmative_fit']['satisfied_groups'])
                self.assertTrue(browser._primary_presentation_matches(context))

    def test_duty_sentence_is_not_a_heading_and_qualified_blocks_stay_separate(self):
        self.role('AI Generalist')
        self.source('Scope of Work\n\n'
                    'Deliver clear feedback based on project requirements.\n\n'
                    'Participate in reviewing and improving model outputs.\n\n'
                    'Preferred Qualifications\n\nYou will evaluate AI responses in your training course.\n\n'
                    'Requirements\n\nYou will have experience evaluating AI outputs.')
        _, _, context = self.current()
        facts = self.match(context)['accepted_task_fit']['facts']
        self.assertEqual([f['quote'] for f in facts], ['Participate in reviewing and improving model outputs.'])
        self.assertTrue(facts[0]['block_reference'].startswith('accepted text paragraph '))

    def test_accepted_structured_duty_blocks_use_the_same_vocabulary_and_identity(self):
        self.role('AI Generalist')
        self.source('An AI training project.', metadata={'lists': [
            {'text': 'Responsibilities', 'content': '<ul><li>Compare responses from AI models.</li></ul>'},
            {'text': 'Preferred Qualifications', 'content': '<p>You will have experience evaluating clinical AI outputs.</p>'},
        ]})
        _, _, context = self.current()
        m = self.match(context)
        self.assertEqual(m['score'], 12)
        self.assertEqual([f['quote'] for f in m['accepted_task_fit']['facts']], ['Compare responses from AI models.'])
        self.assertEqual(m['accepted_task_fit']['facts'][0]['block_reference'], 'metadata.lists[0].content paragraph 1')
        self.assertIsNone(self.match(context, 7006)['accepted_task_fit'])

    def test_unknown_required_condition_is_conditional_and_preferred_is_not_required(self):
        self.role('AI Generalist')
        for heading, expected in (('Requirements', 'conditional'), ('Preferred Qualifications', 'main')):
            with self.subTest(heading=heading):
                self.source("Scope of Work\n\nEvaluate AI outputs.\n\n" + heading + "\n\nBachelor's in Biology.")
                response, _, context = self.current()
                m = self.match(context)
                self.assertEqual(m['score'], 12)
                main = browser._primary_presentation_matches(context)
                conditional = browser._conditional_presentation_matches(context)
                self.assertEqual(bool(main), expected == 'main')
                self.assertEqual(bool(conditional), expected == 'conditional')
                self.assertFalse(m['affirmative_fit']['conflicting_requirements'])
                if conditional:
                    q = m['source_task_fit']['conditions'][0]
                    self.assertEqual(q['status'], 'not_established')
                    self.assertEqual(q['source']['job_id'], 7003)
                    self.assertIn('Qualifications &amp; conditions', response.body.decode())

    def test_explicit_required_condition_conflict_is_not_a_conditional_escape(self):
        self.role('AI Generalist')
        c = candidate(['AI evaluation'])
        c['education']['education_level'] = 'no_degree'
        from wahojobs.profiles.canonical import field_sources_for_profile
        c['provenance']['field_sources'] = field_sources_for_profile(c, 'user_confirmation', explicit=True)
        self.f.profile = v2(c)
        self.source("Scope of Work\n\nEvaluate AI outputs.\n\nRequirements\n\nBachelor's in Biology.")
        _, _, context = self.current()
        self.assertEqual(self.match(context)['affirmative_fit_status'], 'conflicting')
        self.assertFalse(browser._primary_presentation_matches(context))
        self.assertFalse(browser._conditional_presentation_matches(context))

    def test_explicit_must_inside_ideal_block_stays_a_required_unknown(self):
        self.source(VOICE)
        _, _, context = self.current()
        m = self.match(context)
        self.assertTrue(browser._conditional_presentation_matches(context))
        self.assertEqual(m['source_task_fit']['conditions'][0]['modality'], 'required')
        self.assertIn('MUST own', m['source_task_fit']['conditions'][0]['source']['quote'])
        self.assertFalse(m['affirmative_fit']['conflicting_requirements'])

    def test_tool_mention_does_not_confirm_required_proficiency(self):
        self.role('AI Generalist')
        self.f.profile = v2(candidate(['AI evaluation'], skills=['Python']))
        self.source('Scope of Work\n\nEvaluate AI outputs.\n\nRequirements\n\nWorking proficiency in Python or R.')
        _, _, context = self.current()
        m = self.match(context)
        self.assertTrue(browser._conditional_presentation_matches(context))
        q = m['source_task_fit']['conditions'][0]
        self.assertEqual(q['status'], 'not_established')
        self.assertIn('tool mention: Python', q['supported_parts'])
        self.assertFalse(m['affirmative_fit']['conflicting_requirements'])

    def test_no_experience_wording_is_not_a_new_missing_requirement(self):
        self.role('AI Generalist')
        self.source('No prior AI experience is required.\n\nScope of Work\n\n'
                    'Evaluate AI outputs.\n\nPreferred Qualifications\n\nAnnotation experience is a plus, not required.')
        _, _, context = self.current()
        self.assertTrue(browser._primary_presentation_matches(context))
        self.f.profile = v2(candidate())
        _, _, context = self.current()
        m = self.match(context)
        self.assertIsNone(m['accepted_task_fit'])
        self.assertFalse(m['affirmative_fit']['missing_requirements'])
        self.assertFalse(m.get('accepted_task_section_admission'))

    def test_incomplete_source_and_non_task_penalty_do_not_get_section_admission(self):
        self.role('AI Generalist')
        self.source('Help the future of AI. Our researchers evaluate model outputs.')
        _, _, context = self.current()
        self.assertIsNone(self.match(context)['accepted_task_fit'])
        self.assertFalse(browser._primary_presentation_matches(context))
        self.source('Scope of Work\n\nEvaluate AI outputs.')
        _, _, context = self.current()
        m = self.match(context)
        from copy import deepcopy
        for field in ('avoid_keyword_penalty', 'quality_gate_penalty', 'specialist_domain_penalty'):
            penalized = deepcopy(m)
            penalized.update(preview_section='explore_only', accepted_task_section_admission=False)
            penalized['score_components'][field] = 10
            self.assertEqual(accepted_tasks.apply_task_section_admission(penalized)['preview_section'], 'explore_only')
        profession = deepcopy(m)
        profession.update(preview_section='explore_only', accepted_task_section_admission=False)
        profession['affirmative_fit']['required_groups'] = [dict(source='title', label='Separate profession')]
        self.assertEqual(accepted_tasks.apply_task_section_admission(profession)['preview_section'], 'explore_only')

    def test_source_only_applicant_restriction_cannot_be_bypassed_by_task_admission(self):
        self.role('AI Generalist', 'Remote')
        for source in (
            'Location: Remote (US Only)\n\nScope of Work\n\nEvaluate AI outputs.',
            'Location: Must be currently based in the United States.\n\nScope of Work\n\nEvaluate AI outputs.',
            'Scope of Work\n\nEvaluate AI outputs.\n\n**What we’re looking for**\n\n- Based in the United States or Canada',
            '<h2>Scope of Work</h2><p>Evaluate AI outputs.</p><h2>Requirements</h2>'
            '<p>Must be currently based in the United States. Applications from other countries will not be considered.</p>',
        ):
            with self.subTest(source=source):
                self.source(source)
                _, _, context = self.current()
                m = self.match(context)
                self.assertEqual(m['score'], 12)
                self.assertFalse(browser._primary_presentation_matches(context))
                self.assertFalse(browser._conditional_presentation_matches(context))
                self.assertEqual(m['source_task_location_checks'][0]['status'], 'incompatible')
                self.assertIn('accepted_task_source_location', m['primary_admission_reasons'])

    def test_source_location_alternatives_and_preferred_region_remain_distinct(self):
        self.role('AI Generalist', 'Remote')
        for heading, line in (
            ('Requirements', 'Based in Brazil or Canada'),
            ('Preferred Qualifications', 'Based in Portugal or Western Europe'),
        ):
            with self.subTest(heading=heading):
                self.source('Scope of Work\n\nEvaluate AI outputs.\n\n' + heading + '\n\n' + line)
                _, _, context = self.current()
                self.assertTrue(browser._primary_presentation_matches(context)
                                or browser._conditional_presentation_matches(context))
                self.assertFalse(self.match(context)['affirmative_fit']['conflicting_requirements'])

    def test_explicit_onsite_location_unknown_to_country_comparator_is_not_permission(self):
        self.role('AI Generalist', 'Remote')
        self.source('Location: Example City (On-site)\n\nScope of Work\n\nEvaluate AI outputs.')
        _, _, context = self.current()
        self.assertFalse(browser._primary_presentation_matches(context))
        self.assertFalse(browser._conditional_presentation_matches(context))
        self.assertEqual(self.match(context)['source_task_location_checks'][0]['status'], 'unknown')
        self.assertFalse(self.match(context)['affirmative_fit']['conflicting_requirements'])

    def test_qualification_ability_is_not_an_assigned_duty(self):
        self.role('AI Generalist')
        self.source('Role Overview\n\nAI training project.\n\nKey Qualifications\n\n'
                    'Ability to evaluate nuanced AI outputs.')
        _, _, context = self.current()
        self.assertIsNone(self.match(context)['accepted_task_fit'])
        self.assertFalse(browser._primary_presentation_matches(context))

    def test_curly_headings_and_html_labels_preserve_material_conditions(self):
        self.role('AI Generalist')
        for source in (
            'Scope of Work\n\nEvaluate AI outputs.\n\n**What we’re looking for**\n\nSound judgment around sensitive content.',
            '<h2>Scope of Work</h2><p>Evaluate AI outputs.</p><h2>Requirements</h2>'
            '<p>Must own a specialized recording device.</p>',
        ):
            with self.subTest(source=source):
                self.source(source)
                response, _, context = self.current()
                m = self.match(context)
                self.assertTrue(browser._conditional_presentation_matches(context))
                self.assertTrue(m['source_task_fit']['conditions'])
                self.assertIn('Qualifications &amp; conditions', response.body.decode())

    def test_structured_required_blocks_are_not_lost_after_task_projection(self):
        self.role('AI Generalist')
        self.source('AI training project.', metadata={'lists': [
            {'text': 'Responsibilities', 'content': '<p>Evaluate AI outputs.</p>'},
            {'text': 'Requirements', 'content': '<p>Must own a specialized recording device.</p>'},
        ]})
        response, _, context = self.current()
        m = self.match(context)
        self.assertEqual(m['score'], 12)
        self.assertTrue(browser._conditional_presentation_matches(context))
        self.assertIn('specialized recording device', response.body.decode())
        self.assertEqual(m['source_task_fit']['conditions'][0]['source']['job_id'], 7003)
