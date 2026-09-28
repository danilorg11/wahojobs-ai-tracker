"""Explicit cold snapshots and non-overwriting recovery for one beta database.

No default paths, config/secret reads, migrations, network, pin rebinding or
in-place restore. The operator must stop all writers before taking a snapshot.
The product lifetime owner prevents participating application/maintenance work.
"""
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat

from wahojobs.crawler.local_inventory import local_database_path
from wahojobs.database_lifetime_ownership import (
    ROLE_OFFLINE_OPERATOR, acquire_database_lifetime_ownership,
    release_database_lifetime_ownership,
)
from wahojobs.evidence_maintenance import database_identity, journal_binding, report
from wahojobs.workos_authkit_staging import validate_workos_authkit_staging_database

VERSION = 'private_beta_cold_snapshot_v1'
MAX_FILES = 10000
MAX_MANIFEST_BYTES = 8_000_000
PREPARATION_VERSION = 'private_beta_journal_preparation_v1'
MAX_PREPARATION_AGE_SECONDS = 900


def _file(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve():
        raise ValueError('recovery_absolute_canonical_path_required')
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('recovery_unsafe_path')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('recovery_ordinary_single_link_file_required')
    return path


def _hash(path):
    with _file(path).open('rb') as stream:
        digest = sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
        return digest.hexdigest()


def _new_directory(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve() or path.exists():
        raise ValueError('recovery_new_absolute_directory_required')
    if any((part / '.git').exists() for part in path.parents):
        raise ValueError('recovery_storage_must_be_outside_git')
    # Require an existing safe parent. Never recursively create an accidental path.
    for part in path.parents:
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('recovery_unsafe_path')
    path.mkdir(mode=0o700)
    return path


def _write(path, raw):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _json(value):
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True).encode('ascii')


def _check_sqlite(path, *, product=False, companion=False, read_only=True):
    _file(path)
    # A verifier must never recover a crashed writer or consume its journal.
    # lexists also rejects dangling sidecar links before SQLite touches storage.
    if any(os.path.lexists(str(path) + suffix) for suffix in ('-journal', '-wal', '-shm')):
        raise ValueError('recovery_sqlite_sidecars_present')
    with path.open('rb') as stream:
        header = stream.read(100)
    if header[:16] != b'SQLite format 3\x00' or header[18:20] != b'\x01\x01':
        raise ValueError('recovery_rollback_journal_required')
    with closing(sqlite3.connect(path.as_uri() + ('?mode=ro' if read_only else '?mode=rw'), uri=True)) as connection:
        if read_only:
            connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA foreign_keys=ON')
        if connection.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
            raise ValueError('recovery_rollback_journal_required')
        if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError('recovery_integrity_failed')
        if connection.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('recovery_foreign_keys_failed')
        if product and read_only:
            # Reuse the exact supported attestor, including its writable probe,
            # on an in-memory copy. The immutable snapshot stays read-only.
            with closing(sqlite3.connect(':memory:')) as validation:
                connection.backup(validation)
                validation.execute('PRAGMA foreign_keys=ON')
                validate_workos_authkit_staging_database(validation)
        elif product:
            # Original cold-backup source remains under offline ownership and
            # must also pass the existing real-file nonparticipating-writer probe.
            validate_workos_authkit_staging_database(connection)
        if companion:
            from wahojobs.professional_background_store import _attest
            _attest(connection)


def _inventory(database, companion, *, validate_journal=True, binding_output=None):
    files = {'product.sqlite3': database}
    from wahojobs.storage_relocation import LINEAGE, HOLD, sidecar, relocation_binding
    if sidecar(database, LINEAGE).exists():
        relocation_binding(database)
    for name, suffix in (('maintenance-lineage.json', LINEAGE), ('recovery-hold.json', HOLD)):
        path = sidecar(database, suffix)
        if path.exists():
            files[name] = _file(path)
    history = database.parent / 'lineage-history'
    if history.exists():
        for path in history.iterdir():
            import re
            if not re.fullmatch('[0-9a-f]{64}\\.json', path.name):
                raise ValueError('recovery_lineage_invalid')
            if _hash(path) != path.stem:
                raise ValueError('recovery_lineage_invalid')
            files['lineage-history/' + path.name] = _file(path)
    drafts = database.with_name(database.name + '.correction-drafts.sqlite3')
    if drafts.exists():
        files['correction-drafts.sqlite3'] = local_database_path(drafts)
    if companion is not None:
        companion = local_database_path(companion)
        if companion in files.values():
            raise ValueError('recovery_distinct_companion_required')
        files['companion.sqlite3'] = companion
    binding = journal_binding(database)
    if binding is not None:
        files['maintenance-pin.json'] = database.with_name(database.name + '.evidence-maintenance.json')
        root = Path(binding['journal_root'])
        if not root.is_absolute() or not root.is_dir() or root != root.resolve():
            raise ValueError('recovery_journal_unavailable')
        for path in sorted(root.rglob('*')):
            if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('recovery_unsafe_journal')
            if path.is_file():
                files['journal/' + path.relative_to(root).as_posix()] = _file(path)
        # Validate existing complete and interrupted chains, not just their file bytes.
        if validate_journal:
            _validate_journal(root)
    if len(files) > MAX_FILES:
        raise ValueError('recovery_snapshot_file_limit')
    if binding_output is not None:
        binding_output.append(binding)
    return files


def _validate_journal(root):
    for path in root.iterdir():
        if path.is_dir() and (path / 'plan.json').exists():
            report(root, path.name)


def _release_labels(code_commit, configuration_revision, run_id=None):
    import re
    if (not isinstance(code_commit, str) or not re.fullmatch('[a-f0-9]{40}', code_commit)
            or not isinstance(configuration_revision, str)
            or not re.fullmatch('[A-Za-z0-9_.-]{1,80}', configuration_revision)):
        raise ValueError('recovery_nonsecret_release_labels_required')
    if run_id is not None and (not isinstance(run_id, str)
            or not re.fullmatch('[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', run_id)):
        raise ValueError('recovery_preparation_binding_invalid')


def _journal_files(files):
    return {name: path for name, path in files.items()
            if name == 'maintenance-pin.json' or name.startswith('journal/')}


def _records(files):
    return {name: dict(identity=database_identity(path), sha256=_hash(path))
            for name, path in files.items()}


def _bound_inventory(database, companion=None):
    binding = []
    files = _inventory(database, companion, validate_journal=False, binding_output=binding)
    return files, binding[0]


def _prepare_snapshot_journal(database, destination, *, run_id, code_commit, configuration_revision):
    """Stage only immutable evidence while the caller holds the operation gate.

    The native supervisor lends its verified gate to its worker. Standalone
    callers use prepare_snapshot_journal, which acquires that gate itself.
    Runtime user writes remain available; no SQLite contents are read or copied.
    """
    _release_labels(code_commit, configuration_revision, run_id)
    if run_id is None:
        raise ValueError('recovery_preparation_binding_invalid')
    database = local_database_path(database)
    identity = database_identity(database)
    inventory, binding = _bound_inventory(database)
    sources = _journal_files(inventory)
    if binding and Path(destination).is_relative_to(Path(binding['journal_root'])):
        raise ValueError('recovery_preparation_binding_invalid')
    before = _records(sources)
    target = _new_directory(destination)
    for name, path in sources.items():
        output = target / name
        output.parent.mkdir(parents=True, exist_ok=True)
        with path.open('rb') as src, output.open('xb') as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if _hash(output) != before[name]['sha256']:
            raise ValueError('recovery_source_changed')
    if any(name.startswith('journal/') for name in sources):
        _validate_journal(target / 'journal')
    after_inventory, after_binding = _bound_inventory(database)
    after = _records(_journal_files(after_inventory))
    if before != after or identity != database_identity(database) or binding != after_binding:
        raise ValueError('recovery_source_changed')
    receipt = dict(version=PREPARATION_VERSION, run_id=run_id, code_commit=code_commit,
        configuration_revision=configuration_revision, database=identity,
        journal_binding=binding, files=before, created_at=datetime.now(timezone.utc).isoformat())
    raw = _json(receipt)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError('recovery_manifest_limit')
    _write(target / 'PREPARED.json', raw)
    _write(target / 'PREPARED.sha256', (sha256(raw).hexdigest() + '\n').encode())
    return receipt


def prepare_snapshot_journal(database, destination, **options):
    """Prepare evidence online without acquiring product lifetime ownership."""
    from wahojobs.maintenance_gate import operation_gate
    with operation_gate(database):
        return _prepare_snapshot_journal(database, destination, **options)


def _prepared_journal(prepared, database, sources, *, binding, run_id, code_commit, configuration_revision,
                      expected_preparation_sha256):
    prepared = Path(prepared)
    path = _file(prepared / 'PREPARED.json')
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('recovery_manifest_limit')
    raw = path.read_bytes()
    # This digest comes directly from the successful preparation call (or its
    # worker pipe), never from a mutable on-disk reference. The disk checksum
    # alone is not authority to skip journal chain validation in the cold phase.
    digest = sha256(raw).hexdigest()
    if (digest != expected_preparation_sha256
            or digest != _file(prepared / 'PREPARED.sha256').read_text().strip()):
        raise ValueError('recovery_preparation_integrity_failed')
    receipt = json.loads(raw)
    if (receipt.get('version') != PREPARATION_VERSION or receipt.get('run_id') != run_id or run_id is None
            or receipt.get('code_commit') != code_commit or receipt.get('configuration_revision') != configuration_revision
            or receipt.get('database') != database_identity(database)
            or receipt.get('journal_binding') != binding
            or receipt.get('files') != _journal_files(sources)):
        raise ValueError('recovery_preparation_binding_invalid')
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(receipt['created_at'])).total_seconds()
    except (ValueError, TypeError, KeyError):
        raise ValueError('recovery_preparation_binding_invalid') from None
    if not 0 <= age <= MAX_PREPARATION_AGE_SECONDS:
        raise ValueError('recovery_preparation_expired')
    actual = set()
    for member in prepared.rglob('*'):
        if member.is_symlink() or getattr(member.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('recovery_unsafe_journal')
        if member.is_file():
            actual.add(member.relative_to(prepared).as_posix())
            _file(member)
    if actual != {*receipt['files'], 'PREPARED.json', 'PREPARED.sha256'}:
        raise ValueError('recovery_preparation_integrity_failed')
    for name, record in receipt['files'].items():
        if _hash(prepared / name) != record['sha256']:
            raise ValueError('recovery_preparation_integrity_failed')
    return receipt


def _create_snapshot(database, destination, *, companion=None, code_commit, configuration_revision, ownership=None,
                     prepared_journal=None, run_id=None, expected_preparation_sha256=None):
    """Snapshot current quiescent storage, optionally adopting prepared evidence.

    Preparation never supplies a database or the protected-domain baseline.
    The finished artifact retains the existing independently restorable v1 format.
    """
    _release_labels(code_commit, configuration_revision, run_id)
    database = local_database_path(database)
    from wahojobs.database_lifetime_ownership import require_database_lifetime_ownership
    lease = ownership or acquire_database_lifetime_ownership(database, role=ROLE_OFFLINE_OPERATOR)
    require_database_lifetime_ownership(lease, role=ROLE_OFFLINE_OPERATOR, database_path=database)
    cold_connections = []
    try:
        sources, binding = _bound_inventory(database, companion)
        before = _records(sources)
        if prepared_journal is not None:
            _prepared_journal(prepared_journal, database, before, binding=binding, run_id=run_id,
                code_commit=code_commit, configuration_revision=configuration_revision,
                expected_preparation_sha256=expected_preparation_sha256)
            if Path(prepared_journal).stat().st_dev != Path(destination).parent.stat().st_dev:
                raise ValueError('recovery_preparation_same_filesystem_required')
        for name, path in sources.items():
            if name.endswith('.sqlite3'):
                _check_sqlite(path, product=name == 'product.sqlite3', companion=name == 'companion.sqlite3', read_only=False)
                cold = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=0)
                cold_connections.append(cold)
                cold.execute('BEGIN EXCLUSIVE')
        target = _new_directory(destination)
        if prepared_journal is not None and any(name.startswith('journal/') for name in sources):
            # Atomic same-filesystem adoption keeps the validated artifact bytes
            # and avoids another full history copy while the app is unavailable.
            (Path(prepared_journal) / 'journal').rename(target / 'journal')
        for name, path in sources.items():
            if prepared_journal is not None and name.startswith('journal/'):
                continue
            output = target / name
            output.parent.mkdir(parents=True, exist_ok=True)
            with path.open('rb') as src, output.open('xb') as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            if _hash(output) != before[name]['sha256']:
                raise ValueError('recovery_source_changed')
        # Validate the actual copied journal once, before declaring a snapshot
        # complete. Its bytes already match the initial source hashes; the final
        # inventory below still checks all paths, membership, identities and
        # hashes. This binds the chain proof to the backup without decoding the
        # growing historical chains twice during the unavailable interval.
        if prepared_journal is None and any(name.startswith('journal/') for name in sources):
            _validate_journal(target / 'journal')
        after_sources = _inventory(database, companion, validate_journal=False)
        after = {name: dict(identity=database_identity(path), sha256=_hash(path))
                 for name, path in after_sources.items()}
        if before != after:
            raise ValueError('recovery_source_changed')
        manifest = dict(version=VERSION, created_at=datetime.now(timezone.utc).isoformat(),
            code_commit=code_commit, configuration_revision=configuration_revision,
            files=before, companion_configured=companion is not None,
            maintenance_pinned='maintenance-pin.json' in sources)
        raw = _json(manifest)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError('recovery_manifest_limit')
        _write(target / 'manifest.json', raw)
        _write(target / 'COMPLETE.sha256', (sha256(raw).hexdigest() + '\n').encode())
        return manifest
    finally:
        for cold in reversed(cold_connections):
            cold.rollback()
            cold.close()
        if ownership is None:
            release_database_lifetime_ownership(lease, role=ROLE_OFFLINE_OPERATOR, database_path=database)


