"""Server-private cross-process ownership for one exact database lifetime.

Importing this module is dormant.  Ownership is acquired only by an explicit
call to :func:`acquire_database_lifetime_ownership`.
"""

from __future__ import annotations

import errno
import io
import os
from pathlib import Path
import stat
import threading


ROLE_DURABLE_RUNTIME = "durable_runtime"
ROLE_OFFLINE_OPERATOR = "offline_operator"
_ROLES = frozenset({ROLE_DURABLE_RUNTIME, ROLE_OFFLINE_OPERATOR})

_ERROR_CATEGORIES = frozenset(
    {
        "invalid_request",
        "contention",
        "unavailable",
        "invalid_capability",
        "ownership_lost",
        "cleanup_incomplete",
        "unsupported_platform",
    }
)
_ERROR_MESSAGES = {
    "invalid_request": "Database lifetime ownership request is invalid.",
    "contention": "Database lifetime ownership is already held.",
    "unavailable": "Database lifetime ownership is unavailable.",
    "invalid_capability": "Database lifetime ownership capability is invalid.",
    "ownership_lost": "Database lifetime ownership is no longer valid.",
    "cleanup_incomplete": "Database lifetime ownership cleanup is incomplete.",
    "unsupported_platform": "Database lifetime ownership is unsupported.",
}

_SQLITE_HEADER_BYTES = 100
_COORDINATION_SUFFIX = ".wahojobs-lifetime.lock"
_PATH_TYPE = type(Path())
_WINDOWS_REPARSE_ATTRIBUTE = getattr(
    stat,
    "FILE_ATTRIBUTE_REPARSE_POINT",
    0x400,
)
_ERROR_CAPABILITY = object()
_LEASE_CAPABILITY = object()


class DatabaseLifetimeOwnershipError(Exception):
    """A fixed, redacted ownership failure."""

    __slots__ = ("_category",)

    def __init__(self, capability, category):
        if (
            capability is not _ERROR_CAPABILITY
            or type(category) is not str
            or category not in _ERROR_CATEGORIES
        ):
            category = "unavailable"
        self._category = category
        super().__init__(_ERROR_MESSAGES[category])

    @property
    def category(self):
        return self._category

    def __repr__(self):
        return f"DatabaseLifetimeOwnershipError(category={self._category!r})"


class _ProcessEpoch:
    __slots__ = ("pid", "proof", "token")

    def __init__(self):
        proof = os.urandom(32)
        if type(proof) is not bytes or len(proof) != 32:
            raise _error("unavailable")
        self.pid = os.getpid()
        self.proof = proof
        self.token = object()

    def __repr__(self):
        return "_ProcessEpoch(<sealed>)"


class _FileIdentity:
    __slots__ = (
        "device",
        "inode",
        "mode",
        "links",
        "size",
        "modified_ns",
        "changed_ns",
    )

    def __init__(self, metadata):
        self.device = metadata.st_dev
        self.inode = metadata.st_ino
        self.mode = metadata.st_mode
        self.links = metadata.st_nlink
        self.size = metadata.st_size
        self.modified_ns = getattr(
            metadata,
            "st_mtime_ns",
            int(metadata.st_mtime * 1_000_000_000),
        )
        self.changed_ns = getattr(
            metadata,
            "st_ctime_ns",
            int(metadata.st_ctime * 1_000_000_000),
        )

    @property
    def stable(self):
        return (
            self.device,
            self.inode,
            stat.S_IFMT(self.mode),
            self.links,
            self.size,
            self.modified_ns,
            None if os.name == "nt" else self.changed_ns,
        )

    @property
    def object_identity(self):
        return (
            self.device,
            self.inode,
            stat.S_IFMT(self.mode),
            self.links,
        )


class _WindowsNativeBackend:
    __slots__ = ("_native",)

    def __init__(self, native):
        if native is None:
            raise _error("unsupported_platform")
        self._native = native

    def acquire(self, descriptor):
        os.lseek(descriptor, 0, os.SEEK_SET)
        self._native.locking(descriptor, self._native.LK_NBLCK, 1)

    def release(self, descriptor):
        os.lseek(descriptor, 0, os.SEEK_SET)
        self._native.locking(descriptor, self._native.LK_UNLCK, 1)


