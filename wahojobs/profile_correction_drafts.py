"""Private, unconfirmed correction checkpoints, independent of review tokens.

The correction service supplies authenticated owner and revision bindings.
This small account-side table contains no confirmation authority, and never
changes a canonical profile or an import entitlement. Rows are immutable so
older proposals remain inspectable after a conflict or a subsequent edit.
"""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


TABLE = 'product_profile_correction_drafts'
MAX_PAYLOAD_BYTES = 524_288


@contextmanager
def store_connection(account_connection, *, write=False):
    """Keep draft persistence outside the closed canonical-account schema.

    Derive the private sidecar from the already configured account database,
    never from a browser path. Its lifetime is independent of server tokens.
    """
    main = next((row[2] for row in account_connection.execute('PRAGMA database_list') if row[1] == 'main'), '')
    if not main:
        raise ValueError('correction_checkpoint_unavailable')
    account_path = Path(main)
    path = account_path.with_name(account_path.name + '.correction-drafts.sqlite3')
    if path.is_symlink():
        raise ValueError('correction_checkpoint_unavailable')
    if not write and not path.exists():
        yield None
        return
    connection = sqlite3.connect(path.as_uri() + ('?mode=rwc' if write else '?mode=ro'), uri=True, timeout=3)
    try:
        yield connection
    finally:
        connection.close()


def encode(payload):
    value = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
    if len(value.encode('ascii')) > MAX_PAYLOAD_BYTES:
        raise ValueError('correction_checkpoint_unavailable')
    return value


def save(connection, *, reference, owner, base_revision, base_hash, payload, created_at, predecessor=None):
    value = encode(payload)
    # Installed on the first authenticated draft save, in private sidecar
    # storage. No migration or write is performed by a read/resume GET.
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        if predecessor and is_discarded(connection, owner=owner, reference=predecessor):
            raise ValueError('draft_discarded')
        connection.execute('CREATE TABLE IF NOT EXISTS ' + TABLE + ''' (
            draft_reference TEXT PRIMARY KEY NOT NULL,
            owner_binding TEXT NOT NULL,
            base_revision_id TEXT NOT NULL,
            base_profile_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL
        )''')
        connection.execute('INSERT INTO ' + TABLE + ' VALUES (?,?,?,?,?,?,?)', (
            reference, owner, base_revision, base_hash, value,
            hashlib.sha256(value.encode('ascii')).hexdigest(), created_at))


def load(connection, *, owner, reference=None):
    if connection is None:
        return None
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return None
    sql = 'SELECT draft_reference,base_revision_id,base_profile_sha256,payload_json,payload_sha256 FROM ' + TABLE + ' WHERE owner_binding=?'
    args = [owner]
    if reference is not None:
        sql += ' AND draft_reference=?'
        args.append(reference)
    row = connection.execute(sql + ' ORDER BY rowid DESC LIMIT 1', args).fetchone()
    if row is None:
        return None
    if is_discarded(connection, owner=owner, reference=row[0]):
        return None
    value = row[3]
    if (type(value) is not str or len(value.encode('utf-8')) > MAX_PAYLOAD_BYTES
            or hashlib.sha256(value.encode('utf-8')).hexdigest() != row[4]):
        raise ValueError('correction_checkpoint_unavailable')
    payload = json.loads(value)
    if encode(payload) != value:
        raise ValueError('correction_checkpoint_unavailable')
    return row[0], row[1], row[2], payload


def is_discarded(connection, *, owner, reference):
    if connection is None or not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='discarded_profile_drafts'").fetchone():
        return False
    return connection.execute('SELECT 1 FROM discarded_profile_drafts WHERE owner_binding=? AND draft_reference=?',
                              (owner, reference)).fetchone() is not None


def discard(connection, *, owner, reference):
    """Retain immutable proposals, invalidate their authority, preserve newer work."""
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        latest = connection.execute('SELECT rowid,draft_reference FROM ' + TABLE +
            ' WHERE owner_binding=? ORDER BY rowid DESC LIMIT 1', (owner,)).fetchone()
        if latest is None or latest[1] != reference:
            raise ValueError('draft_changed')
        connection.execute('CREATE TABLE IF NOT EXISTS discarded_profile_drafts ('
            'draft_reference TEXT PRIMARY KEY, owner_binding TEXT NOT NULL)')
        connection.execute('INSERT OR IGNORE INTO discarded_profile_drafts '
            'SELECT draft_reference,owner_binding FROM ' + TABLE + ' WHERE owner_binding=? AND rowid<=?',
            (owner, latest[0]))


@contextmanager
def confirmation_guard(account, *, owner, reference):
    if reference is None:
        yield True
        return
    with store_connection(account, write=True) as connection, connection:
        connection.execute('BEGIN IMMEDIATE')
        yield not is_discarded(connection, owner=owner, reference=reference)
