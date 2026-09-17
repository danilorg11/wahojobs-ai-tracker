"""Explicit fresh beta setup through accepted migrations; no historical imports."""
from contextlib import closing
from pathlib import Path
import argparse
import json
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def initialize_fresh(destination):
    from wahojobs.beta_recovery import _new_directory, _write, _json
    from wahojobs.db.repository import initialize_database
    from wahojobs.database_lifetime_ownership import (
        acquire_database_lifetime_ownership, release_database_lifetime_ownership, ROLE_OFFLINE_OPERATOR)
    from scripts.pipeline_state_migration import apply_pipeline_state_migration
    from scripts.accounts_migration import apply_accounts_migration
    from scripts.ownership_migration import apply_ownership_migration
    from scripts.persistent_profiles_migration import apply_persistent_profiles_migration
    from scripts.persistent_profile_canonical_v2_migration import apply_persistent_profile_canonical_v2_migration
    from scripts.google_oidc_authorization_transactions_migration import (
        apply_google_oidc_authorization_transactions_migration, database_file_identity)
    from scripts.closed_schema_convergence_migration import apply_closed_schema_convergence_migration
    from scripts.workos_authkit_provider_migration import apply_workos_authkit_provider_migration
    from scripts.public_job_identity_migration import apply_public_job_identity_migration
    from scripts.ai_profile_import_migration import apply_ai_profile_import_migration
    from scripts.resumable_ai_profile_intake_migration import apply_resumable_ai_profile_intake_migration
    from wahojobs.workos_authkit_staging import validate_workos_authkit_staging_database
    target = _new_directory(destination)
    database = target / 'product.sqlite3'
    initialize_database(database)
    lease = acquire_database_lifetime_ownership(database, role=ROLE_OFFLINE_OPERATOR)
    try:
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            for migration in (apply_pipeline_state_migration, apply_accounts_migration,
                    apply_ownership_migration, apply_persistent_profiles_migration,
                    apply_persistent_profile_canonical_v2_migration):
                migration(connection)
            apply_google_oidc_authorization_transactions_migration(connection,
                requested_path=database, expected_identity=database_file_identity(database))
            connection.row_factory = None
            apply_closed_schema_convergence_migration(connection, requested_path=database,
                expected_identity=database_file_identity(database), ownership=lease)
            for migration in (apply_workos_authkit_provider_migration, apply_public_job_identity_migration,
                    apply_ai_profile_import_migration, apply_resumable_ai_profile_intake_migration):
                migration(connection)
            validate_workos_authkit_staging_database(connection)
            counts = {table: connection.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                for table in ('jobs', 'users', 'account_sessions', 'account_invitations', 'auth_identities')}
            if any(counts.values()):
                raise ValueError('fresh_storage_not_empty')
        receipt = dict(version='private_beta_fresh_storage_v1', schema='M011',
            accounts_and_inventory=counts, source_requests=0, model_requests=0,
            companion='not_configured', historical_ledger='absent', synthetic_rows=0)
        _write(target / 'FRESH-READY.json', _json(receipt))
        return receipt
    finally:
        release_database_lifetime_ownership(lease, role=ROLE_OFFLINE_OPERATOR, database_path=database)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--new-directory', required=True)
    args = parser.parse_args(argv)
    try:
        initialize_fresh(args.new_directory)
        print('fresh_storage_ready:M011; zero accounts, jobs and historical executions')
        return 0
    except Exception:
        print('fresh_storage_failed; retain partial output and use a new destination', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