class _PosixNativeBackend:
    __slots__ = ("_native",)

    def __init__(self, native):
        if native is None:
            raise _error("unsupported_platform")
        self._native = native

    def acquire(self, descriptor):
        self._native.flock(
            descriptor,
            self._native.LOCK_EX | self._native.LOCK_NB,
        )

    def release(self, descriptor):
        self._native.flock(descriptor, self._native.LOCK_UN)


class _OwnershipRecord:
    __slots__ = (
        "backend",
        "coordination_identity",
        "coordination_path",
        "database_identity",
        "database_path",
        "descriptor",
        "epoch",
        "acquisition_failed",
        "key",
        "lease",
        "lease_token",
        "native_locked",
        "role",
        "state",
    )

    def __init__(
        self,
        *,
        epoch,
        key,
        role,
        database_path,
        database_identity,
        coordination_path,
        backend,
    ):
        self.epoch = epoch
        self.key = key
        self.role = role
        self.database_path = database_path
        self.database_identity = database_identity
        self.coordination_path = coordination_path
        self.coordination_identity = None
        self.backend = backend
        self.descriptor = None
        self.acquisition_failed = True
        self.lease = None
        self.native_locked = False
        self.lease_token = None
        self.state = "opening"


class DatabaseLifetimeOwnership:
    """Opaque authority proving one current-process exclusive ownership."""

    __slots__ = ("__epoch", "__key", "__role", "__state", "__token")

    def __init__(self, capability, *, record, token):
        if (
            capability is not _LEASE_CAPABILITY
            or type(record) is not _OwnershipRecord
            or token is None
        ):
            raise _error("invalid_capability")
        object.__setattr__(self, "_DatabaseLifetimeOwnership__epoch", record.epoch)
        object.__setattr__(self, "_DatabaseLifetimeOwnership__key", record.key)
        object.__setattr__(self, "_DatabaseLifetimeOwnership__role", record.role)
        object.__setattr__(self, "_DatabaseLifetimeOwnership__token", token)
        object.__setattr__(self, "_DatabaseLifetimeOwnership__state", "owned")

    def __repr__(self):
        state = object.__getattribute__(
            self,
            "_DatabaseLifetimeOwnership__state",
        )
        return f"DatabaseLifetimeOwnership(<{state}>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("database_lifetime_ownership_not_serializable")

    def __copy__(self):
        raise TypeError("database_lifetime_ownership_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("database_lifetime_ownership_not_copyable")

    def __setattr__(self, _name, _value):
        raise AttributeError("database_lifetime_ownership_is_immutable")

    def __delattr__(self, _name):
        raise AttributeError("database_lifetime_ownership_is_immutable")


_REGISTRY_LOCK = threading.RLock()
_OWNERS = {}
_PROCESS_EPOCH = None
_AT_FORK_REGISTERED = False


def acquire_database_lifetime_ownership(
    database_path,
    *,
    role,
    _checkpoint=None,
    _publisher=None,
):
    """Acquire one exclusive OS-backed ownership capability or fail closed."""

    if type(role) is not str or role not in _ROLES or (
        _checkpoint is not None and not callable(_checkpoint)
    ) or (
        _publisher is not None and not callable(_publisher)
    ):
        raise _error("invalid_request")
    record = None
    lease = None
    primary = None
    try:
        _ensure_at_fork_registered()
        with _REGISTRY_LOCK:
            epoch = _current_process_epoch_locked()
            path, database_identity = _capture_database_identity(database_path)
            coordination_path = _coordination_path_for_database(path)
            key = _coordination_key(coordination_path)
            existing = _OWNERS.get(key)
            if existing is not None:
                if existing.state == "retired":
                    _mark_record_retired_locked(existing)
                    existing = None
                elif (
                    existing.acquisition_failed
                    or (
                        existing.state.startswith("cleanup_pending")
                        and existing.lease_token is None
                    )
                ):
                    _retry_record_cleanup_locked(existing)
                    if _OWNERS.get(key) is not existing:
                        existing = None
                if existing is not None:
                    raise _error("contention")
            backend = _native_backend()
            record = _OwnershipRecord(
                epoch=epoch,
                key=key,
                role=role,
                database_path=path,
                database_identity=database_identity,
                coordination_path=coordination_path,
                backend=backend,
            )
            _OWNERS[key] = record
            _emit_checkpoint(_checkpoint, "identity_captured")
            descriptor, coordination_identity = _open_coordination_file(
                coordination_path,
                database_parent=path.parent,
            )
            record.descriptor = descriptor
            record.coordination_identity = coordination_identity
            record.state = "coordination_open"
            _emit_checkpoint(_checkpoint, "coordination_open")
            try:
                backend.acquire(descriptor.fileno())
            except OSError as exc:
                if _native_contention(exc):
                    raise _error("contention") from None
                raise _error("unavailable") from None
            record.native_locked = True
            record.state = "native_owned"
            _emit_checkpoint(_checkpoint, "native_acquired")
            _revalidate_record_files(record)
            token = object()
            lease = DatabaseLifetimeOwnership(
                _LEASE_CAPABILITY,
                record=record,
                token=token,
            )
            record.lease = lease
            record.lease_token = token
            record.state = "owned"
            if _publisher is not None:
                _publisher(lease)
            _emit_checkpoint(_checkpoint, "published")
            record.acquisition_failed = False
            return lease
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        primary = exc
    except DatabaseLifetimeOwnershipError as exc:
        primary = exc
    except Exception:
        primary = _error("unavailable")
    except BaseException as exc:
        primary = exc

    preserve_arbitrary_base_exception = (
        isinstance(primary, BaseException)
        and not isinstance(
            primary,
            (
                Exception,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ),
        )
    )

    cleanup_complete = True
    if record is not None:
        with _REGISTRY_LOCK:
            record.acquisition_failed = True
            record.state = (
                "cleanup_pending_locked"
                if record.native_locked
                else "cleanup_pending_unlocked"
            )
            cleanup_complete = _retire_failed_acquisition_locked(record, lease)
    lease = None
    record = None
    if not cleanup_complete:
        if preserve_arbitrary_base_exception:
            raise primary from None
        raise _error("cleanup_incomplete") from None
    if isinstance(primary, (KeyboardInterrupt, SystemExit, GeneratorExit)):
        raise primary from None
    if isinstance(primary, DatabaseLifetimeOwnershipError):
        _detach_public_error(primary)
        raise primary from None
    if preserve_arbitrary_base_exception:
        raise primary from None
    raise _error("unavailable") from None


def require_database_lifetime_ownership(
    ownership,
    *,
    role,
    database_path,
):
    """Require authentic, current, unchanged ownership without exposing it."""

    error = None
    try:
        with _REGISTRY_LOCK:
            record = _authentic_record_locked(
                ownership,
                role=role,
                database_path=database_path,
                allow_retired=False,
            )
            if (
                record.state != "owned"
                or not record.native_locked
                or record.descriptor is None
                or record.descriptor.closed
            ):
                raise _error("ownership_lost")
            try:
                _revalidate_record_files(record)
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                record.state = (
                    "lost_locked"
                    if record.native_locked
                    else "lost_unlocked"
                )
                raise
            except BaseException:
                record.state = (
                    "lost_locked"
                    if record.native_locked
                    else "lost_unlocked"
                )
                error = _error("ownership_lost")
            if error is None:
                return True
    except DatabaseLifetimeOwnershipError as exc:
        error = exc
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except Exception:
        error = _error("ownership_lost")
    _detach_public_error(error)
    raise error from None


def _release_database_lifetime_ownership(
    ownership,
    *,
    role,
    database_path,
    _checkpoint=None,
):
    """Authentically release ownership; completed replay is idempotent."""

    if _checkpoint is not None and not callable(_checkpoint):
        raise _error("invalid_request")
    primary = None
    with _REGISTRY_LOCK:
        record = _authentic_record_locked(
            ownership,
            role=role,
            database_path=database_path,
            allow_retired=True,
        )
        if record is None:
            return True
        if record.state == "cleanup_indeterminate":
            raise _error("cleanup_incomplete")
        if record.state not in {
            "owned",
            "cleanup_pending_locked",
            "cleanup_pending_unlocked",
            "lost_locked",
            "lost_unlocked",
        }:
            raise _error("invalid_capability")
        if record.native_locked:
            try:
                _emit_checkpoint(_checkpoint, "before_native_release")
            except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                primary = exc
            except Exception:
                primary = _error("cleanup_incomplete")

        if primary is not None and record.native_locked:
            record.state = "cleanup_pending_locked"
            raise primary from None

        descriptor_closed, close_error = (
            _close_record_descriptor_capturing(record)
        )
        if descriptor_closed:
            record.native_locked = False
            record.state = "cleanup_pending_unlocked"
            if isinstance(
                close_error,
                (KeyboardInterrupt, SystemExit, GeneratorExit),
            ):
                primary = close_error
            if primary is None:
                try:
                    _emit_checkpoint(_checkpoint, "native_released")
                    _emit_checkpoint(_checkpoint, "descriptor_closed")
                    _emit_checkpoint(
                        _checkpoint,
                        "before_lease_retirement",
                    )
                except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                    primary = exc
                except Exception:
                    primary = _error("cleanup_incomplete")
            _retire_record_locked(record, ownership)
            if primary is None:
                try:
                    _emit_checkpoint(_checkpoint, "lease_retired")
                except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                    primary = exc
                except Exception:
                    primary = _error("cleanup_incomplete")
        else:
            if record.state != "cleanup_indeterminate":
                record.state = (
                    "cleanup_pending_locked"
                    if record.native_locked
                    else "cleanup_pending_unlocked"
                )
            if primary is None:
                if isinstance(
                    close_error,
                    (KeyboardInterrupt, SystemExit, GeneratorExit),
                ):
                    primary = close_error
                else:
                    primary = _error("cleanup_incomplete")

    if primary is not None:
        raise primary from None
    return True


def release_database_lifetime_ownership(
    ownership,
    *,
    role,
    database_path,
    _checkpoint=None,
):
    """Release through one authentic capability with fixed public errors."""

    error = None
    try:
        return _release_database_lifetime_ownership(
            ownership,
            role=role,
            database_path=database_path,
            _checkpoint=_checkpoint,
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except DatabaseLifetimeOwnershipError as exc:
        error = exc
    except Exception:
        error = _error("cleanup_incomplete")
    _detach_public_error(error)
    raise error from None


def database_lifetime_ownership_is_released(ownership):
    """Return only terminal state for cleanup coordination probes."""

    if type(ownership) is not DatabaseLifetimeOwnership:
        return False
    try:
        state = object.__getattribute__(
            ownership,
            "_DatabaseLifetimeOwnership__state",
        )
        return state == "retired"
    except BaseException:
        return False


def _authentic_record_locked(
    ownership,
    *,
    role,
    database_path,
    allow_retired,
):
    if (
        type(ownership) is not DatabaseLifetimeOwnership
        or type(role) is not str
        or role not in _ROLES
        or type(allow_retired) is not bool
    ):
        raise _error("invalid_capability")
    try:
        epoch = object.__getattribute__(
            ownership,
            "_DatabaseLifetimeOwnership__epoch",
        )
        key = object.__getattribute__(
            ownership,
            "_DatabaseLifetimeOwnership__key",
        )
        owned_role = object.__getattribute__(
            ownership,
            "_DatabaseLifetimeOwnership__role",
        )
        token = object.__getattribute__(
            ownership,
            "_DatabaseLifetimeOwnership__token",
        )
        state = object.__getattribute__(
            ownership,
            "_DatabaseLifetimeOwnership__state",
        )
    except BaseException:
        raise _error("invalid_capability") from None
    current = _current_process_epoch_locked()
    if epoch is not current or epoch.pid != os.getpid() or owned_role != role:
        raise _error("invalid_capability")
    path = _validated_path_argument(database_path, require_exists=False)
    if key != _coordination_key(_coordination_path_for_database(path)):
        raise _error("invalid_capability")
    if state == "retired":
        if allow_retired:
            return None
        raise _error("ownership_lost")
    record = _OWNERS.get(key)
    if (
        type(record) is not _OwnershipRecord
        or record.epoch is not epoch
        or record.lease is not ownership
        or record.lease_token is not token
        or record.role != role
        or record.database_path != path
    ):
        raise _error("invalid_capability")
    if record.state == "retired":
        _mark_record_retired_locked(record)
        if allow_retired:
            return None
        raise _error("ownership_lost")
    return record


def _capture_database_identity(database_path):
    path = _validated_path_argument(database_path, require_exists=True)
    _require_safe_components(path)
    metadata = os.lstat(path)
    identity = _FileIdentity(metadata)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or _is_reparse(metadata)
        or metadata.st_size < _SQLITE_HEADER_BYTES
    ):
        raise _error("invalid_request")
    raw_descriptor = None
    descriptor = None
    try:
        raw_descriptor = os.open(path, _read_only_flags())
        os.set_inheritable(raw_descriptor, False)
        if os.get_inheritable(raw_descriptor):
            raise _error("unavailable")
        descriptor = io.FileIO(
            raw_descriptor,
            mode="r",
            closefd=True,
        )
        raw_descriptor = None
        opened = _FileIdentity(os.fstat(descriptor.fileno()))
        if opened.stable != identity.stable:
            raise _error("invalid_request")
        header = descriptor.read(_SQLITE_HEADER_BYTES)
        if (
            type(header) is not bytes
            or len(header) != _SQLITE_HEADER_BYTES
            or header[:16] != b"SQLite format 3\x00"
            or header[18:20] != b"\x01\x01"
        ):
            raise _error("invalid_request")
        current = _FileIdentity(os.lstat(path))
        if current.stable != identity.stable:
            raise _error("invalid_request")
        return path, identity
    finally:
        if descriptor is not None:
            try:
                descriptor.close()
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except BaseException:
                raise _error("cleanup_incomplete") from None
        elif raw_descriptor is not None:
            try:
                os.close(raw_descriptor)
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except BaseException:
                raise _error("cleanup_incomplete") from None


def _validated_path_argument(value, *, require_exists):
    if type(require_exists) is not bool or type(value) not in {str, _PATH_TYPE}:
        raise _error("invalid_request")
    path = Path(value)
    if not path.is_absolute():
        raise _error("invalid_request")
    if require_exists:
        resolved = path.resolve(strict=True)
        if resolved != path:
            raise _error("invalid_request")
    else:
        if path != Path(os.path.abspath(os.fspath(path))):
            raise _error("invalid_request")
        resolved = path
    return resolved


def _coordination_path_for_database(database_path):
    return database_path.with_name(database_path.name + _COORDINATION_SUFFIX)


def _coordination_key(coordination_path):
    return os.path.normcase(os.fspath(coordination_path))


def _open_coordination_file(coordination_path, *, database_parent):
    _require_safe_components(database_parent)
    parent_before = _FileIdentity(os.lstat(database_parent))
    if not stat.S_ISDIR(parent_before.mode):
        raise _error("invalid_request")
    raw_descriptor = None
    descriptor = None
    created = False
    try:
        for _attempt in range(4):
            try:
                raw_descriptor = os.open(
                    coordination_path,
                    _coordination_flags(create=True),
                    0o600,
                )
                created = True
                break
            except FileExistsError:
                try:
                    existing = os.lstat(coordination_path)
                except FileNotFoundError:
                    continue
                _require_safe_coordination_metadata(existing)
                try:
                    raw_descriptor = os.open(
                        coordination_path,
                        _coordination_flags(create=False),
                    )
                    break
                except FileNotFoundError:
                    continue
        if raw_descriptor is None:
            raise _error("unavailable")
        os.set_inheritable(raw_descriptor, False)
        if os.get_inheritable(raw_descriptor):
            raise _error("unavailable")
        if os.name == "posix" and created:
            os.fchmod(raw_descriptor, 0o600)
        descriptor = io.FileIO(
            raw_descriptor,
            mode="r+",
            closefd=True,
        )
        raw_descriptor = None
        opened = os.fstat(descriptor.fileno())
        _require_safe_coordination_metadata(opened)
        if os.name == "posix":
            if opened.st_uid != os.geteuid() or opened.st_mode & (
                stat.S_IRWXG | stat.S_IRWXO
            ):
                raise _error("invalid_request")
        current = os.lstat(coordination_path)
        _require_safe_coordination_metadata(current)
        opened_identity = _FileIdentity(opened)
        current_identity = _FileIdentity(current)
        parent_after = _FileIdentity(os.lstat(database_parent))
        if (
            (opened_identity.device, opened_identity.inode)
            != (current_identity.device, current_identity.inode)
            or (
                parent_after.device,
                parent_after.inode,
                stat.S_IFMT(parent_after.mode),
            )
            != (
                parent_before.device,
                parent_before.inode,
                stat.S_IFMT(parent_before.mode),
            )
        ):
            raise _error("invalid_request")
        return descriptor, opened_identity
    except BaseException:
        if descriptor is not None:
            try:
                descriptor.close()
            except BaseException:
                pass
        elif raw_descriptor is not None:
            try:
                os.close(raw_descriptor)
            except BaseException:
                pass
        raise


def _require_safe_coordination_metadata(metadata):
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size != 0
        or _is_reparse(metadata)
    ):
        raise _error("invalid_request")


