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
    with path.open('xb') as stream:
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


def _inventory(database, companion):
    files = {'product.sqlite3': database}
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
        for path in root.iterdir():
            if path.is_dir() and (path / 'plan.json').exists():
                report(root, path.name)
    if len(files) > MAX_FILES:
        raise ValueError('recovery_snapshot_file_limit')
    return files


def create_snapshot(database, destination, *, companion=None, code_commit, configuration_revision):
    """Snapshot quiescent storage; never inspect or copy runtime configuration.

    code_commit and configuration_revision are operator-selected nonsecret labels.
    The snapshot contains private candidate data and requires private OS storage.
    """
    import re
    if (not isinstance(code_commit, str) or not re.fullmatch('[a-f0-9]{40}', code_commit)
            or not isinstance(configuration_revision, str)
            or not re.fullmatch('[A-Za-z0-9_.-]{1,80}', configuration_revision)):
        raise ValueError('recovery_nonsecret_release_labels_required')
    database = local_database_path(database)
    lease = acquire_database_lifetime_ownership(database, role=ROLE_OFFLINE_OPERATOR)
    try:
        sources = _inventory(database, companion)
        before = {name: dict(identity=database_identity(path), sha256=_hash(path))
                  for name, path in sources.items()}
        for name, path in sources.items():
            if name.endswith('.sqlite3'):
                _check_sqlite(path, product=name == 'product.sqlite3', companion=name == 'companion.sqlite3', read_only=False)
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
        after_sources = _inventory(database, companion)
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
        release_database_lifetime_ownership(lease, role=ROLE_OFFLINE_OPERATOR, database_path=database)


def verify_snapshot(snapshot):
    snapshot = Path(snapshot)
    raw = _file(snapshot / 'manifest.json').read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES or sha256(raw).hexdigest() != _file(snapshot / 'COMPLETE.sha256').read_text().strip():
        raise ValueError('recovery_manifest_invalid')
    manifest = json.loads(raw)
    files = manifest.get('files', {})
    if (manifest.get('version') != VERSION or type(files) is not dict
            or not 1 <= len(files) <= MAX_FILES or 'product.sqlite3' not in files):
        raise ValueError('recovery_manifest_invalid')
    for name, record in files.items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name
                or relative.as_posix() != name or name not in {
                    'product.sqlite3', 'companion.sqlite3', 'correction-drafts.sqlite3',
                    'maintenance-pin.json'} and not name.startswith('journal/')):
            raise ValueError('recovery_manifest_invalid')
        if _hash(snapshot / name) != record.get('sha256'):
            raise ValueError('recovery_snapshot_integrity_failed')
        if name.endswith('.sqlite3') and '/' not in name:
            _check_sqlite(snapshot / name, product=name == 'product.sqlite3', companion=name == 'companion.sqlite3')
    return manifest


def restore_snapshot(snapshot, destination):
    """Restore into an absent directory only, retaining every immutable receipt.

    The copied maintenance pin intentionally keeps the ORIGINAL database identity
    and journal path. Maintenance on recovered storage therefore fails closed.
    Normal application consumption needs no maintenance pin mutation.
    """
    snapshot = Path(snapshot)
    manifest = verify_snapshot(snapshot)
    target = _new_directory(destination)
    names = {'product.sqlite3': 'product.sqlite3', 'companion.sqlite3': 'companion.sqlite3',
        'correction-drafts.sqlite3': 'product.sqlite3.correction-drafts.sqlite3',
        'maintenance-pin.json': 'product.sqlite3.evidence-maintenance.json'}
    for name, record in manifest['files'].items():
        output = target / names.get(name, name)
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
        activation='explicit_runtime_configuration_required', code_commit=manifest['code_commit'],
        configuration_revision=manifest['configuration_revision'],
        limitation='No history, consumed attempt, pin or journal is rewritten. Reconcile any post-snapshot writes before activation.')
    _write(target / 'RECOVERY-READY.json', _json(receipt))
    return receipt
