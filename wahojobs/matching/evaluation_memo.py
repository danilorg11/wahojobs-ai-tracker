"""Bounded memoization within one matching evaluation, never between owners."""
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps

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
