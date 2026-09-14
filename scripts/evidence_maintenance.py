"""Inspect, plan, explicitly execute, and recover Alignerr/Mercor maintenance."""
import argparse
import json
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wahojobs import evidence_maintenance as maintenance


def owner_scope(database, session_file, budget_file=None, *, enable=False, offline=False):
    """Reuse the normal read-only session/CSRF/profile authority; no login."""
    from wahojobs.authenticated_profile_matches import AuthenticatedProfileMatchesService
    from wahojobs.browser_session_authentication import DurableBrowserSessionAuthenticationGateway
    from wahojobs.persistent_profile_read_authorization import DurablePersistentProfileReadAuthorizationGateway
    from wahojobs.professional_background_preparation import RECIPE, PreparationBudget, ProfessionalBackgroundPreparer
    from wahojobs.professional_background_semantics import ProfessionalBackgroundEvidence
    from wahojobs.professional_background_store import SQLiteProfessionalBackgroundStore
    from wahojobs.opportunity_llm import configured_openai_client, DEFAULT_MODEL
    document = json.loads(Path(session_file).read_text(encoding='utf-8'))
    budget = PreparationBudget(**json.loads(Path(budget_file).read_text(encoding='utf-8'))) if budget_file else None
    provider = lambda: maintenance.read_connection(database)
    now = maintenance.clock_now
    service = AuthenticatedProfileMatchesService(
        authentication_gateway=DurableBrowserSessionAuthenticationGateway(
            trusted_environment_namespace=document['environment'], clock=now),
        authorization_gateway=DurablePersistentProfileReadAuthorizationGateway(),
        connection_provider=provider, clock=now, binding_secret=secrets.token_bytes(32))
    if offline:
        from tests.professional_background_preparation_support import OfflineClient
        client = OfflineClient()
    else:
        client = configured_openai_client(enabled=True) if enable else None
    model = client.model if client is not None else DEFAULT_MODEL
    evidence = ProfessionalBackgroundEvidence(recipe=RECIPE, model=model,
        basis='offline_labelled_stub' if offline else 'semantic_model_output',
        durable_store=SQLiteProfessionalBackgroundStore(document['companion']))
    preparer = ProfessionalBackgroundPreparer(evidence, client=client, budget=budget,
        enabled=enable, allow_real_requests=enable and not offline)
    return maintenance.OwnerPreparation(service, provider, preparer,
        owner=(document['account_id'], document['environment'], document['principal_id']),
        profile_id=document['profile_id'], job_ids=document['job_ids'],
        credentials=dict(authentication_input=(('Cookie', 'wahojobs_session=' + document['session_token']),),
                         session_token=document['session_token'], csrf_secret=document['csrf_secret']))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command')
    for name in ('inspect', 'plan', 'execute'):
        p = sub.add_parser(name)
        if name != 'execute':
            p.add_argument('--db', type=Path, required=True)
            p.add_argument('--providers', nargs='+', choices=maintenance.PROVIDERS, required=True)
            p.add_argument('--http-limit', type=int)
            p.add_argument('--detail-limit', type=int, default=0)
            p.add_argument('--details', choices=('needed','all','none'), default='needed')
            p.add_argument('--phase', choices=('all','source','derived'), default='all')
            if name == 'plan':
                p.add_argument('--out', type=Path, required=True)
        else:
            p.add_argument('--plan', type=Path, required=True)
            p.add_argument('--journal', type=Path, required=True)
            p.add_argument('--yes', action='store_true')
            p.add_argument('--allow-provider-requests', action='store_true')
            p.add_argument('--allow-derived-writes', action='store_true')
            p.add_argument('--allow-preparation', action='store_true')
            p.add_argument('--allow-enrichment-model', action='store_true')
        p.add_argument('--owner-session', type=Path, help='Private current owner/session/CSRF selection; never logs in')
        p.add_argument('--preparation-budget', type=Path)
        p.add_argument('--enable-preparation', action='store_true', help='Explicit trusted preparer configuration; dry-run still makes no calls')
        p.add_argument('--enrichment-canonical-id', type=int, help='One explicit canonical source-bound model preparation/repair')
        p.add_argument('--enrichment-budget', type=Path, help='Existing PreparationBudget JSON; verified rates required for real execution')
        p.add_argument('--enrichment-journal', type=Path, help='Stable journal for immutable enrichment reservations')
    for name in ('report', 'recover'):
        p = sub.add_parser(name)
        p.add_argument('--journal', type=Path, required=True)
        p.add_argument('--plan-id', required=True)
    p = sub.add_parser('demo', help='Labelled offline disposable operator demonstration')
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--step', choices=('init','inspect','plan','execute','report','next','run'), default='inspect')
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == 'demo':
        from tests.evidence_maintenance_support import demo_command
        result = demo_command(args.directory, args.step)
    elif args.command in ('report','recover'):
        result = maintenance.report(args.journal, args.plan_id)
    else:
        plan = json.loads(args.plan.read_text(encoding='utf-8')) if args.command == 'execute' else None
        database = Path(plan['database']['path']) if plan else args.db
        owner = owner_scope(database, args.owner_session, args.preparation_budget,
                            enable=args.enable_preparation) if args.owner_session else None
        enrichment = None
        if args.enrichment_canonical_id is not None:
            if args.enrichment_budget is None or args.enrichment_journal is None:
                parser.error('Enrichment configuration requires --enrichment-budget and --enrichment-journal')
            from wahojobs.opportunity_llm import configured_openai_client
            from wahojobs.professional_background_preparation import PreparationBudget
            enrichment = maintenance.EnrichmentRepair(args.enrichment_canonical_id, configured_openai_client(enabled=True),
                PreparationBudget(**json.loads(args.enrichment_budget.read_text(encoding='utf-8'))), args.enrichment_journal)
        if args.command == 'execute':
            result = maintenance.execute_plan(plan, args.journal, authorized=args.yes,
                authorize_sources=args.allow_provider_requests, authorize_derived=args.allow_derived_writes,
                authorize_preparation=args.allow_preparation, owner=owner, enrichment=enrichment,
                authorize_enrichment=args.allow_enrichment_model)
        else:
            result = maintenance.build_plan(args.db, args.providers, http_limit=args.http_limit,
                detail_limit=args.detail_limit, details=None if args.details == 'none' else args.details,
                phase=args.phase, owner=owner, enrichment=enrichment)
            if args.command == 'plan':
                maintenance.save_json(args.out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
