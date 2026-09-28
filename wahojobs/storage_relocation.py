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
import re
import sqlite3

VERSION = 'private_beta_lossless_relocation_v1'
VERSION_V2 = 'private_beta_lossless_relocation_v2'
HOLD = '.recovery-hold.json'
LINEAGE = '.maintenance-relocation.json'
RETIRED = '.retired.json'
RECOVERY_AUTHORITY = object()  # in-process operator capability; never config/HTTP


def sidecar(database, suffix):
    return Path(str(database) + suffix)


def _read(path):
    from wahojobs.beta_recovery import _file
    if _file(path).stat().st_size > 8_000_000:
        raise ValueError('recovery_lineage_invalid')
    from wahojobs.recovery_archive import load_json
    return load_json(path.read_bytes())


def _durable_write(path, value):
    from wahojobs.beta_recovery import _write, _json
    raw = _json(value)
    if len(raw) > 8_000_000:
        raise ValueError('recovery_lineage_limit')
    _write(path, raw)
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
    if (receipt.get('version') not in (VERSION, VERSION_V2) or receipt.get('database') != database_identity(database)
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
        for name, expected in _journal_receipt_records(database.parent, receipt):
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
    if receipt['version'] == VERSION_V2:
        predecessor = receipt.get('predecessor')
        if predecessor is not None and (not isinstance(predecessor, str)
                or not re.fullmatch('[0-9a-f]{64}', predecessor)
                or expected_history.get(predecessor + '.json') != predecessor):
            raise ValueError('recovery_lineage_invalid')
    return dict(database=database_identity(database), journal_root=str(root)) if root else None


def _journal_receipt_records(directory, receipt):
    if receipt['version'] == VERSION:
        yield from receipt['journal_files'].items()
        return
    from wahojobs import recovery_archive as archive
    from wahojobs.beta_recovery import _hash, _file
    chunks = receipt.get('journal_chunks')
    if type(chunks) is not list or len(chunks) > archive.MAX_SEGMENTS:
        raise ValueError('recovery_lineage_invalid')
    records = []
    previous = None
    for chunk in chunks:
        if (type(chunk) is not dict or not isinstance(chunk.get('sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', chunk['sha256'])):
            raise ValueError('recovery_lineage_invalid')
        path = directory / 'lineage-history' / (chunk['sha256'] + '.json')
        if _file(path).stat().st_size > archive.MAX_INDEX_BYTES:
            raise ValueError('recovery_lineage_invalid')
        if _hash(path) != chunk['sha256']:
            raise ValueError('recovery_lineage_invalid')
        value = _read(path)
        files = value.get('files')
        if (value.get('version') != archive.CHUNK_VERSION or type(files) is not dict
                or not 1 <= len(files) <= archive.MAX_MEMBERS or len(files) != chunk.get('members')):
            raise ValueError('recovery_lineage_invalid')
        names = sorted(files)
        if (chunk.get('first') != names[0] or chunk.get('last') != names[-1]
                or previous is not None and names[0] <= previous):
            raise ValueError('recovery_lineage_invalid')
        previous = names[-1]
        if len(records) + len(files) > archive.MAX_LOGICAL_FILES:
            raise ValueError('recovery_lineage_invalid')
        records.extend((name, files[name]) for name in names)
    if archive.inventory_summary(records) != receipt.get('journal_inventory'):
        raise ValueError('recovery_lineage_invalid')
    for name, record in records:
        yield name[8:], record['sha256']


def _journal_chunks(destination, records):
    from wahojobs import recovery_archive as archive
    from wahojobs.beta_recovery import _json
    history = destination / 'lineage-history'
    history.mkdir(mode=0o700, exist_ok=True)
    chunks = []
    names = sorted(name for name in records if name.startswith('journal/'))
    for start in range(0, len(names), archive.MAX_MEMBERS):
        group = {name: records[name] for name in names[start:start + archive.MAX_MEMBERS]}
        value = dict(version=archive.CHUNK_VERSION, files=group)
        raw = _json(value)
        if len(raw) > archive.MAX_INDEX_BYTES:
            raise ValueError('recovery_lineage_limit')
        digest = sha256(raw).hexdigest()
        _write_identical(history / (digest + '.json'), value)
        chunks.append(dict(sha256=digest, members=len(group), first=next(iter(group)), last=next(reversed(group))))
    if len(chunks) > archive.MAX_SEGMENTS:
        raise ValueError('recovery_lineage_limit')
    return chunks


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
    from wahojobs.beta_recovery import verify_snapshot, _inventory, _hash, _check_sqlite, _file, restored_name, logical_snapshot_records
    from wahojobs import recovery_archive as archive
    from wahojobs.evidence_maintenance import database_identity, report
    from wahojobs.database_lifetime_ownership import (
        acquire_database_lifetime_ownership, release_database_lifetime_ownership, ROLE_OFFLINE_OPERATOR)
    snapshot, destination = Path(snapshot), Path(destination)
    manifest = verify_snapshot(snapshot)
    logical = dict(logical_snapshot_records(snapshot, manifest))
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
        sources = _inventory(source, authoritative_companion, snapshot_version=manifest['version'])
        if set(sources) != set(logical):
            raise ValueError('recovery_authoritative_history_changed')
        targets = {name: destination / restored_name(name, record) for name, record in logical.items()}
        # Exclude nonparticipating SQLite writers, including companion writers,
        # throughout the final comparison and durable handoff.
        for path in sorted({*sources.values(), *targets.values()}, key=str):
            if path.suffix == '.sqlite3':
                _check_sqlite(path)
                connection = stack.enter_context(closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=0)))
                connection.execute('BEGIN EXCLUSIVE')
                stack.callback(connection.rollback)
        for name, path in sources.items():
            expected = logical[name]
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
            if actual != {name[8:] for name in logical if name.startswith('journal/')}:
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
            journal_files={name[8:]: record['sha256'] for name, record in logical.items()
                           if name.startswith('journal/')},
            lineage_files={path.name: _hash(path) for path in targets.values()
                           if path.parent == destination / 'lineage-history'},
            predecessor=_read(sources['maintenance-lineage.json']) if 'maintenance-lineage.json' in sources else None,
            semantics='No row or binding rewritten; all consumed and uncertain attempts retained.')
        if manifest['version'] == archive.VERSION:
            receipt['version'] = VERSION_V2
            receipt.pop('journal_files')
            receipt['journal_chunks'] = _journal_chunks(destination, logical)
            receipt['journal_inventory'] = manifest['journal_inventory']
            receipt['predecessor'] = logical.get('maintenance-lineage.json', {}).get('sha256')
            for chunk in receipt['journal_chunks']:
                receipt['lineage_files'][chunk['sha256'] + '.json'] = chunk['sha256']
            # Refuse an oversized receipt before fencing its source.
            from wahojobs.beta_recovery import _json
            if len(_json(receipt)) > 8_000_000:
                raise ValueError('recovery_lineage_limit')
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
