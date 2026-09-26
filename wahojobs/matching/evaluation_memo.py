"""Bounded memoization within one matching evaluation, never between owners."""
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import json
import marshal

_current = ContextVar('matching_evaluation_memo', default=None)
_profile = ContextVar('matching_evaluation_profile', default=None)


def evaluation_scope(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _current.set({})
        profile_token = _profile.set(args[0] if args else kwargs.get('profile'))
        try:
            return function(*args, **kwargs)
        finally:
            _profile.reset(profile_token)
            _current.reset(token)
    return wrapped


def memoized_profile(function):
    """Pure projections of the unchanged profile within one evaluation only.

    No identity cache survives the scope; standalone calls and other profiles
    execute normally. Callers must not mutate the evaluation's profile.
    """
    @wraps(function)
    def wrapped(profile, *args, **kwargs):
        scope = _current.get()
        if kwargs or scope is None or profile is not _profile.get():
            return function(profile, *args, **kwargs)
        cache = scope.setdefault(function, {})
        if args not in cache:
            result = function(profile, *args)
            if len(cache) >= 128:
                return result
            cache[args] = result
        value = cache[args]
        return set(value) if isinstance(value, set) else value
    return wrapped


def memoized_text(function):
    """Only pure positional text predicates/parsers use this decorator."""
    @wraps(function)
    def wrapped(*args):
        scope = _current.get()
        if scope is None:
            return function(*args)
        key = tuple(frozenset(value) if isinstance(value, set) else value for value in args)
        cache = scope.setdefault(function, {})
        if key not in cache:
            result = function(*args)
            if len(cache) >= 8192:
                return result
            cache[key] = result
        result = cache[key]
        # Locale parsers return nested structures; no caller owns cached values.
        return deepcopy(result) if isinstance(result, (list, dict)) else result
    return wrapped


class VariantEvaluationMemo:
    """Bounded equivalent-input comparisons within exactly one evaluation.

    No canonical grouping or candidate pruning happens here. Task evidence is
    identity-validated for every row; trust and accepted eligibility run after
    this boundary for every row. The cache holds no source-reference receipt.
    Unknown input types bypass reuse instead of weakening the equality key.
    """

    MAX_ENTRIES = 1024
    MAX_BYTES = 16 * 1024 * 1024
    MAX_ITEM_BYTES = 65536

    def __init__(self, profile):
        self._profile = profile
        self._cache = {}
        self._bytes = 0

    def evaluate(self, profile, row, score, guardrails, **kwargs):
        from wahojobs.matching.accepted_tasks import matched_accepted_tasks
        task_fit = matched_accepted_tasks(profile, row)
        confirmed = matched_accepted_tasks(profile, row, include_transferable=False)
        # These are the only fields read outside the semantic boundary. All
        # other row fields, including future metadata, participate in equality.
        semantic_row = {k: v for k, v in row.items() if k not in {
            'job_id', 'url', 'accepted_task_evidence', 'accepted_eligibility_evidence'}}
        semantic_task = ({k: v for k, v in task_fit.items() if k != 'source_reference'}
                         if task_fit else task_fit)
        key = (self._encode([semantic_row, semantic_task, bool(confirmed)])
               if profile is self._profile else None)
        cached = self._cache.get(key) if key is not None else None
        if cached is not None:
            match = marshal.loads(cached)
            match.update(job_id=row['job_id'], url=row['url'], accepted_task_fit=task_fit)
            return match
        match = guardrails(profile, row, score(profile, row, task_fit=task_fit,
                           confirmed_task_fit=confirmed), **kwargs)
        if key is not None and len(self._cache) < self.MAX_ENTRIES:
            template = {k: v for k, v in match.items() if k not in {
                'job_id', 'url', 'accepted_task_fit'}}
            # An in-process copy of our own built-in projection tree, never a
            # file or caller-provided serialized payload. Tuple/list shapes are
            # preserved and each hit owns all of its mutable containers.
            try:
                encoded = marshal.dumps(template)
            except (TypeError, ValueError):
                encoded = None
            size = len(key) + len(encoded) if encoded is not None else self.MAX_BYTES + 1
            if size <= self.MAX_ITEM_BYTES and self._bytes + size <= self.MAX_BYTES:
                self._cache[key] = encoded
                self._bytes += size
        return match

    @staticmethod
    def _encode(value):
        def supported(item):
            if type(item) in (str, int, bool, float, type(None)):
                return True
            if type(item) is list:
                return all(supported(v) for v in item)
            if type(item) is dict:
                return all(type(k) is str and supported(v) for k, v in item.items())
            return False
        if not supported(value):
            return None
        try:
            result = json.dumps(value, sort_keys=True, ensure_ascii=False,
                                allow_nan=False, separators=(',', ':')).encode('utf-8')
        except (ValueError, TypeError):
            return None
        return result if len(result) <= VariantEvaluationMemo.MAX_ITEM_BYTES else None
