"""Bounded, local request diagnostics with no request content or owner identity."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
import json
import logging
from pathlib import Path
import stat
import threading


LOGIN_START_OUTCOMES = frozenset({
    'login_form_rejected', 'login_cookie_header_absent',
    'login_cookie_headers_multiple', 'login_cookie_size_rejected',
    'login_cookie_pairs_rejected', 'login_cookie_segment_rejected',
    'login_cookie_target_absent', 'login_cookie_target_duplicate',
    'login_cookie_target_invalid', 'login_csrf_mismatch',
    'login_invitation_shape_rejected', 'login_prepare_unavailable',
    'login_authorization_prepared',
})


def login_start_outcome(response, status):
    """Read optional closed metadata; diagnostics cannot change delivery."""
    try:
        value = getattr(response, 'login_start_outcome', None)
        if type(value) is str and value in LOGIN_START_OUTCOMES:
            expected = 303 if value == 'login_authorization_prepared' else 403
            if status == expected:
                return value
    except BaseException:
        # A faulty optional observer/property must not reject a valid response.
        pass
    return None


def route_category(target: str) -> str:
    # Never retain a query, posting/profile identifier, callback code, or arbitrary path.
    path = target.split('?', 1)[0]
    if path in ('/login', '/logout'):
        return 'session'
    if path.startswith('/auth/'):
        return 'authentication'
    if path == '/find-matches':
        return 'matches'
    if path.startswith('/job/'):
        return 'opportunity'
    if path.startswith('/tracker'):
        return 'my_jobs'
    if path.startswith('/account/profile') or path.startswith('/profile'):
        return 'profile'
    return 'other'


@dataclass(frozen=True, slots=True)
class RequestDiagnostic:
    request_id: str
    method: str
    route: str
    status: int
    outcome: str
    elapsed_ms: int
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec='milliseconds'))


class RequestDiagnostics:
    """Private bounded ring; optional operator-configured rotating local log.

    The handler supplies only closed labels, numbers and a generated correlation ID.
    No HTTP endpoint publishes this ring. Logging failures cannot change delivery.
    """
    def __init__(self, *, capacity=200, logger=None):
        if type(capacity) is not int or not 1 <= capacity <= 1000:
            raise ValueError('invalid_diagnostic_capacity')
        self._records = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._logger = logger

    def record(self, diagnostic):
        if type(diagnostic) is not RequestDiagnostic:
            raise ValueError('invalid_request_diagnostic')
        with self._lock:
            self._records.append(diagnostic)
        if self._logger is not None:
            self._logger.info(json.dumps(asdict(diagnostic), separators=(',', ':')))

    def snapshot(self):
        with self._lock:
            return tuple(self._records)


@contextmanager
def diagnostic_log(directory):
    """Create a new diagnostic log in an explicit existing operator directory.

    At most three 256 KiB files. Refuse a preexisting log family so symlinks,
    unrelated files and another running writer cannot be silently reused.
    """
    path = Path(directory)
    if not path.is_absolute() or path.resolve() != path or not path.is_dir():
        raise ValueError('invalid_diagnostic_directory')
    if any(p.is_symlink() or getattr(p.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
           for p in (path, *path.parents)):
        raise ValueError('invalid_diagnostic_directory')
    log_path = path/'requests.jsonl'
    if any((path/name).exists() for name in ('requests.jsonl','requests.jsonl.1','requests.jsonl.2')):
        raise ValueError('diagnostic_log_already_exists')
    with log_path.open('x', encoding='utf-8'):
        pass
    handler = RotatingFileHandler(log_path, maxBytes=262144, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger = logging.Logger('wahojobs.request', level=logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield RequestDiagnostics(logger=logger)
    finally:
        handler.close()
