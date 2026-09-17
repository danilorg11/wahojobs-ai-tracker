"""Offline account requests using existing Accounts lifecycle; no email or purge."""
from contextlib import contextmanager, closing
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
import argparse
import json
import os
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from wahojobs import accounts
from wahojobs.beta_recovery import _new_directory, _write, _json, _check_sqlite
from wahojobs.crawler.local_inventory import local_database_path
from wahojobs.database_lifetime_ownership import (
    acquire_database_lifetime_ownership, release_database_lifetime_ownership, ROLE_OFFLINE_OPERATOR)
from wahojobs.workos_authkit_staging import validate_workos_authkit_staging_database


@contextmanager
def account_storage(database, user_id):
    if type(user_id) is not str or not re.fullmatch('usr_[0-9a-f]{32}',user_id):
        raise ValueError('account_selection_invalid')
    path=local_database_path(database)
    lease=acquire_database_lifetime_ownership(path,role=ROLE_OFFLINE_OPERATOR)
    try:
        _check_sqlite(path,product=True)
        with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True)) as connection:
            connection.row_factory=sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            validate_workos_authkit_staging_database(connection)
            if connection.execute('SELECT 1 FROM users WHERE user_id=?',(user_id,)).fetchone() is None:
                raise ValueError('account_not_found')
            yield connection
    finally:
        release_database_lifetime_ownership(lease,role=ROLE_OFFLINE_OPERATOR,database_path=path)


def status(database,user_id):
    with account_storage(database,user_id) as connection:
        row=connection.execute('SELECT user_id,lifecycle_status,row_version FROM users WHERE user_id=?',(user_id,)).fetchone()
        return dict(row)


def suspend(database,user_id,*,expected_version,request_id,now=None):
    with account_storage(database,user_id) as connection:
        result=accounts.suspend_user(connection,user_id=user_id,expected_version=expected_version,
            source='private_beta_operator',idempotency_key=request_id,now=now)
        return dict(status=result.user.lifecycle_status,version=result.user.row_version,
                    data_erased=False,sessions_revoked=True)


def request_closure(database,user_id,*,expected_version,request_id,cooling_days,purge_days,now=None):
    # The operator supplies an approved policy. There are deliberately no defaults.
    if any(type(n) is not int or not 1<=n<=3650 for n in (cooling_days,purge_days)):
        raise ValueError('account_policy_required')
    with account_storage(database,user_id) as connection:
        result=accounts.request_account_deletion(connection,user_id=user_id,expected_version=expected_version,
            cooling_period=timedelta(days=cooling_days),purge_after=timedelta(days=purge_days),
            request_source='private_beta_operator',idempotency_key=request_id,now=now)
        return dict(status='deletion_requested',data_erased=False,
            next_action='Existing lifecycle deactivation and separately reviewed purge/backup policy; no purge job exists.')