def verify_snapshot(snapshot):
    snapshot = Path(snapshot)
    raw = _file(snapshot / 'manifest.json').read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES or sha256(raw).hexdigest() != _file(snapshot / 'COMPLETE.sha256').read_text().strip():
        raise ValueError('recovery_manifest_invalid')
    manifest = json.loads(raw)
    files = manifest.get('files', {})
    if (manifest.get('version') != VERSION or type(files) is not dict
            or not 1 <= len(files) <= MAX_FILES or 'product.sqlite3' not in files
            or type(manifest.get('companion_configured')) is not bool
            or type(manifest.get('maintenance_pinned')) is not bool
            or manifest['companion_configured'] != ('companion.sqlite3' in files)
            or manifest['maintenance_pinned'] != ('maintenance-pin.json' in files)
            or ('maintenance-lineage.json' in files) != ('recovery-hold.json' in files)):
        raise ValueError('recovery_manifest_invalid')
    for name, record in files.items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name
                or relative.as_posix() != name or name not in {
                    'product.sqlite3', 'companion.sqlite3', 'correction-drafts.sqlite3',
                    'maintenance-pin.json', 'maintenance-lineage.json', 'recovery-hold.json'}
                and not name.startswith(('journal/', 'lineage-history/'))):
            raise ValueError('recovery_manifest_invalid')
        if _hash(snapshot / name) != record.get('sha256'):
            raise ValueError('recovery_snapshot_integrity_failed')
        if name.endswith('.sqlite3') and '/' not in name:
            _check_sqlite(snapshot / name, product=name == 'product.sqlite3', companion=name == 'companion.sqlite3')
    return manifest


