"""Explicit, lossless cold relocation. Never merge an older backup into authority.

The original database must still be available and exactly equal to the snapshot,
including all journals and companion reservations. Missing or newer authority
blocks activation. Source retirement precedes destination activation durably.
"""
from contextlib import ExitStack, closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

VERSION = 'private_beta_lossless_relocation_v1'
HOLD = '.recovery-hold.json'
LINEAGE = '.maintenance-relocation.json'
RETIRED = '.retired.json'
RECOVERY_AUTHORITY = object()  # in-process operator capability; never config/HTTP


def sidecar(database, suffix):
    return Path(str(database) + suffix)


def _read(path):
    from wahojobs.beta_recovery import _file
    raw = _file(path).read_bytes()
    if len(raw) > 8_000_000:
        raise ValueError('recovery_lineage_invalid')
    return json.loads(raw)


def _durable_write(path, value):
    from wahojobs.beta_recovery import _write, _json
    _write(path, _json(value))
    if os.name == 'posix':
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_identical(path, value):
    if path.exists():
        if _read(path) != value:
            raise ValueError('recovery_relocation_already_assigned')
        with path.open('r+b') as stream:
            os.fsync(stream.fileno())
        if os.name == 'posix':
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    else:
        _durable_write(path, value)


def _persist_destination(destination):
    """Persist recovered bytes and all directory entries before source fencing."""
    from wahojobs.beta_recovery import _file
    from wahojobs.workos_authkit_staging import _require_no_reparse_components
    directories = [destination]
    for path in destination.rglob('*'):
        _require_no_reparse_components(path)
        if path.is_dir():
            directories.append(path)
        else:
            with _file(path).open('r+b') as stream:
                os.fsync(stream.fileno())
    if os.name == 'posix':
        for path in sorted(directories, key=lambda p: len(p.parts), reverse=True) + [destination.parent]:
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def relocation_binding(database, original=None):
    from wahojobs.beta_recovery import _hash
    from wahojobs.evidence_maintenance import database_identity
    receipt = _read(sidecar(database, LINEAGE))
    hold = _read(sidecar(database, HOLD))
    if (receipt.get('version') != VERSION or receipt.get('database') != database_identity(database)
            or receipt.get('hold_sha256') != _hash(sidecar(database, HOLD))
            or receipt.get('snapshot_manifest_sha256') != hold.get('snapshot_manifest_sha256')
            or hold.get('database_path') != str(database)):
        raise ValueError('recovery_lineage_invalid')
    pin = sidecar(database, '.evidence-maintenance.json')
    if receipt.get('original_pin_sha256') is None:
        if pin.exists() and _read(pin) != dict(database=database_identity(database),
                                              journal_root=str(database.parent / 'journal')):
            raise ValueError('recovery_lineage_invalid')
    elif receipt['original_pin_sha256'] != _hash(pin):
        raise ValueError('recovery_lineage_invalid')
    root = Path(receipt['journal_root']) if receipt['journal_root'] else None
    if root is not None:
        if root != database.parent / 'journal' or root != root.resolve() or not root.is_dir():
            raise ValueError('recovery_lineage_invalid')
        for name, expected in receipt['journal_files'].items():
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts or _hash(root / relative) != expected:
                raise ValueError('recovery_lineage_invalid')
    if original is not None and root is None:
        raise ValueError('recovery_lineage_invalid')
    history = database.parent / 'lineage-history'
    expected_history = receipt['lineage_files']
    actual_history = {p.name for p in history.iterdir()} if history.exists() else set()
    if actual_history != set(expected_history):
        raise ValueError('recovery_lineage_invalid')
    for name, expected in expected_history.items():
        if name != expected + '.json' or _hash(history / name) != expected:
            raise ValueError('recovery_lineage_invalid')
    return dict(database=database_identity(database), journal_root=str(root)) if root else None


def require_storage_activation(database):
    if os.path.lexists(sidecar(database, RETIRED)):
        raise ValueError('recovery_source_retired')
    if os.path.lexists(sidecar(database, HOLD)):
        if not sidecar(database, LINEAGE).exists():
            raise ValueError('recovery_reconciliation_required')
        relocation_binding(database)
    elif os.path.lexists(sidecar(database, LINEAGE)):
        raise ValueError('recovery_lineage_invalid')


