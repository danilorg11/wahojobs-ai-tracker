"""Explicit cold backup and non-overwriting local beta recovery. No network."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from wahojobs.beta_recovery import create_snapshot, verify_snapshot, restore_snapshot
from wahojobs.database_lifetime_ownership import DatabaseLifetimeOwnershipError
from wahojobs.workos_authkit_staging import WorkOSAuthKitStagingError


_SAFE_FAILURE_CODES = frozenset({
    'recovery_absolute_canonical_path_required', 'recovery_unsafe_path',
    'recovery_ordinary_single_link_file_required', 'recovery_new_absolute_directory_required',
    'recovery_storage_must_be_outside_git', 'recovery_sqlite_sidecars_present',
    'recovery_rollback_journal_required', 'recovery_integrity_failed',
    'recovery_foreign_keys_failed', 'recovery_distinct_companion_required',
    'recovery_journal_unavailable', 'recovery_unsafe_journal',
    'recovery_snapshot_file_limit', 'recovery_nonsecret_release_labels_required',
    'recovery_source_changed', 'recovery_manifest_limit', 'recovery_manifest_invalid',
    'recovery_snapshot_integrity_failed', 'recovery_restore_integrity_failed',
    'maintenance_journal_database_identity_changed', 'maintenance_journal_integrity_failed',
})


def failure_category(error):
    if isinstance(error, DatabaseLifetimeOwnershipError):
        return 'recovery_database_owned_or_unavailable'
    if isinstance(error, WorkOSAuthKitStagingError):
        return 'recovery_supported_database_attestation_failed'
    if (type(error) is ValueError and len(error.args) == 1
            and type(error.args[0]) is str and error.args[0] in _SAFE_FAILURE_CODES):
        return error.args[0]
    if isinstance(error, OSError):
        return 'recovery_storage_unavailable'
    return 'recovery_operation_unavailable'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    backup = commands.add_parser('backup', help='Stopped writers only; private new destination outside Git.')
    backup.add_argument('--database', required=True)
    backup.add_argument('--companion')
    backup.add_argument('--destination', required=True)
    backup.add_argument('--code-commit', required=True)
    backup.add_argument('--configuration-revision', required=True, help='Nonsecret vault/config revision label only.')
    verify = commands.add_parser('verify', help='Verify snapshot hashes and supported database integrity.')
    verify.add_argument('--snapshot', required=True)
    restore = commands.add_parser('restore', help='Restore to a NEW directory. Never replace current storage or rebind maintenance.')
    restore.add_argument('--snapshot', required=True)
    restore.add_argument('--destination', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'backup':
            result = create_snapshot(args.database, args.destination, companion=args.companion,
                code_commit=args.code_commit, configuration_revision=args.configuration_revision)
            count = len(result['files'])
        elif args.command == 'verify':
            count = len(verify_snapshot(args.snapshot)['files'])
        else:
            count = restore_snapshot(args.snapshot, args.destination)['file_count']
        print(f'{args.command}: verified {count} files; no network or current-storage replacement.')
        if args.command == 'restore':
            print('Review RECOVERY-READY.json. Recovered maintenance remains held by original immutable identity; no activation performed.')
        return 0
    except Exception as error:
        print('Recovery operation failed: ' + failure_category(error)
              + '. Retain partial output; resolve this prerequisite before choosing a new destination.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