def _revalidate_record_files(record):
    if (
        type(record) is not _OwnershipRecord
        or record.descriptor is None
        or record.descriptor.closed
        or os.get_inheritable(record.descriptor.fileno())
    ):
        raise _error("ownership_lost")
    _require_safe_components(record.database_path)
    current_database = _FileIdentity(os.lstat(record.database_path))
    if (
        current_database.object_identity
        != record.database_identity.object_identity
    ):
        raise _error("ownership_lost")
    _require_safe_components(record.coordination_path.parent)
    opened = _FileIdentity(os.fstat(record.descriptor.fileno()))
    current = os.lstat(record.coordination_path)
    _require_safe_coordination_metadata(current)
    current_identity = _FileIdentity(current)
    if (
        opened.stable != record.coordination_identity.stable
        or (current_identity.device, current_identity.inode)
        != (
            record.coordination_identity.device,
            record.coordination_identity.inode,
        )
    ):
        raise _error("ownership_lost")


def _require_safe_components(path):
    current = path
    while True:
        metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise _error("invalid_request")
        if os.name == "posix" and stat.S_ISDIR(metadata.st_mode):
            owner = metadata.st_uid
            effective_user = os.geteuid()
            writable_by_others = bool(
                metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            )
            protected_shared_directory = bool(
                metadata.st_mode & stat.S_ISVTX
            )
            if owner not in {0, effective_user} or (
                writable_by_others and not protected_shared_directory
            ):
                raise _error("invalid_request")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _is_reparse(metadata):
    return bool(
        getattr(metadata, "st_file_attributes", 0)
        & _WINDOWS_REPARSE_ATTRIBUTE
    )


