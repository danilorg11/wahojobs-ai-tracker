"""Offline native rollback recovery; normal application guards remain strict."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import uuid

from wahojobs.beta_recovery import _file
from wahojobs.database_lifetime_ownership import (
    ROLE_OFFLINE_OPERATOR, acquire_database_lifetime_ownership,
    release_database_lifetime_ownership,
)


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _journal(path):
    journal = Path(str(path) + '-journal')
    for suffix in ('-wal', '-shm'):
        if os.path.lexists(str(path) + suffix):
            raise ValueError('unexpected_sqlite_recovery_mode')
    if not os.path.lexists(journal):
        return None
    _file(journal)
    with journal.open('rb') as stream:
        header = stream.read(28)
        stream.seek(max(0, journal.stat().st_size - 16))
        trailer = stream.read()
    if trailer[-8:] == bytes.fromhex('d9d505f920a163d7'):
        raise ValueError('linked_transaction_requires_operator_recovery')
    return header


def native_finalize(path):
    """SQLite alone consumes hot or zero-header rollback journals. No unlink."""
    path = _file(path)
    with path.open('rb') as stream:
        header = stream.read(20)
    if header[:16] != b'SQLite format 3\0' or header[18:20] != b'\1\1':
        raise ValueError('rollback_database_required')
    journal_header = _journal(path)
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=2)) as db:
        # This first real read allows SQLite to recover a hot journal.
        if db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
            raise ValueError('delete_journal_required')
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('recovered_integrity_failed')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('recovered_foreign_keys_failed')
        if Path(str(path) + '-journal').exists():
            if not journal_header or journal_header[:8] != b'\0' * 8:
                raise ValueError('native_recovery_incomplete')
            # A non-hot artifact can survive a read. An identical, rolled-back
            # header write asks SQLite to finalize it without changing data.
            version = db.execute('PRAGMA user_version').fetchone()[0]
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('PRAGMA user_version=' + str(version))
            finally:
                db.rollback()
        if _journal(path) is not None:
            raise ValueError('native_recovery_incomplete')


def recover_storage(database, evidence_root, *, validate, protected, expected_protected):
    """Caller holds the operation gate; this owner excludes app/worker writers.

    Preserve paired files before SQLite opens them, rehearse on another copy,
    and require identical validated results from authoritative native recovery.
    Evidence is never rewritten; unsupported linked transactions fail closed.
    """
    database = _file(database)
    lease = acquire_database_lifetime_ownership(database, role=ROLE_OFFLINE_OPERATOR)
    try:
        # Any open descriptor from another process is an additional exclusion
        # failure, including a nonparticipating SQLite connection.
        if os.name == 'posix' and Path('/proc').is_dir():
            for proc in Path('/proc').iterdir():
                if not proc.name.isdigit() or int(proc.name) == os.getpid():
                    continue
                try:
                    for fd in (proc / 'fd').iterdir():
                        try:
                            target = os.readlink(fd)
                        except FileNotFoundError:
                            continue
                        if target.startswith(str(database)) and target not in (
                                str(database)+'.wahojobs-maintenance.lock',str(database)+'.wahojobs-lifetime.lock'):
                            raise ValueError('database_process_still_active')
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except PermissionError:
                    # Root preflight checks all processes before dropping to the
                    # database owner. Other users cannot traverse its 0700 dir.
                    if proc.stat().st_uid == os.geteuid():
                        raise ValueError('process_exclusion_unavailable') from None
        files = [_file(p) for p in database.parent.glob(database.name + '*') if p.is_file() or p.is_symlink()]
        for p in files:
            if p.name.endswith(('-wal', '-shm')) or '-mj' in p.name:
                raise ValueError('linked_transaction_requires_operator_recovery')
            if p.name.endswith('-journal') and p.name != database.name+'-journal':
                raise ValueError('companion_transaction_requires_operator_recovery')
        _journal(database)
        interrupted = Path(str(database) + '-journal').exists()
        if not interrupted:
            validate(database)
            if expected_protected is not None and protected(database) != expected_protected:
                raise ValueError('protected_data_changed')
            return {'recovered': False, 'validated': True}
        if expected_protected is None:
            raise ValueError('verified_prepublication_baseline_required')
        root = Path(evidence_root)
        if not root.is_absolute() or root.resolve() != root:
            raise ValueError('safe_recovery_evidence_path_required')
        for parent in (root, *root.parents):
            if parent.exists() and parent.is_symlink():
                raise ValueError('safe_recovery_evidence_path_required')
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_IMODE(root.stat().st_mode) != 0o700 or root.stat().st_uid != os.geteuid():
            raise ValueError('private_recovery_evidence_required')
        required = sum(p.stat().st_size for p in files)
        if shutil.disk_usage(root).free < required * 3 + 64 * 1024**2:
            raise ValueError('recovery_space_unavailable')
        attempt = root / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex)
        attempt.mkdir(mode=0o700)
        evidence = attempt / 'evidence'; evidence.mkdir(mode=0o700)
        work = attempt / 'working'; work.mkdir(mode=0o700)
        manifest = {}
        for p in files:
            info = p.stat(); digest = _sha(p)
            manifest[p.name] = dict(sha256=digest, size=info.st_size, device=info.st_dev,
                inode=info.st_ino, uid=info.st_uid, gid=info.st_gid, mode=info.st_mode,
                mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)
            for destination in (evidence / p.name, work / p.name):
                shutil.copy2(p, destination)
                os.chmod(destination, 0o600)
                with destination.open('rb') as stream: os.fsync(stream.fileno())
                if _sha(destination) != digest: raise ValueError('recovery_copy_changed')
        with (attempt / 'manifest.json').open('x') as stream:
            json.dump(manifest, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
        for directory in (evidence, work, attempt, root):
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
        staged = work / database.name
        native_finalize(staged)
        validate(staged)
        if protected(staged) != expected_protected:
            raise ValueError('protected_data_changed')
        recovered_sha = _sha(staged)
        for p in files:
            if _sha(p) != manifest[p.name]['sha256']:
                raise ValueError('authoritative_changed_during_rehearsal')
        native_finalize(database)
        validate(database)
        if _sha(database) != recovered_sha or protected(database) != expected_protected:
            raise ValueError('authoritative_recovery_differs')
        result = dict(recovered=True, validated=True, evidence=str(attempt), sha256=recovered_sha)
        with (attempt / 'result.json').open('x') as stream:
            json.dump(result, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
        return result
    finally:
        release_database_lifetime_ownership(lease, role=ROLE_OFFLINE_OPERATOR, database_path=database)
