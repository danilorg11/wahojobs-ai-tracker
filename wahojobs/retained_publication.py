"""Explicit bounded recovery of sealed observations, never ordinary navigation.

The default handoff window is unchanged. A reviewed offline operator may bind
exact retained collections to an unchanged source/lifecycle contract for up to
24 hours after collection. Source verification dates remain the original dates.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json

_ACTIVE = ContextVar('retained_publication_recovery', default=None)
MAX_OBSERVATION_AGE_SECONDS = 86400
MAX_AUTHORIZATION_SECONDS = 7200
# These are the reviewed operational changes in this release. The catalog
# presentation/preparation changes below were already accepted for cd813368.
# All remaining collection, capture, tracking and closure files must match.
_OPERATIONAL_FILES = frozenset(('operational_budgets.py', 'daily_inventory.py',
    'daily_source_policy.py', 'retained_publication.py', 'crawler/staged_observation.py'))
_ACCEPTED_CATALOG_FILES = frozenset(('authenticated_profile_matches.py', 'candidate_source_display.py',
    'catalog_display.py', 'catalog_source_geography.py', 'catalog_source_links.py',
    'catalog_source_presentation.py', 'crawler/provider_details.py', 'opportunity_enrichment.py',
    'persistent_profiles_browser.py', 'public_catalog_brand.py', 'public_catalog_configuration.py',
    'public_catalog_reader.py', 'public_job_page.py', 'public_jobs_catalog.py', 'remote_beta.py',
    'workos_authkit_staging.py'))


def source_contract(root):
    root = Path(root) / 'wahojobs'
    records = {}
    for path in sorted(root.rglob('*')):
        name = path.relative_to(root).as_posix()
        if (path.is_file() and path.suffix in ('.py', '.sql', '.json')
                and name not in _OPERATIONAL_FILES | _ACCEPTED_CATALOG_FILES):
            records[name] = sha256(path.read_bytes()).hexdigest()
    return sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()


def _aware(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None: raise ValueError('aware_recovery_time_required')
    return result


@contextmanager
def retained_publication_scope(authorization, *, database, ownership):
    """Bind one explicit reviewed grant to real offline ownership and code."""
    from wahojobs.database_lifetime_ownership import require_database_lifetime_ownership, ROLE_OFFLINE_OPERATOR
    from wahojobs.evidence_maintenance import database_identity, contract_fingerprint
    database = Path(database)
    require_database_lifetime_ownership(ownership, role=ROLE_OFFLINE_OPERATOR, database_path=database)
    now = datetime.now(timezone.utc)
    issued, expires = _aware(authorization['issued_at']), _aware(authorization['expires_at'])
    current = Path(__file__).resolve().parents[1]
    origin = Path(authorization['origin_release'])
    expected = authorization['source_contract_sha256']
    if (authorization.get('authorized') is not True or not authorization.get('reviewed_run_id')
            or not issued <= now < expires or (expires-issued).total_seconds() > MAX_AUTHORIZATION_SECONDS
            or authorization['database'] != database_identity(database)
            or authorization['current_contract_fingerprint'] != contract_fingerprint()
            or origin.name != authorization['origin_commit']
            or source_contract(origin) != expected or source_contract(current) != expected
            or not 1 <= len(authorization['collections']) <= 14 or _ACTIVE.get() is not None):
        raise ValueError('bound_reviewed_retained_publication_required')
    token = _ACTIVE.set((authorization, database, ownership))
    try: yield
    finally: _ACTIVE.reset(token)


def compatible_collection(report):
    active = _ACTIVE.get()
    if active is None: return False
    authorization, _, _ = active
    expected = authorization['collections'].get(report['plan_id'])
    plan = report['plan']
    return bool(expected and plan['code_commit'] == authorization['origin_commit']
        and plan['contract_fingerprint'] == expected['contract_fingerprint']
        and plan['source'] == expected['source'] and plan['run_id'] == expected['run_id']
        and report['events'][-1]['hash'] == expected['journal_hash'])


def recovery_age_limit(observation):
    active = _ACTIVE.get()
    if active is None: return None
    authorization, database, ownership = active
    from wahojobs.database_lifetime_ownership import require_database_lifetime_ownership, ROLE_OFFLINE_OPERATOR
    from wahojobs.evidence_maintenance import contract_fingerprint, read_connection
    require_database_lifetime_ownership(ownership, role=ROLE_OFFLINE_OPERATOR, database_path=database)
    expected = authorization['collections'].get(observation.collection_plan_id)
    now = datetime.now(timezone.utc)
    if (now >= _aware(authorization['expires_at'])
            or contract_fingerprint() != authorization['current_contract_fingerprint']
            or not expected or expected['source'] != observation.source
            or expected['journal_hash'] != observation.journal_hash
            or expected['started_at'] != observation.started_at
            or expected['completed_at'] != observation.completed_at):
        raise ValueError('exact_retained_observation_required')
    # Reject a duplicate committed observation or superseded source publication.
    # Failed attempts also require review rather than silent replay.
    with read_connection(database) as connection:
        latest = connection.execute('SELECT cr.started_at FROM crawl_runs cr JOIN companies c '
            'ON c.id=cr.company_id WHERE c.slug=? ORDER BY cr.started_at DESC,cr.id DESC LIMIT 1',
            (observation.source,)).fetchone()
    if latest and _aware(latest[0]) >= _aware(observation.started_at):
        raise ValueError('retained_observation_already_attempted_or_superseded')
    return MAX_OBSERVATION_AGE_SECONDS