def _native_backend():
    if os.name == "nt":
        try:
            import msvcrt
        except ImportError:
            raise _error("unsupported_platform") from None
        return _WindowsNativeBackend(msvcrt)
    if os.name == "posix":
        try:
            import fcntl
        except ImportError:
            raise _error("unsupported_platform") from None
        return _PosixNativeBackend(fcntl)
    raise _error("unsupported_platform")


def _native_contention(exc):
    return isinstance(exc, OSError) and exc.errno in {
        errno.EACCES,
        errno.EAGAIN,
        errno.EDEADLK,
    }


def _read_only_flags():
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _coordination_flags(*, create):
    flags = os.O_RDWR
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    return flags


def _retire_failed_acquisition_locked(record, lease):
    if _OWNERS.get(record.key) is not record:
        return True
    complete = _cleanup_record_locked(record)
    if complete:
        _mark_record_retired_locked(record)
    return complete


def _retry_record_cleanup_locked(record):
    if record.state == "cleanup_indeterminate":
        return False
    if (
        not record.acquisition_failed
        and record.state not in {
            "cleanup_pending_locked",
            "cleanup_pending_unlocked",
        }
    ):
        return False
    complete = _cleanup_record_locked(record)
    if complete:
        _mark_record_retired_locked(record)
    return complete


