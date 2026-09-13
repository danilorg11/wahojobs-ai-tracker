"""Explicit local SQLite companion for private derived comparisons.

No product schema changes, startup migrations, source/profile authority, model
dispatch, or response cache. Each operation owns its short-lived connection.
"""
from contextlib import contextmanager, closing
import json
import os
from pathlib import Path
import sqlite3
import uuid
from functools import lru_cache

from wahojobs.professional_background_semantics import digest
from wahojobs.workos_authkit_schema import iter_sql_statements


SCHEMA_PATH = Path(__file__).parent / 'db' / 'professional_background.sql'
APPLICATION_ID = 1463963714
SCHEMA_VERSION = 1


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _schema(connection):
    return [tuple(row) for row in connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                              "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]


@lru_cache(maxsize=1)
def _expected_schema():
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding='utf-8'))
        return _schema(connection)


def initialize_preparation_store(connection):
    """Explicit transactional setup of an empty, disposable/approved database.

    Takes an already open connection, never a default path. Rejects product
    databases, partial installations, and open transactions. No startup caller.
    """
    try:
        if (type(connection) is not sqlite3.Connection or connection.in_transaction
                or connection.execute('PRAGMA query_only').fetchone()[0]):
            raise ValueError('professional_evidence_storage_setup_unavailable')
        connection.execute('BEGIN IMMEDIATE')
        if _schema(connection):
            _attest(connection)
            connection.rollback()
            return False
        if (connection.execute('PRAGMA application_id').fetchone()[0]
                or connection.execute('PRAGMA user_version').fetchone()[0]):
            raise ValueError('professional_evidence_storage_setup_unavailable')
        for statement in iter_sql_statements(SCHEMA_PATH.read_text(encoding='utf-8')):
            connection.execute(statement)
        connection.execute('INSERT INTO preparation_meta VALUES (1, ?, 0)', (uuid.uuid4().hex,))
        connection.execute(f'PRAGMA application_id = {APPLICATION_ID}')
        connection.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        _attest(connection)
        connection.commit()
        return True
    except (sqlite3.Error, ValueError, TypeError):
        if type(connection) is sqlite3.Connection and connection.in_transaction:
            connection.rollback()
        raise ValueError('professional_evidence_storage_setup_unavailable') from None


def _attest(connection):
    if (connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
            or connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION
            or _schema(connection) != _expected_schema()):
        raise ValueError('professional_evidence_storage_unavailable')
    rows = connection.execute('SELECT store_id,generation FROM preparation_meta').fetchall()
    if (len(rows) != 1 or type(rows[0][0]) is not str or len(rows[0][0]) != 32
            or type(rows[0][1]) is not int or rows[0][1] < 0):
        raise ValueError('professional_evidence_storage_unavailable')
    return tuple(rows[0])