def restored_name(name, record):
    names = {'product.sqlite3': 'product.sqlite3', 'companion.sqlite3': 'companion.sqlite3',
        'correction-drafts.sqlite3': 'product.sqlite3.correction-drafts.sqlite3',
        'maintenance-pin.json': 'product.sqlite3.evidence-maintenance.json'}
    if name in ('maintenance-lineage.json', 'recovery-hold.json'):
        return 'lineage-history/' + record['sha256'] + '.json'
    return names.get(name, name)


def restore_snapshot(snapshot, destination):
    """Restore into an absent directory only, retaining every immutable receipt.

    The copied maintenance pin intentionally keeps the ORIGINAL database identity
    and journal path. Maintenance on recovered storage therefore fails closed.
    Normal application consumption needs no maintenance pin mutation.
    """
    snapshot = Path(snapshot)
    manifest = verify_snapshot(snapshot)
    target = _new_directory(destination)
    from wahojobs.storage_relocation import VERSION as RELOCATION_VERSION, HOLD, sidecar, _durable_write
    _durable_write(sidecar(target / 'product.sqlite3', HOLD), dict(version=RELOCATION_VERSION,
        database_path=str(target / 'product.sqlite3'), snapshot_manifest_sha256=_hash(snapshot / 'manifest.json')))
    for name, record in manifest['files'].items():
        output = target / restored_name(name, record)
        output.parent.mkdir(parents=True, exist_ok=True)
        with (snapshot / name).open('rb') as src, output.open('xb') as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if _hash(output) != record['sha256']:
            raise ValueError('recovery_restore_integrity_failed')
        if name.endswith('.sqlite3') and '/' not in name:
            _check_sqlite(output, product=name == 'product.sqlite3', companion=name == 'companion.sqlite3')
    # Recheck the source snapshot as well; an interrupted restore has no READY receipt.
    verify_snapshot(snapshot)
    receipt = dict(version=VERSION, snapshot_manifest_sha256=_hash(snapshot / 'manifest.json'),
        file_count=len(manifest['files']), product_database='product.sqlite3',
        companion_database='companion.sqlite3' if manifest['companion_configured'] else None,
        maintenance='held_original_physical_identity' if manifest['maintenance_pinned'] else 'unconfigured',
        activation='blocked_until_lossless_authoritative_reconciliation', code_commit=manifest['code_commit'],
        configuration_revision=manifest['configuration_revision'],
        limitation='No history, consumed attempt, pin or journal is rewritten. Reconcile any post-snapshot writes before activation.')
    _write(target / 'RECOVERY-READY.json', _json(receipt))
    return receipt


def create_snapshot(database, destination, *, ownership=None, **options):
    if ownership is not None:
        return _create_snapshot(database, destination, ownership=ownership, **options)
    from wahojobs.maintenance_gate import operation_gate
    with operation_gate(database):
        return _create_snapshot(database, destination, **options)