def _cleanup_record_locked(record):
    if record.state == "cleanup_indeterminate":
        return False
    if not _close_record_descriptor(record):
        if record.state != "cleanup_indeterminate":
            record.state = (
                "cleanup_pending_locked"
                if record.native_locked
                else "cleanup_pending_unlocked"
            )
        return False
    record.native_locked = False
    record.state = "retired"
    return True


def _close_record_descriptor(record):
    closed, _error_value = _close_record_descriptor_capturing(record)
    return closed


def _close_record_descriptor_capturing(record):
    descriptor = record.descriptor
    if descriptor is None:
        return True, None
    close_error = None
    try:
        _close_coordination_descriptor(descriptor)
    except BaseException as exc:
        close_error = exc
    try:
        closed = descriptor.closed is True
    except BaseException:
        closed = False
    proven_closed = closed and (
        close_error is None
        or isinstance(
            close_error,
            (KeyboardInterrupt, SystemExit, GeneratorExit),
        )
    )
    if proven_closed:
        record.descriptor = None
    elif closed and close_error is not None:
        record.state = "cleanup_indeterminate"
    return proven_closed, close_error


def _close_coordination_descriptor(descriptor):
    if type(descriptor) is not io.FileIO:
        raise _error("cleanup_incomplete")
    descriptor.close()
    return descriptor.closed is True