def export_account(database,user_id,destination):
    """Private account-native beta export, excluding credentials and other owners.

    Main and declared manual draft data only; no scan of backups/logs or unrelated
    accounts. No data leaves the filesystem. Operator verifies recipient identity.
    """
    with account_storage(database,user_id) as connection:
        connection.execute('BEGIN IMMEDIATE')
        def rows(sql,args=()): return [dict(row) for row in connection.execute(sql,args)]
        data={}
        for table in ('users','auth_identities','account_lifecycle_events','consent_events','account_deletion_requests'):
            data[table]=rows('SELECT * FROM '+table+' WHERE user_id=?',(user_id,))
        data['invitations']=rows('SELECT invitation_id,email_display_hint,created_at,expires_at,consumed_at,invitation_status '
            'FROM account_invitations WHERE consumed_by_user_id=?',(user_id,))
        data['session_summary']=rows('SELECT session_id,created_at,last_seen_at,idle_expires_at,absolute_expires_at,revoked_at '
            'FROM account_sessions WHERE user_id=?',(user_id,))
        bindings=rows('SELECT * FROM principal_account_bindings WHERE user_id=?',(user_id,))
        data['bindings']=bindings
        for binding in bindings:
            # Initial beta supports account-native, exclusive ownership only.
            principal=binding['principal_id']; env=binding['environment_namespace']
            owner=rows('SELECT * FROM product_principals WHERE principal_id=? AND environment_namespace=?',(principal,env))
            if (len(owner)!=1 or owner[0]['principal_type']!='account_native' or env!='private_beta'
                    or connection.execute('SELECT 1 FROM principal_account_bindings WHERE principal_id=? AND user_id<>?',
                                          (principal,user_id)).fetchone()):
                raise ValueError('account_export_ownership_review_required')
            profiles=rows('SELECT * FROM product_profiles WHERE principal_id=? AND environment_namespace=?',(principal,env))
            data.setdefault('profiles',[]).extend(profiles)
            for profile in profiles:
                pid=profile['profile_id']
                for table in ('product_profile_revisions','product_profile_sources','user_pipeline_items',
                              'user_pipeline_transitions','applicant_status_updates'):
                    data.setdefault(table,[]).extend(rows('SELECT * FROM '+table+' WHERE profile_id=?',(pid,)))
                data.setdefault('workflow_state',[]).extend(rows('SELECT s.* FROM user_pipeline_state s JOIN user_pipeline_items i '
                    'ON i.pipeline_item_id=s.pipeline_item_id WHERE i.profile_id=?',(pid,)))
            from wahojobs.profile_correction_drafts import store_connection
            with store_connection(connection) as drafts:
                if drafts is not None:
                    drafts.row_factory=sqlite3.Row
                    drafts.execute('PRAGMA query_only=ON')
                    tables={r[0] for r in drafts.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    manual_owner=sha256(json.dumps((user_id,env,principal)).encode()).hexdigest()
                    if 'manual_profile_drafts' in tables:
                        data.setdefault('manual_drafts',[]).extend(dict(r) for r in drafts.execute(
                            'SELECT * FROM manual_profile_drafts WHERE owner_binding=?',(manual_owner,)))
                    if 'product_profile_correction_drafts' in tables:
                        from wahojobs.persistent_profile_corrections import _canonical_json_bytes, PROFILE_CORRECTION_PURPOSE
                        for profile in profiles:
                            owner_hash=sha256(_canonical_json_bytes([user_id,env,principal,profile['profile_id'],PROFILE_CORRECTION_PURPOSE])).hexdigest()
                            data.setdefault('correction_drafts',[]).extend(dict(r) for r in drafts.execute(
                                'SELECT * FROM product_profile_correction_drafts WHERE owner_binding=?',(owner_hash,)))
        # Session credentials/hashes and operator idempotency material are not a
        # candidate data export. Profile/source digests retain binding provenance.
        for table,records in data.items():
            for row in records:
                for key in tuple(row):
                    if key in {'link_idempotency_key','idempotency_key','request_fingerprint'}:
                        row.pop(key)
        payload=dict(version='private_beta_account_export_v1',account_id=user_id,data=data,
            limits=['No provider tokens or session/CSRF credentials.',
                    'Backups and sanitized logs require separate policy handling.',
                    'Document extraction and paid companion preparation are disabled in this fresh beta.'])
        raw=_json(payload)
        if len(raw)>32_000_000: raise ValueError('account_export_size_review_required')
        target=_new_directory(destination)
        _write(target/'account-data.json',raw)
        connection.rollback()
        return dict(status='private_export_created',sha256=sha256(raw).hexdigest())


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('status','export','suspend','request-closure'))
    parser.add_argument('--database',required=True)
    parser.add_argument('--user-id',required=True)
    parser.add_argument('--new-directory')
    parser.add_argument('--expected-version',type=int)
    parser.add_argument('--request-id')
    parser.add_argument('--cooling-days',type=int)
    parser.add_argument('--purge-days',type=int)
    args=parser.parse_args(argv)
    try:
        if args.operation=='status':
            result=status(args.database,args.user_id)
        elif args.operation=='export':
            result=export_account(args.database,args.user_id,args.new_directory)
        elif args.operation=='suspend':
            result=suspend(args.database,args.user_id,expected_version=args.expected_version,request_id=args.request_id)
        else:
            result=request_closure(args.database,args.user_id,expected_version=args.expected_version,
                request_id=args.request_id,cooling_days=args.cooling_days,purge_days=args.purge_days)
        print(json.dumps(result,sort_keys=True))
        return 0
    except Exception:
        print('account_operation_failed; no private exception details logged',file=sys.stderr)
        return 2


if __name__=='__main__': raise SystemExit(main())