def reconcile_relocation(snapshot, destination, authoritative_database, *, authoritative_companion=None,
                         checkpoint=None):
    """Retire the exact current source and activate one equal recovered copy.

    No network, private-state transformations, ledger reset, implicit retry or
    data overwrite. Partial output is retained; same-input retry can finish a
    fence-before-activation interruption. Different destination is rejected.
    """
    from wahojobs.beta_recovery import verify_snapshot, _inventory, _hash, _check_sqlite, _file, restored_name
    from wahojobs.evidence_maintenance import database_identity, report
    from wahojobs.database_lifetime_ownership import (
        acquire_database_lifetime_ownership, release_database_lifetime_ownership, ROLE_OFFLINE_OPERATOR)
    snapshot, destination = Path(snapshot), Path(destination)
    manifest = verify_snapshot(snapshot)
    source = _file(Path(authoritative_database))
    target = _file(destination / 'product.sqlite3')
    if os.path.lexists(sidecar(target, RETIRED)):
        raise ValueError('recovery_destination_retired')
    snapshot_hash = _hash(snapshot / 'manifest.json')
    hold = _read(sidecar(target, HOLD))
    if (source == target or database_identity(source) != manifest['files']['product.sqlite3']['identity']
            or hold != dict(version=VERSION, database_path=str(target), snapshot_manifest_sha256=snapshot_hash)):
        raise ValueError('recovery_authoritative_source_required')
    if bool(authoritative_companion is not None) != manifest['companion_configured']:
        raise ValueError('recovery_companion_required')
    with ExitStack() as stack:
        # Stable lock order; maintenance and runtime cannot own either database.
        for path in sorted((source, target), key=str):
            lease = acquire_database_lifetime_ownership(path, role=ROLE_OFFLINE_OPERATOR,
                _recovery_authority=RECOVERY_AUTHORITY)
            stack.callback(release_database_lifetime_ownership, lease,
                role=ROLE_OFFLINE_OPERATOR, database_path=path)
        sources = _inventory(source, authoritative_companion)
        if set(sources) != set(manifest['files']):
            raise ValueError('recovery_authoritative_history_changed')
        targets = {name: destination / restored_name(name, record) for name, record in manifest['files'].items()}
        # Exclude nonparticipating SQLite writers, including companion writers,
        # throughout the final comparison and durable handoff.
        for path in sorted({*sources.values(), *targets.values()}, key=str):
            if path.suffix == '.sqlite3':
                _check_sqlite(path)
                connection = stack.enter_context(closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=0)))
                connection.execute('BEGIN EXCLUSIVE')
                stack.callback(connection.rollback)
        for name, path in sources.items():
            expected = manifest['files'][name]
            if (database_identity(path) != expected['identity'] or _hash(path) != expected['sha256']
                    or _hash(targets[name]) != expected['sha256']):
                raise ValueError('recovery_authoritative_history_changed')
        root = destination / 'journal'
        if root != root.resolve() or root.is_symlink():
            raise ValueError('recovery_unsafe_journal')
        from wahojobs.workos_authkit_staging import _require_no_reparse_components
        _require_no_reparse_components(root if root.exists() else root.parent)
        if manifest['maintenance_pinned']:
            actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
            if actual != {name[8:] for name in manifest['files'] if name.startswith('journal/')}:
                raise ValueError('recovery_authoritative_history_changed')
            for path in root.iterdir():
                if path.is_dir() and (path / 'plan.json').exists():
                    report(root, path.name)
        else:
            root.mkdir(exist_ok=True)
            if any(root.iterdir()):
                raise ValueError('recovery_authoritative_history_changed')
        pin = sidecar(target, '.evidence-maintenance.json')
        receipt = dict(version=VERSION, database=database_identity(target),
            source_database=database_identity(source), snapshot_manifest_sha256=snapshot_hash,
            hold_sha256=_hash(sidecar(target, HOLD)),
            original_pin_sha256=_hash(pin) if pin.exists() else None,
            journal_root=str(root) if root else None,
            journal_files={name[8:]: record['sha256'] for name, record in manifest['files'].items()
                           if name.startswith('journal/')},
            lineage_files={path.name: _hash(path) for path in targets.values()
                           if path.parent == destination / 'lineage-history'},
            predecessor=_read(sources['maintenance-lineage.json']) if 'maintenance-lineage.json' in sources else None,
            semantics='No row or binding rewritten; all consumed and uncertain attempts retained.')
        from wahojobs.evidence_maintenance import digest
        fence = dict(version=VERSION, source_database=database_identity(source),
            destination_database=database_identity(target), activation_sha256=digest(receipt))
        if sidecar(target, LINEAGE).exists() and _read(sidecar(target, LINEAGE)) != receipt:
            raise ValueError('recovery_relocation_already_assigned')
        history = destination / 'lineage-history'
        actual_history = {p.name for p in history.iterdir()} if history.exists() else set()
        if actual_history != set(receipt['lineage_files']):
            raise ValueError('recovery_lineage_invalid')
        _persist_destination(destination)
        if checkpoint:
            checkpoint('destination_persisted')
        _write_identical(sidecar(source, RETIRED), fence)
        if checkpoint:
            checkpoint('source_retired')
        _write_identical(sidecar(target, LINEAGE), receipt)
        if checkpoint:
            checkpoint('destination_activated')
    require_storage_activation(target)
    return receipt