class SQLiteProfessionalBackgroundStore:
    """One explicit local file, concurrent short transactions, no persistent cache.

    Instances cannot cross a fork/process boundary. Fresh processes construct
    their own instance. The database identity cannot change under an instance.
    The file and its directory have the same private operator trust as product
    storage; the checksum detects damage, not malicious database administrators.
    """
    def __init__(self, path):
        if not isinstance(path, (str, Path)) or not Path(path).is_absolute():
            raise ValueError('professional_evidence_storage_path_required')
        self.path = Path(path).resolve()
        self._pid = os.getpid()
        self._store_id = None
        with self.transaction() as (connection, token):
            self._store_id = token[0]

    @contextmanager
    def transaction(self, *, write=False, expected=None):
        connection = None
        try:
            if self._pid != os.getpid():
                raise ValueError('professional_evidence_storage_process_mismatch')
            connection = sqlite3.connect(self.path.as_uri() + '?mode=' + ('rw' if write else 'ro'),
                                         uri=True, timeout=1.0)
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 131072)
            connection.execute('PRAGMA foreign_keys = ON')
            if not write:
                connection.execute('PRAGMA query_only = ON')
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            token = _attest(connection)
            if self._store_id is not None and token[0] != self._store_id:
                raise ValueError('professional_evidence_storage_identity_changed')
            if expected is not None and token != expected:
                raise ValueError('professional_evidence_changed_during_consumption')
            yield connection, token
            # Read/acceptance guards need no commit; mutations commit explicitly.
        except sqlite3.Error:
            raise ValueError('professional_evidence_storage_unavailable') from None
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()

    @property
    def generation(self):
        with self.transaction() as (_, token):
            return token

    def read(self, request):
        with self.transaction() as (connection, _):
            return self.read_in_transaction(connection, request)

    def read_in_transaction(self, connection, request):
        """Same exact-record validation inside a caller's acceptance guard."""
        row = connection.execute('SELECT owner_json,state,record_json,record_sha256 '
            'FROM preparation_results WHERE request_id=?', (request['request_id'],)).fetchone()
        if row is None:
            return 'absent', None
        try:
            if row[0] != _json(request['binding']['owner']):
                return 'invalid', None
            if row[1] in ('attempted', 'revoked') and row[2:] == (None, None):
                return row[1], None
            if row[1] != 'validated' or len(row[2].encode('utf-8')) > 65536:
                return 'invalid', None
            record = json.loads(row[2], object_pairs_hook=_unique_object)
            if (digest(record) != row[3] or set(record) != {'binding', 'output', 'model_identity', 'provenance'}
                    or record['binding'] != request['binding']):
                return 'invalid', None
            return 'validated', record
        except (ValueError, TypeError, KeyError, AttributeError):
            return 'invalid', None

    def _write(self, request, state, record, *, expected, retain_valid=False):
        raw = None if record is None else _json(record)
        if raw is not None and len(raw.encode('utf-8')) > 65536:
            raise ValueError('professional_evidence_storage_record_limit')
        with self.transaction(write=True, expected=expected) as (connection, token):
            old = connection.execute('SELECT state FROM preparation_results WHERE request_id=?',
                                     (request['request_id'],)).fetchone()
            if not (retain_valid and old == ('validated',)):
                connection.execute('INSERT INTO preparation_results VALUES (?,?,?,?,?) '
                    'ON CONFLICT(request_id) DO UPDATE SET owner_json=excluded.owner_json, '
                    'state=excluded.state,record_json=excluded.record_json,record_sha256=excluded.record_sha256',
                    (request['request_id'], _json(request['binding']['owner']), state, raw,
                     None if record is None else digest(record)))
            connection.execute('UPDATE preparation_meta SET generation=generation+1 WHERE singleton=1')
            connection.commit()
            return token[0], token[1] + 1

    def begin_attempt(self, request, *, expected):
        # An interrupted/failed initial attempt remains a no-retry tombstone.
        # Failed replacement preserves the earlier validated record.
        return self._write(request, 'attempted', None, expected=expected, retain_valid=True)

    def publish(self, request, record, *, expected):
        return self._write(request, 'validated', record, expected=expected)

    def revoke(self, request):
        return self._write(request, 'revoked', None, expected=self.generation)

    def purge_owner(self, owner, *, admin):
        """Explicit privacy operation; physical deletion of this owner's payloads.

        Existing privacy-admin authority is required. No public endpoint or
        account state change. Pending publications fail their generation CAS.
        """
        from wahojobs.persistent_profiles import TrustedPrivacyAdminContext
        if (type(admin) is not TrustedPrivacyAdminContext
                or type(owner) is not tuple or len(owner) != 3
                or any(type(v) is not str or not v for v in owner)
                or admin.operation_scope != 'purge' or admin.environment_namespace != owner[1]):
            raise ValueError('professional_evidence_purge_authorization_denied')
        with self.transaction(write=True) as (connection, _):
            connection.execute('PRAGMA secure_delete = ON')
            count = connection.execute('DELETE FROM preparation_results WHERE owner_json=?',
                                       (_json(list(owner)),)).rowcount
            connection.execute('UPDATE preparation_meta SET generation=generation+1 WHERE singleton=1')
            connection.commit()
            return count

    def __repr__(self):
        return 'SQLiteProfessionalBackgroundStore(content=<redacted>)'


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_record_key')
        result[key] = value
    return result
