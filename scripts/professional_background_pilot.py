"""Bounded disposable pilot. Default dry-run; real requests require all gates.

Use the reviewed isolated launcher documented alongside this script. Synthetic
authorization is explicit; this never opens live account/profile storage.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path

from tests.professional_background_preparation_support import PreparationFixture, OfflineClient
from tests.test_accepted_title_uncertainty import profile
from tests import test_professional_background_components as cases
from wahojobs.opportunity_llm import configured_openai_client
from wahojobs.professional_background_preparation import PreparationBudget


QUALITY_HYPOTHESES = {
    'P01': 'No confirmed professional role; no model request is warranted.',
    'P02': 'Assess a partial support-work relation. Ambiguous or no support is a valid conservative outcome; do not force partial support.',
    'C01_unrelated': 'Biology practice does not by itself establish customer-support practice.',
    'C02_shortfall': 'Assess occupational relevance only; the confirmed two-year shortfall is a separate deterministic safeguard.',
    'C03_exact_boundary': 'Same occupational comparison as C02; five years is independently evaluated by exact arithmetic.',
    'C04_alternative': 'Read the full associated degree alternative; occupational output cannot waive or veto it.',
    'C05_proficiency': 'Occupational relevance is separate from the required English proficiency conflict.',
}


def run_pilot(output_dir, *, mode='dry-run', budget=None, authorize_real_requests=False,
              service_tier='default'):
    if mode not in ('dry-run', 'offline', 'real'):
        raise ValueError('invalid_pilot_mode')
    if type(service_tier) is not str or service_tier != 'default':
        raise ValueError('pilot_explicit_standard_tier_required')
    if mode == 'real' and (not authorize_real_requests or type(budget) is not PreparationBudget
                           or budget.request_limit > 6):
        raise ValueError('explicit_six_request_pilot_budget_required')
    # Resolve configured credentials only after explicit execution gates.
    client = (configured_openai_client(enabled=True, service_tier=service_tier) if mode == 'real'
              else OfflineClient(service_tier=service_tier))
    if client.service_tier != service_tier:
        raise ValueError('pilot_explicit_standard_tier_required')
    budget = budget or PreparationBudget(6, 150000)
    if budget.request_limit > 6:
        raise ValueError('explicit_six_request_pilot_budget_required')
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    def save(path, value):
        with path.open('x', encoding='utf-8') as output:
            output.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    save(root/'quality-hypotheses-NOT-MODEL-INPUT.json', QUALITY_HYPOTHESES)
    planned = [dict(case=name, planned_requests=0 if name == 'P01' else 1) for name in QUALITY_HYPOTHESES]
    save(root/'pilot-plan.json', dict(cases=planned, budget=asdict(budget), maximum_physical_requests=6,
                                    requested_service_tier=service_tier))
    active = {'case': None}
    def audit(event):
        folder = root/active['case']/f"attempt-{event['attempt']:02d}-{event['request_id']}"
        kind = event['event']
        if kind == 'request':
            # Exclusive directory ownership is established before dispatch.
            # An explicit later retry must use a new attempt/run identity.
            folder.mkdir(exist_ok=False)
        if kind == 'dispatch':
            for name in ('response.raw.json', 'validated.json', 'failed.json'):
                if (folder/name).exists():
                    raise FileExistsError('pilot_attempt_destination_exists')
        if kind == 'response':
            with (folder/'response.raw.json').open('xb') as output:
                output.write(event['raw_response'])
        else:
            save(folder/(kind + '.json'), event)
    fixture = PreparationFixture(client=client, budget=budget, audit_sink=audit,
                                 real=mode == 'real', enabled=mode != 'dry-run')
    outcomes, ledger = [], []
    halted = None
    try:
        frozen = fixture.frozen_source()
        body = frozen['body']
        for name in QUALITY_HYPOTHESES:
            active['case'] = name
            folder = root/name
            folder.mkdir()
            fixture.f.profile = profile('Customer support specialist', 6)
            source_body = body
            relation = 'supported_partial'  # OFFLINE LABEL ONLY; never sent to a real client
            if name == 'P01':
                fixture.f.profile = profile()
            elif name == 'C01_unrelated':
                fixture.f.profile = profile('Biology researcher', 6)
                relation = 'not_established'
            elif name in ('C02_shortfall', 'C03_exact_boundary'):
                fixture.f.profile = cases.duration_profile(2 if name == 'C02_shortfall' else 5,
                                                           role='Campaign adviser')
                source_body = body.replace('Customer success / support operations', 'marketing')
            elif name == 'C04_alternative':
                cases.ProfessionalAlternativeTests.candidate(fixture, degree='marketing')
                fixture.f.profile['experience']['recent_roles'] = ['Campaign adviser']
                source_body = body.replace('Customer success / support operations', 'marketing').replace(
                    '2. **Native', 'Alternatively, a degree in marketing is sufficient.\n\n2. **Native')
            elif name == 'C05_proficiency':
                fixture.f.profile['languages'][0]['proficiency'] = 'basic'
                source_body += '\n\n## Requirements\n\nNative English required.'
            fixture.revision = 'synthetic-pilot-' + name
            fixture.source(source_body, body_format=frozen['body_format'], metadata=frozen['metadata'])
            if mode != 'real':
                client.session.relation = relation
            plan = fixture.prepare()
            save(folder/'dry-run.json', plan)
            before = fixture.preparer.accounting
            if halted:
                result = dict(plan, items=[dict(item, state='skipped', reason=halted) for item in plan['items']])
            else:
                result = fixture.execute(plan) if mode != 'dry-run' else plan
            save(folder/'preparation.json', result)
            # Inspection is non-dispatching on success, conservative output,
            # failure and exhaustion. Never retry a case to demonstrate reuse.
            reused = fixture.prepare()
            save(folder/'subsequent-inspection.json', reused)
            accounting = fixture.preparer.accounting
            states = [item['state'] for item in result['items']]
            disposition = ('unexecuted_service_tier' if halted else
                           'dry_run' if mode == 'dry-run' else
                           'failed' if 'failed' in states else
                           'published' if 'published' in states else
                           'reused' if 'reusable' in states else
                           'unexecuted_budget' if any(item.get('reason') == 'preparation_budget_exhausted'
                                                     for item in result['items']) else 'selection_skip')
            entry = dict(case=name, planned_requests=0 if name == 'P01' else 1, disposition=disposition,
                         physical_attempts=accounting['physical_attempts'] - before['physical_attempts'],
                         reserved_attempts=accounting['attempts'] - before['attempts'],
                         records=accounting['records'][len(before['records']):],
                         reuse_check=[item['state'] for item in reused['items']],
                         model_quality_observation=name != 'P01' and disposition == 'published')
            ledger.append(entry)
            save(folder/'ledger.json', entry)
            if any(r['physical_attempts'] and (type(r['returned_service_tier']) is not str
                       or r['returned_service_tier'] != service_tier) for r in entry['records']):
                halted = 'pilot_response_service_tier_unverified'
            run, match = fixture.current()
            outcome = dict(case=name, mode=mode, offline_stub=mode != 'real',
                source_body_sha256=hashlib.sha256(source_body.encode()).hexdigest(),
                original_frozen_source=frozen['origin'],
                synthetic_profile=fixture.f.profile, preparation_states=[i['state'] for i in result['items']],
                subsequent_lookup_states=[i['state'] for i in reused['items']],
                comparisons=match.get('source_qualification_comparisons', []),
                score=match['score'], section=match['preview_section'],
                conditional_task_fit=match.get('conditional_task_fit'),
                primary_eligible=match.get('primary_recommendation_eligible'),
                accounting=fixture.preparer.accounting)
            save(folder/'outcome.json', outcome)
            outcomes.append(outcome)
        save(root/'pilot-outcomes.json', outcomes)
        save(root/'pilot-ledger.json', dict(planned_cases=planned, cases=ledger,
                                           halted_reason=halted, requested_service_tier=service_tier,
                                           accounting=fixture.preparer.accounting))
        save(root/'pilot-manifest.json', dict(mode=mode, model=client.model, budget=asdict(budget),
             requested_service_tier=service_tier, halted_reason=halted,
             automatic_recognition_quality_evaluated=False,
             authority='synthetic authenticated-state substitute; production composition and disposable accepted storage',
             source_variants='P01/P02/C01 use frozen wording; other controls explicitly edit synthetic requirement wording',
             accounting=fixture.preparer.accounting,
             files={p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(root.rglob('*')) if p.is_file()}))
        return outcomes
    finally:
        fixture.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--mode', choices=('dry-run', 'offline', 'real'), default='dry-run')
    parser.add_argument('--authorize-real-requests', action='store_true')
    parser.add_argument('--service-tier', choices=('default',), default='default')
    parser.add_argument('--request-limit', type=int, default=6)
    parser.add_argument('--token-limit', type=int, default=150000)
    parser.add_argument('--usd-limit', default='0')
    parser.add_argument('--input-usd-per-million', default='0')
    parser.add_argument('--output-usd-per-million', default='0')
    args = parser.parse_args(argv)
    budget = PreparationBudget(args.request_limit, args.token_limit, args.usd_limit,
                               args.input_usd_per_million, args.output_usd_per_million)
    outcomes = run_pilot(args.output, mode=args.mode, budget=budget,
                         service_tier=args.service_tier,
                         authorize_real_requests=args.authorize_real_requests)
    print(json.dumps(dict(mode=args.mode, cases=len(outcomes), output=str(Path(args.output).resolve()),
                          attempts=outcomes[-1]['accounting']['attempts'])))


if __name__ == '__main__':
    main()