def _retire_record_locked(record, ownership):
    if record.lease is not ownership:
        raise _error("invalid_capability")
    _mark_record_retired_locked(record)


def _mark_record_retired_locked(record):
    record.state = "retired"
    record.lease_token = None
    lease = record.lease
    if type(lease) is DatabaseLifetimeOwnership:
        object.__setattr__(
            lease,
            "_DatabaseLifetimeOwnership__state",
            "retired",
        )
    record.lease = None
    if _OWNERS.get(record.key) is record:
        _OWNERS.pop(record.key, None)


def _emit_checkpoint(callback, name):
    if callback is not None:
        callback(name)


def _error(category):
    return DatabaseLifetimeOwnershipError(_ERROR_CAPABILITY, category)


def _detach_public_error(error):
    if type(error) is not DatabaseLifetimeOwnershipError:
        return False
    try:
        error.args = (_ERROR_MESSAGES[error.category],)
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        error.__suppress_context__ = True
    except BaseException:
        return False
    return True


def _current_process_epoch_locked():
    global _PROCESS_EPOCH
    current = _PROCESS_EPOCH
    if (
        type(current) is _ProcessEpoch
        and current.pid == os.getpid()
    ):
        return current
    _PROCESS_EPOCH = _ProcessEpoch()
    return _PROCESS_EPOCH


