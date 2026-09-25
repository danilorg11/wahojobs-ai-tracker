"""Bounded memoization within one matching evaluation, never between owners."""
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps

_current = ContextVar('matching_evaluation_memo', default=None)


def evaluation_scope(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _current.set({})
        try:
            return function(*args, **kwargs)
        finally:
            _current.reset(token)
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