def _ensure_at_fork_registered():
    global _AT_FORK_REGISTERED
    if not hasattr(os, "register_at_fork"):
        return
    with _REGISTRY_LOCK:
        if _AT_FORK_REGISTERED:
            return
        os.register_at_fork(
            before=_before_fork,
            after_in_parent=_after_fork_parent,
            after_in_child=_after_fork_child,
        )
        _AT_FORK_REGISTERED = True


def _before_fork():
    _REGISTRY_LOCK.acquire()


def _after_fork_parent():
    _REGISTRY_LOCK.release()


def _after_fork_child():
    global _REGISTRY_LOCK
    global _OWNERS
    global _PROCESS_EPOCH
    inherited_cleanup = {}
    for record in tuple(_OWNERS.values()):
        if _close_record_descriptor(record):
            record.native_locked = False
            record.state = "inherited_invalid"
        else:
            record.acquisition_failed = True
            if record.state != "cleanup_indeterminate":
                record.state = "cleanup_pending_locked"
            inherited_cleanup[record.key] = record
    _OWNERS = inherited_cleanup
    _PROCESS_EPOCH = None
    _REGISTRY_LOCK = threading.RLock()


__all__ = (
    "DatabaseLifetimeOwnership",
    "DatabaseLifetimeOwnershipError",
    "ROLE_DURABLE_RUNTIME",
    "ROLE_OFFLINE_OPERATOR",
    "acquire_database_lifetime_ownership",
    "database_lifetime_ownership_is_released",
    "release_database_lifetime_ownership",
    "require_database_lifetime_ownership",
)
