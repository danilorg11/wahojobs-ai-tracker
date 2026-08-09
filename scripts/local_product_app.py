import argparse
from copy import deepcopy
import hashlib
import html
import json
import math
import re
import secrets
import sqlite3
import sys
import threading
import time
import types
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote_to_bytes, urlencode, urlparse, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import product_demo_report as demo
import profile_to_matches_preview as profile_preview
import product_state
import user_pipeline_digest as pipeline_digest
from wahojobs import (
    browser_session_authentication as browser_session_security,
    pipeline_actions,
    pipeline_reconciliation,
    pipeline_records,
    pipeline_state,
    persistent_profiles_browser as persistent_profile_browser_security,
)
from wahojobs.db.connection import get_connection
from wahojobs.profiles.canonical import (
    PROFILE_SOURCE_PARSED_TEXT,
    PROFILE_SOURCE_USER_CONFIRMATION,
    PROFILE_SOURCE_USER_CORRECTION,
    SCHEMA_VERSION,
    UNKNOWN,
    field_sources_for_profile,
    unique_strings,
    validate_canonical_profile,
)
from wahojobs.profiles import normalizer as profile_normalizer
from wahojobs.profiles import review as canonical_review
from wahojobs.profiles.countries import normalize_country
from wahojobs.profiles.review import (
    CREDENTIAL_STATUSES,
    EDUCATION_LEVELS,
    LANGUAGE_PROFICIENCIES,
)
from wahojobs.persistent_profiles import IdentityFreeCanonicalProfileV1


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
NORMAL_OWNER_PROFILE_ID = "local_user"
PREVIEW_MATCH_LIMIT = 160
MATCH_RUN_REGISTRY_LIMIT = 64
PRESENTATION_MATCH_LIMIT = 10
RECENT_CACHED_MATCH_MAX_AGE_HOURS = 7 * 24
ACTIONABLE_PRESENTATION_SECTIONS = (
    "do_these_first",
    "best_matches",
    "also_worth_reviewing",
)
INLINE_ACTION_HEADER = "X-Wahojobs-Inline-Action"
LOCAL_PRODUCT_PROFILE_SEED_PATH = (
    Path(__file__).resolve().parent.parent / "profiles" / "local_product_profiles.json"
)
FIND_MATCHES_PATHS = {"/find-matches", "/preview"}
PROFILE_CONFIRMATION_PATH = "/find-matches"
MAX_PROFILE_CONFIRMATION_BODY_BYTES = 65_536
MAX_PROFILE_CONFIRMATION_FIELDS = 128
PROFILE_CONFIRMATION_OWNER_WAIT_SECONDS = 1.0
PROFILE_CONFIRMATION_RETENTION_SECONDS = 600.0
TRACKER_PATHS = {"/", "/tracker"}
HEAVY_DASHBOARD_PATHS = {"/dashboard", "/market-dashboard"}
_HTTP_METHOD_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_BROWSER_HEADER_VALUE_FORBIDDEN = re.compile(
    r"[\x00-\x08\x0a-\x1f\x7f]"
)
_INVALID_FORM_PERCENT_ESCAPE = re.compile(rb"%(?![0-9A-Fa-f]{2})")

PREVIEW_SAMPLES = {
    "beginner_bilingual": {
        "label": "Beginner bilingual",
        "owner_profile_id": "beginner_bilingual_no_degree",
        "style": "short_paragraph",
        "text": (
            "I speak English and Spanish, no college degree, looking for remote beginner "
            "AI data tasks with no phone calls."
        ),
    },
    "software_engineer": {
        "label": "Software engineer",
        "owner_profile_id": "software_engineer",
        "style": "resume_or_linkedin_style",
        "text": (
            "Senior Software Engineer with 8 years of Python, TypeScript, React, API, "
            "and data platform experience. Interested in remote AI coding, evaluation, "
            "and software review work. No biology or medical credentials."
        ),
    },
    "biology_academic": {
        "label": "Biology academic",
        "owner_profile_id": "biology_or_medicine_academic",
        "style": "resume_or_linkedin_style",
        "text": (
            "PhD biology researcher with computational biology, genomics, microbiology, "
            "and academic research experience. Looking for remote AI evaluation and "
            "biology expert review work. No medical license."
        ),
    },
}


@dataclass(frozen=True)
class MatchRun:
    match_run_id: str
    owner_profile_id: str
    raw_input: str
    input_style: str
    demo_persona: str | None
    recommendation_context: dict | None
    created_at: datetime
    last_accessed_at: datetime
    canonical_profile: IdentityFreeCanonicalProfileV1 | None = None
    profile_confirmed: bool = False
    review_token: str = ""


@dataclass(frozen=True, repr=False)
class ConfirmedProfileCreation:
    match_run: MatchRun
    artifact_offer: object
    _authority_binding: tuple = field(repr=False)

    def __post_init__(self):
        if (
            type(self.match_run) is not MatchRun
            or not _is_confirmed_profile_artifact_offer(self.artifact_offer)
            or type(self._authority_binding) is not tuple
            or len(self._authority_binding) != 10
        ):
            raise ValueError("invalid_confirmed_profile_creation")

    def __repr__(self):
        return "ConfirmedProfileCreation(<redacted>)"


@dataclass(frozen=True, repr=False)
class _ProfileConfirmationLease:
    registry: object
    match_run_id: str
    owner: object
    confirmation_identity: str
    reviewed_run: MatchRun
    recovery_only: bool

    def __repr__(self):
        return "_ProfileConfirmationLease(<redacted>)"


@dataclass(frozen=True, repr=False)
class _CompletedProfileConfirmationReplay:
    registry: object
    match_run_id: str
    completed_result: ConfirmedProfileCreation
    authority_binding: tuple = field(repr=False)

    def __repr__(self):
        return "_CompletedProfileConfirmationReplay(<redacted>)"


@dataclass(repr=False)
class _ProfileConfirmationState:
    original_run: MatchRun
    reviewed_run: MatchRun
    original_draft_digest: str
    reviewed_request_digest: str
    confirmation_identity: str
    state: str
    owner: object | None
    retention_deadline: float | None = None
    recovery_only: bool = False
    completed_result: ConfirmedProfileCreation | None = None

    def __repr__(self):
        return f"_ProfileConfirmationState(state={self.state!r})"


class _ConfirmationIssuanceWitness:
    """Monotone, process-private evidence shared with the trusted artifact sink."""

    __slots__ = ("_authority_binding", "_lease", "_lock", "_may_exist", "_offer")

    def __init__(self, lease):
        if type(lease) is not _ProfileConfirmationLease:
            raise ValueError("invalid_profile_confirmation_lease")
        self._lease = lease
        self._lock = threading.Lock()
        self._may_exist = lease.recovery_only
        self._offer = None
        self._authority_binding = None

    def record_authority_binding(self, binding):
        if type(binding) is not tuple or len(binding) != 10:
            raise ValueError("invalid_profile_confirmation_authority_binding")
        with self._lock:
            if self._authority_binding is None:
                self._authority_binding = binding
            elif self._authority_binding != binding:
                raise ValueError("conflicting_profile_confirmation_authority_binding")
            return self._authority_binding

    def mark_artifact_may_exist(self):
        with self._lock:
            if self._authority_binding is None:
                raise ValueError("missing_profile_confirmation_authority_binding")
            self._may_exist = True

    def mark_artifact_definitely_absent(self):
        with self._lock:
            if self._offer is not None:
                raise ValueError("confirmed_profile_artifact_already_exists")
            self._may_exist = False

    def record_valid_offer(self, offer):
        if not _is_confirmed_profile_artifact_offer(offer):
            raise ValueError("invalid_confirmed_profile_creation")
        with self._lock:
            self._may_exist = True
            if self._offer is None:
                self._offer = offer
            elif self._offer != offer:
                raise ValueError("conflicting_confirmed_profile_artifact_offer")
            recorded = self._offer
            binding = self._authority_binding
        return self._lease.registry.complete_profile_confirmation(
            self._lease,
            recorded,
            binding,
        )

    @property
    def artifact_may_exist(self):
        with self._lock:
            return self._may_exist

    @property
    def valid_offer(self):
        with self._lock:
            return self._offer

    def __repr__(self):
        return "_ConfirmationIssuanceWitness(<redacted>)"


class MatchRunRegistry:
    """Bounded process-local run storage for the local prototype."""

    def __init__(
        self,
        max_size=MATCH_RUN_REGISTRY_LIMIT,
        *,
        absolute_ttl_seconds=None,
        _retention_clock=time.monotonic,
    ):
        if max_size < 1:
            raise ValueError("MatchRun registry max_size must be positive.")
        if not callable(_retention_clock):
            raise ValueError("MatchRun registry retention clock must be callable.")
        if (
            absolute_ttl_seconds is not None
            and (
                type(absolute_ttl_seconds) not in (float, int)
                or not math.isfinite(absolute_ttl_seconds)
                or absolute_ttl_seconds <= 0
            )
        ):
            raise ValueError("MatchRun registry absolute TTL must be positive.")
        self.max_size = max_size
        self._absolute_ttl_seconds = absolute_ttl_seconds
        self._retention_clock = _retention_clock
        self._runs = OrderedDict()
        self._run_deadlines = {}
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._confirmations = {}

    def create(
        self,
        owner_profile_id,
        raw_input,
        input_style,
        demo_persona=None,
        recommendation_context=None,
        canonical_profile=None,
        profile_confirmed=False,
    ):
        if canonical_profile is not None and type(canonical_profile) is not IdentityFreeCanonicalProfileV1:
            raise ValueError("invalid_identity_free_profile_draft")
        now = datetime.now(timezone.utc)
        run = MatchRun(
            match_run_id=secrets.token_urlsafe(18),
            owner_profile_id=owner_profile_id,
            raw_input=raw_input,
            input_style=input_style,
            demo_persona=demo_persona,
            recommendation_context=recommendation_context,
            created_at=now,
            last_accessed_at=now,
            canonical_profile=canonical_profile,
            profile_confirmed=profile_confirmed,
            review_token=secrets.token_urlsafe(32),
        )
        with self._condition:
            self._purge_expired_confirmation_results_locked()
            self._runs[run.match_run_id] = run
            if self._absolute_ttl_seconds is not None:
                self._run_deadlines[run.match_run_id] = (
                    self._retention_now_locked() + self._absolute_ttl_seconds
                )
            self._runs.move_to_end(run.match_run_id)
            while len(self._runs) > self.max_size:
                candidate = next(
                    (
                        run_id
                        for run_id in self._runs
                        if run_id != run.match_run_id
                        and not self._confirmation_pinned_locked(run_id)
                    ),
                    None,
                )
                if candidate is None:
                    del self._runs[run.match_run_id]
                    self._run_deadlines.pop(run.match_run_id, None)
                    raise ActionError(
                        "Profile review is temporarily unavailable.",
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                del self._runs[candidate]
                self._run_deadlines.pop(candidate, None)
                self._confirmations.pop(candidate, None)
        return run

    def confirm_profile(self, match_run_id, canonical_profile, recommendation_context):
        if type(canonical_profile) is not IdentityFreeCanonicalProfileV1:
            raise ValueError("invalid_identity_free_profile_draft")
        with self._condition:
            run = self._runs.get(match_run_id)
            if run is None:
                return None
            run = replace(
                run,
                canonical_profile=canonical_profile,
                profile_confirmed=True,
                recommendation_context=recommendation_context,
                last_accessed_at=datetime.now(timezone.utc),
            )
            self._runs[match_run_id] = run
            self._runs.move_to_end(match_run_id)
            return run

    def confirmation_draft(self, match_run_id):
        with self._condition:
            self._purge_expired_confirmation_results_locked()
            run = self._runs.get(match_run_id)
            if run is None:
                return None
            state = self._confirmations.get(match_run_id)
            draft = state.original_run if state is not None else run
            self._runs.move_to_end(match_run_id)
            return draft

    def acquire_profile_confirmation(
        self,
        *,
        original_run,
        original_draft_digest,
        reviewed_request_digest,
        confirmation_identity,
        reviewed_profile,
        acquisition,
    ):
        if (
            type(original_run) is not MatchRun
            or type(reviewed_profile) is not IdentityFreeCanonicalProfileV1
            or not _is_sha256_digest(original_draft_digest)
            or not _is_sha256_digest(reviewed_request_digest)
            or not _is_sha256_digest(confirmation_identity)
            or type(acquisition) is not list
            or acquisition
        ):
            raise ValueError("invalid_profile_confirmation_claim")
        deadline = time.monotonic() + PROFILE_CONFIRMATION_OWNER_WAIT_SECONDS
        with self._condition:
            while True:
                self._purge_expired_confirmation_results_locked()
                state = self._confirmations.get(original_run.match_run_id)
                if state is not None:
                    if not self._confirmation_matches_locked(
                        state,
                        original_run,
                        original_draft_digest,
                        reviewed_request_digest,
                        confirmation_identity,
                    ):
                        raise ActionError(
                            "This profile draft is no longer current.",
                            HTTPStatus.FORBIDDEN,
                        )
                    if state.state == "completed":
                        completed = state.completed_result
                        if type(completed) is not ConfirmedProfileCreation:
                            raise RuntimeError("invalid_profile_confirmation_state")
                        binding = completed._authority_binding
                        if type(binding) is not tuple or len(binding) != 10:
                            raise RuntimeError("invalid_profile_confirmation_state")
                        return _CompletedProfileConfirmationReplay(
                            self,
                            original_run.match_run_id,
                            completed,
                            binding,
                        )
                    if state.state == "maybe_issued":
                        owner = object()
                        lease = _ProfileConfirmationLease(
                            self,
                            original_run.match_run_id,
                            owner,
                            confirmation_identity,
                            state.reviewed_run,
                            True,
                        )
                        acquisition.append(lease)
                        state.owner = owner
                        state.recovery_only = True
                        state.state = "issuing"
                        return lease
                    if state.state != "issuing":
                        raise RuntimeError("invalid_profile_confirmation_state")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ActionError(
                            "Profile confirmation is temporarily unavailable.",
                            HTTPStatus.SERVICE_UNAVAILABLE,
                        )
                    self._condition.wait(timeout=remaining)
                    continue

                current = self._runs.get(original_run.match_run_id)
                if (
                    current is None
                    or current.profile_confirmed
                    or current.review_token != original_run.review_token
                    or not secrets.compare_digest(
                        profile_draft_fingerprint(current.canonical_profile),
                        original_draft_digest,
                    )
                ):
                    raise ActionError(
                        "This profile draft is no longer current.",
                        HTTPStatus.FORBIDDEN,
                    )
                reviewed_run = replace(
                    current,
                    canonical_profile=reviewed_profile,
                    profile_confirmed=True,
                    recommendation_context=None,
                    last_accessed_at=datetime.now(timezone.utc),
                )
                owner = object()
                state = _ProfileConfirmationState(
                    original_run=current,
                    reviewed_run=reviewed_run,
                    original_draft_digest=original_draft_digest,
                    reviewed_request_digest=reviewed_request_digest,
                    confirmation_identity=confirmation_identity,
                    state="issuing",
                    owner=owner,
                )
                lease = _ProfileConfirmationLease(
                    self,
                    current.match_run_id,
                    owner,
                    confirmation_identity,
                    reviewed_run,
                    False,
                )
                acquisition.append(lease)
                self._confirmations[current.match_run_id] = state
                return lease

    def complete_profile_confirmation(self, lease, offer, authority_binding):
        if (
            type(lease) is not _ProfileConfirmationLease
            or lease.registry is not self
            or not _is_confirmed_profile_artifact_offer(offer)
            or type(authority_binding) is not tuple
            or len(authority_binding) != 10
        ):
            raise ValueError("invalid_profile_confirmation_completion")
        with self._condition:
            state = self._confirmations.get(lease.match_run_id)
            if state is None or state.confirmation_identity != lease.confirmation_identity:
                raise RuntimeError("profile_confirmation_owner_lost")
            if state.state == "completed":
                if (
                    state.completed_result.artifact_offer != offer
                    or state.completed_result._authority_binding != authority_binding
                ):
                    raise RuntimeError("profile_confirmation_offer_conflict")
                return state.completed_result
            if state.state != "issuing" or state.owner is not lease.owner:
                raise RuntimeError("profile_confirmation_owner_lost")
            result = ConfirmedProfileCreation(
                state.reviewed_run,
                offer,
                authority_binding,
            )
            state.completed_result = result
            if state.retention_deadline is None:
                state.retention_deadline = (
                    self._retention_clock()
                    + PROFILE_CONFIRMATION_RETENTION_SECONDS
                )
            self._runs[lease.match_run_id] = state.reviewed_run
            if self._absolute_ttl_seconds is not None:
                self._run_deadlines[lease.match_run_id] = (
                    self._retention_now_locked() + self._absolute_ttl_seconds
                )
            self._runs.move_to_end(lease.match_run_id)
            state.recovery_only = False
            state.state = "completed"
            state.owner = None
            self._condition.notify_all()
            return result

    def replay_completed_profile_confirmation(self, replay):
        if (
            type(replay) is not _CompletedProfileConfirmationReplay
            or replay.registry is not self
            or type(replay.authority_binding) is not tuple
            or len(replay.authority_binding) != 10
        ):
            raise ValueError("invalid_completed_profile_confirmation_replay")
        with self._condition:
            self._purge_expired_confirmation_results_locked()
            state = self._confirmations.get(replay.match_run_id)
            if (
                state is None
                or state.state != "completed"
                or state.completed_result is not replay.completed_result
                or state.completed_result._authority_binding
                is not replay.authority_binding
            ):
                return None
            return state.completed_result

    def fail_profile_confirmation(self, lease, *, definite_absence):
        if (
            type(lease) is not _ProfileConfirmationLease
            or lease.registry is not self
            or type(definite_absence) is not bool
        ):
            raise ValueError("invalid_profile_confirmation_failure")
        with self._condition:
            state = self._confirmations.get(lease.match_run_id)
            if state is None:
                return None
            if state.state == "completed":
                return state.completed_result
            if state.state != "issuing" or state.owner is not lease.owner:
                return None
            state.owner = None
            state.recovery_only = False
            state.completed_result = None
            self._runs[lease.match_run_id] = state.original_run
            self._runs.move_to_end(lease.match_run_id)
            if definite_absence:
                del self._confirmations[lease.match_run_id]
            else:
                if state.retention_deadline is None:
                    state.retention_deadline = (
                        self._retention_clock()
                        + PROFILE_CONFIRMATION_RETENTION_SECONDS
                    )
                state.state = "maybe_issued"
            self._condition.notify_all()
            return None

    @staticmethod
    def _confirmation_matches_locked(
        state,
        original_run,
        original_draft_digest,
        reviewed_request_digest,
        confirmation_identity,
    ):
        return (
            state.original_run.match_run_id == original_run.match_run_id
            and secrets.compare_digest(
                state.original_run.review_token,
                original_run.review_token,
            )
            and secrets.compare_digest(
                state.original_draft_digest,
                original_draft_digest,
            )
            and secrets.compare_digest(
                state.reviewed_request_digest,
                reviewed_request_digest,
            )
            and secrets.compare_digest(
                state.confirmation_identity,
                confirmation_identity,
            )
        )

    def _confirmation_pinned_locked(self, match_run_id):
        state = self._confirmations.get(match_run_id)
        return state is not None and state.state in {
            "issuing",
            "maybe_issued",
            "completed",
        }

    def _purge_expired_confirmation_results_locked(self):
        now = self._retention_now_locked()
        for match_run_id, state in tuple(self._confirmations.items()):
            if (
                state.state in {"maybe_issued", "completed"}
                and state.retention_deadline is not None
                and now >= state.retention_deadline
            ):
                self._confirmations.pop(match_run_id, None)
                self._runs.pop(match_run_id, None)
                self._run_deadlines.pop(match_run_id, None)
        for match_run_id, deadline in tuple(self._run_deadlines.items()):
            if (
                now >= deadline
                and not self._confirmation_pinned_locked(match_run_id)
            ):
                self._run_deadlines.pop(match_run_id, None)
                self._runs.pop(match_run_id, None)

    def _retention_now_locked(self):
        now = self._retention_clock()
        if type(now) not in (float, int) or not math.isfinite(now):
            raise RuntimeError("invalid_profile_confirmation_retention_clock")
        return now

    def peek(self, match_run_id):
        """Return a run without changing its access time, ordering, or storage."""

        with self._condition:
            run = self._runs.get(match_run_id)
            if run is None:
                return None
            now = self._retention_now_locked()
            deadline = self._run_deadlines.get(match_run_id)
            state = self._confirmations.get(match_run_id)
            if deadline is not None and now >= deadline:
                return None
            if (
                state is not None
                and state.state in {"maybe_issued", "completed"}
                and state.retention_deadline is not None
                and now >= state.retention_deadline
            ):
                return None
            return run

    def get(self, match_run_id):
        with self._condition:
            self._purge_expired_confirmation_results_locked()
            run = self._runs.get(match_run_id)
            if run is None:
                return None
            run = replace(run, last_accessed_at=datetime.now(timezone.utc))
            self._runs[match_run_id] = run
            self._runs.move_to_end(match_run_id)
            return run

    def __len__(self):
        with self._condition:
            self._purge_expired_confirmation_results_locked()
            return len(self._runs)


class ActionError(Exception):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


class MalformedActionRequest(ActionError):
    def __init__(self):
        super().__init__("Malformed action request.", HTTPStatus.BAD_REQUEST)


class MalformedProfileReview(ActionError):
    def __init__(self, message="The profile review form was malformed. Start the review again."):
        super().__init__(message, HTTPStatus.BAD_REQUEST)

ACTION_STATUSES = {
    "show_again": "saved",
    "save": "saved",
    "applied": "applied",
    "assessment_started": "assessment_started",
    "assessment_completed": "assessment_completed",
    "remind_later": "remind_later",
    "not_interested": "not_interested",
    "accepted": "accepted",
    "rejected": "rejected",
}

ACTION_LABELS = {
    "show_again": "Show again",
    "save": "Save",
    "applied": "Mark as applied",
    "assessment_started": "Mark assessment started",
    "assessment_completed": "Mark assessment complete",
    "remind_later": "Remind me in 7 days",
    "not_interested": "Not interested",
    "accepted": "Mark as accepted",
    "rejected": "Mark as not selected",
}

STATUS_LABELS = {
    "recommended": "Recommended",
    "saved": "Saved",
    "remind_later": "Saved",
    "applied": "Applied",
    "waiting": "Waiting for update",
    "assessment_invited": "Assessment ready",
    "assessment_started": "Assessment in progress",
    "assessment_completed": "Waiting for result",
    "accepted": "Accepted",
    "active_worker": "Active",
    "paid_task_received": "Paid task received",
    "not_interested": "Not interested",
    "rejected": "Not selected",
    "expired": "No longer available",
    "workflow_unknown": "Workflow needs confirmation",
}

ACTIVE_PIPELINE_STATUSES = {
    "recommended",
    "saved",
    "remind_later",
    "applied",
    "waiting",
    "assessment_invited",
    "assessment_started",
    "assessment_completed",
}
ACCEPTED_STATUSES = {"accepted", "active_worker", "paid_task_received"}
HIDDEN_STATUSES = {"not_interested"}
CLOSED_STATUSES = {"rejected", "expired"}
MAIN_RECOMMENDATION_EXCLUDED_STATUSES = ACCEPTED_STATUSES | HIDDEN_STATUSES | CLOSED_STATUSES

TRACKER_FILTERS = (
    ("all", "All"),
    ("saved", "Saved"),
    ("in_progress", "In progress"),
    ("active", "Active"),
    ("closed", "Closed"),
)
TRACKER_FILTER_STATUSES = {
    "saved": {"recommended", "saved"},
    "in_progress": {
        "applied",
        "waiting",
        "assessment_invited",
        "assessment_started",
        "assessment_completed",
    },
    "active": ACCEPTED_STATUSES,
    "closed": CLOSED_STATUSES,
    "hidden": HIDDEN_STATUSES,
}

STATUS_ACTIONS = {
    None: ("save", "applied", "not_interested"),
    "recommended": ("save", "applied", "not_interested"),
    "saved": ("applied", "remind_later", "not_interested"),
    "remind_later": ("applied", "not_interested"),
    "applied": ("assessment_started", "remind_later", "not_interested"),
    "waiting": ("assessment_started", "remind_later", "not_interested"),
    "assessment_invited": ("assessment_started", "remind_later", "not_interested"),
    "assessment_started": ("assessment_completed", "remind_later", "not_interested"),
    "assessment_completed": ("remind_later", "accepted", "rejected"),
    "accepted": (),
    "active_worker": (),
    "paid_task_received": (),
    "rejected": (),
    "not_interested": ("show_again",),
    "expired": (),
}


def seed_local_product_profiles():
    profiles, _ = product_state.load_profiles(LOCAL_PRODUCT_PROFILE_SEED_PATH)
    with get_connection() as conn:
        existing_ids = {
            row["profile_id"]
            for row in conn.execute("SELECT profile_id FROM user_profiles").fetchall()
        }
        for profile in profiles:
            if profile["profile_id"] not in existing_ids:
                product_state.upsert_profile(conn, profile)


def main():
    args = parse_args()
    product_state.initialize_product_state_schema()
    seed_local_product_profiles()
    registry = MatchRunRegistry()
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(registry=registry, demo_mode=args.demo),
    )
    url = f"http://{args.host}:{args.port}/"
    print("")
    print("Wahojobs Local Product UI")
    print("=========================")
    print(f"Open: {url}")
    if args.demo:
        print(f"Demo personas: {url}find-matches")
    else:
        print("Normal mode owner: local_user")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped local product UI.")
    finally:
        server.server_close()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run a minimal local Wahojobs product UI prototype."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Expose the three explicit development personas on Find Matches.",
    )
    return parser.parse_args()


_BROWSER_DEPENDENCY_FAILED = object()
_BROWSER_DEPENDENCY_SANITIZE_FAILED = object()
_BROWSER_CONTROL_FLOW = (KeyboardInterrupt, SystemExit, GeneratorExit)
_BROWSER_EXCEPTION_CORE_FIELDS = frozenset(
    {
        "__cause__",
        "__class__",
        "__context__",
        "__dict__",
        "__notes__",
        "__suppress_context__",
        "__traceback__",
        "__weakref__",
        "args",
    }
)


def _detach_browser_handler_exception(exc):
    pending = [exc]
    observed = set()
    sanitized = True
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in observed:
            continue
        observed.add(id(current))

        for link_name in ("__cause__", "__context__"):
            try:
                linked = object.__getattribute__(current, link_name)
            except BaseException:
                sanitized = False
                continue
            if isinstance(linked, BaseException):
                pending.append(linked)

        exception_type = type(current)
        slot_descriptors = []
        try:
            exception_mro = type.__getattribute__(
                exception_type,
                "__mro__",
            )
        except BaseException:
            exception_mro = ()
            sanitized = False
        for current_type in exception_mro:
            try:
                type_namespace = type.__getattribute__(
                    current_type,
                    "__dict__",
                )
            except BaseException:
                sanitized = False
                continue
            for slot_name, descriptor in type_namespace.items():
                if (
                    slot_name in _BROWSER_EXCEPTION_CORE_FIELDS
                    or not isinstance(
                        descriptor,
                        (
                            types.MemberDescriptorType,
                            types.GetSetDescriptorType,
                        ),
                    )
                ):
                    continue
                slot_descriptors.append((slot_name, descriptor))

        slot_values = []
        for slot_name, descriptor in slot_descriptors:
            try:
                slot_value = descriptor.__get__(current, exception_type)
            except (AttributeError, TypeError):
                continue
            except BaseException:
                sanitized = False
                continue
            if isinstance(slot_value, BaseException):
                pending.append(slot_value)
            slot_values.append((slot_name, descriptor, slot_value))
        slot_values.sort(key=lambda row: row[0] == "object")

        for slot_name, descriptor, slot_value in slot_values:
            try:
                descriptor.__set__(current, None)
            except BaseException:
                if (
                    slot_value is not None
                    and type(slot_value) not in (bool, float, int)
                ):
                    sanitized = False
        slot_values = None

        try:
            payload = object.__getattribute__(current, "__dict__")
        except AttributeError:
            payload = None
        except BaseException:
            payload = None
            sanitized = False
        if type(payload) is dict:
            for payload_value in tuple(payload.values()):
                if isinstance(payload_value, BaseException):
                    pending.append(payload_value)
            try:
                payload.clear()
            except BaseException:
                sanitized = False
        payload = None
        payload_value = None

        for field_name, replacement in (
            ("__traceback__", None),
            ("__cause__", None),
            ("__context__", None),
            ("__suppress_context__", True),
            ("args", ()),
        ):
            try:
                BaseException.__dict__[field_name].__set__(
                    current,
                    replacement,
                )
            except BaseException:
                sanitized = False

        for field_name, expected in (
            ("__traceback__", None),
            ("__cause__", None),
            ("__context__", None),
            ("__suppress_context__", True),
            ("args", ()),
        ):
            try:
                if (
                    BaseException.__dict__[field_name].__get__(current)
                    != expected
                ):
                    sanitized = False
            except BaseException:
                sanitized = False
        try:
            notes = object.__getattribute__(current, "__notes__")
        except AttributeError:
            notes = None
        except BaseException:
            notes = None
            sanitized = False
        if notes not in (None, (), []):
            sanitized = False
        notes = None
        current = None
    exc = None
    return sanitized


def _finish_browser_dependency_failure(failure):
    sanitized = _detach_browser_handler_exception(failure)
    failure = None
    if sanitized:
        return _BROWSER_DEPENDENCY_FAILED
    return _BROWSER_DEPENDENCY_SANITIZE_FAILED


def _raise_browser_dependency_control(control):
    _detach_browser_handler_exception(control)
    propagated = control
    control = None
    raise propagated from None


def _match_durable_browser_route_worker(integration, path):
    outcome = _BROWSER_DEPENDENCY_FAILED
    failure = None
    control = None
    try:
        outcome = integration.matches_route(path)
    except _BROWSER_CONTROL_FLOW as exc:
        control = exc
    except Exception as exc:
        failure = exc
    finally:
        integration = None
        path = None
    if failure is not None:
        return _finish_browser_dependency_failure(failure)
    if control is not None:
        _raise_browser_dependency_control(control)
    return outcome


def _handle_durable_browser_request_worker(
    integration,
    method,
    target,
    headers,
    body_stream,
):
    outcome = _BROWSER_DEPENDENCY_FAILED
    failure = None
    control = None
    try:
        outcome = integration.handle(method, target, headers, body_stream)
    except _BROWSER_CONTROL_FLOW as exc:
        control = exc
    except Exception as exc:
        failure = exc
    finally:
        integration = None
        method = None
        target = None
        headers = None
        body_stream = None
    if failure is not None:
        return _finish_browser_dependency_failure(failure)
    if control is not None:
        _raise_browser_dependency_control(control)
    return outcome


def _validate_durable_browser_response_worker(validate, response):
    outcome = True
    failure = None
    control = None
    try:
        validate(response)
    except _BROWSER_CONTROL_FLOW as exc:
        control = exc
    except Exception as exc:
        failure = exc
    finally:
        validate = None
        response = None
    if failure is not None:
        return _finish_browser_dependency_failure(failure)
    if control is not None:
        _raise_browser_dependency_control(control)
    return outcome


def _strict_urlencoded_multimap(body):
    if (
        type(body) is not bytes
        or not body
        or len(body) > MAX_PROFILE_CONFIRMATION_BODY_BYTES
        or _INVALID_FORM_PERCENT_ESCAPE.search(body) is not None
    ):
        return None
    fields = body.split(b"&")
    if (
        len(fields) > MAX_PROFILE_CONFIRMATION_FIELDS
        or any(not field or b"=" not in field for field in fields)
    ):
        return None
    parsed = {}
    try:
        for encoded_field in fields:
            encoded_name, encoded_value = encoded_field.split(b"=", 1)
            name = unquote_to_bytes(encoded_name.replace(b"+", b" ")).decode(
                "utf-8",
                "strict",
            )
            value = unquote_to_bytes(encoded_value.replace(b"+", b" ")).decode(
                "utf-8",
                "strict",
            )
            parsed.setdefault(name, []).append(value)
    except (UnicodeError, ValueError):
        return None
    return parsed


def make_handler(
    registry=None,
    demo_mode=False,
    persistent_profile_browser_integration=None,
    durable_google_login_browser_integration=None,
    exclusive_browser_integration=False,
    confirmed_profile_artifact_sink=None,
    completed_profile_confirmation_authenticator=None,
    profile_confirmation_public_origin=None,
):
    registry = registry if registry is not None else MatchRunRegistry()
    profile_browser_integration = persistent_profile_browser_integration
    if profile_browser_integration is not None and not all(
        callable(getattr(profile_browser_integration, name, None))
        for name in ("matches_route", "handle")
    ):
        raise ValueError("invalid_profile_browser_integration")
    login_browser_integration = durable_google_login_browser_integration
    if login_browser_integration is not None and not all(
        callable(getattr(login_browser_integration, name, None))
        for name in ("matches_route", "handle")
    ):
        raise ValueError("invalid_durable_google_login_browser_integration")
    if type(exclusive_browser_integration) is not bool or (
        exclusive_browser_integration and login_browser_integration is None
    ):
        raise ValueError("invalid_exclusive_browser_integration")
    if confirmed_profile_artifact_sink is not None and not callable(
        confirmed_profile_artifact_sink
    ):
        raise ValueError("invalid_confirmed_profile_artifact_sink")
    if completed_profile_confirmation_authenticator is not None and (
        confirmed_profile_artifact_sink is None
        or not callable(completed_profile_confirmation_authenticator)
    ):
        raise ValueError("invalid_completed_profile_confirmation_authenticator")
    confirmation_origin = profile_confirmation_public_origin
    if confirmed_profile_artifact_sink is None:
        if confirmation_origin is not None:
            raise ValueError("unexpected_profile_confirmation_public_origin")
        confirmation_authority = None
    else:
        if type(confirmation_origin) is not str:
            raise ValueError("invalid_profile_confirmation_public_origin")
        parsed_confirmation_origin = urlparse(confirmation_origin)
        if (
            parsed_confirmation_origin.scheme != "https"
            or not parsed_confirmation_origin.netloc
            or parsed_confirmation_origin.path
            or parsed_confirmation_origin.params
            or parsed_confirmation_origin.query
            or parsed_confirmation_origin.fragment
            or parsed_confirmation_origin.username is not None
            or parsed_confirmation_origin.password is not None
        ):
            raise ValueError("invalid_profile_confirmation_public_origin")
        confirmation_authority = parsed_confirmation_origin.netloc

    class ProductAppHandler(BaseHTTPRequestHandler):
        match_run_registry = registry
        is_demo_mode = demo_mode
        _durable_google_login_browser_integration = login_browser_integration
        _confirmed_profile_artifact_sink = confirmed_profile_artifact_sink
        _completed_profile_confirmation_authenticator = (
            completed_profile_confirmation_authenticator
        )

        def __getattr__(self, name):
            if type(name) is str and name.startswith("do_"):
                method = name[3:]
                if _HTTP_METHOD_TOKEN.fullmatch(method) is not None:
                    return lambda: self.dispatch_extension_method(method)
            raise AttributeError(name)

        def dispatch_extension_method(self, method):
            path = urlparse(self.path).path
            if self.dispatch_durable_google_login_browser_integration(method, path):
                return
            if self.reject_exclusive_browser_fallthrough(method):
                return
            if self.dispatch_profile_browser_integration(method, path):
                return
            self.send_error(
                HTTPStatus.NOT_IMPLEMENTED,
                f"Unsupported method ({method!r})",
            )

        def do_GET(self):
            parsed = urlparse(self.path)
            if self.dispatch_durable_google_login_browser_integration("GET", parsed.path):
                return
            if self.reject_exclusive_browser_fallthrough("GET", parsed.path):
                return
            if self.dispatch_profile_browser_integration("GET", parsed.path):
                return
            if parsed.path == "/health":
                self.write_text("ok\n")
                return
            if parsed.path in FIND_MATCHES_PATHS:
                if (
                    parsed.path == PROFILE_CONFIRMATION_PATH
                    and type(self)._confirmed_profile_artifact_sink is not None
                    and self.profile_confirmation_request_headers() is None
                ):
                    self.write_safe_browser_error(
                        "This profile request is not valid.",
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                params = parse_qs(parsed.query)
                run_id = first_value(params, "run")
                if run_id and registry.get(run_id) is None:
                    self.write_html(
                        render_error("That match run is unknown or has expired. Start a new search."),
                        status=HTTPStatus.GONE,
                    )
                    return
                try:
                    self.write_html(
                        render_preview_from_params(
                            params,
                            registry=registry,
                            demo_mode=demo_mode,
                        )
                    )
                except pipeline_records.PipelineRecordInvariant:
                    self.write_html(
                        render_error(
                            "Matches are temporarily unavailable because pipeline state needs reconciliation."
                        ),
                        status=HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                return
            if parsed.path not in TRACKER_PATHS | HEAVY_DASHBOARD_PATHS:
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            params = parse_qs(parsed.query)
            message = first_value(params, "message")
            error = first_value(params, "error")
            try:
                run = resolve_tracker_match_run(params, registry, demo_mode)
                profile_id = run.owner_profile_id
                if parsed.path in HEAVY_DASHBOARD_PATHS:
                    context = demo.build_demo_context(
                        profile_id=profile_id,
                        use_product_state=True,
                    )
                    context = normalize_dashboard_pipeline_context(
                        context,
                        profile_id,
                    )
                    body = render_dashboard(
                        context,
                        match_run_id=run.match_run_id,
                        demo_mode=demo_mode,
                        message=message,
                        error=error,
                    )
                else:
                    context = load_lightweight_tracker_context(profile_id)
                    body = render_lightweight_tracker(
                        context,
                        match_run_id=run.match_run_id,
                        tracker_view=first_value(params, "view") or "all",
                        demo_mode=demo_mode,
                        message=message,
                        error=error,
                    )
                self.write_html(body)
            except ActionError as exc:
                self.write_html(render_error(str(exc)), status=exc.status)
            except pipeline_records.PipelineRecordInvariant:
                self.write_html(
                    render_error(
                        "My Jobs is temporarily unavailable because pipeline state needs reconciliation."
                    ),
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
            except SystemExit as exc:
                self.write_html(render_error(str(exc)), status=HTTPStatus.BAD_REQUEST)
            except Exception:
                self.write_html(
                    render_error("The page could not be loaded safely."),
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def do_HEAD(self):
            parsed = urlparse(self.path)
            if self.dispatch_durable_google_login_browser_integration("HEAD", parsed.path):
                return
            if self.reject_exclusive_browser_fallthrough("HEAD"):
                return
            if self.dispatch_profile_browser_integration("HEAD", parsed.path):
                return
            self.send_error(HTTPStatus.NOT_IMPLEMENTED, "Unsupported method ('HEAD')")

        def do_POST(self):
            parsed = urlparse(self.path)
            if self.dispatch_durable_google_login_browser_integration("POST", parsed.path):
                return
            form = None
            if (
                exclusive_browser_integration
                and type(self)._confirmed_profile_artifact_sink is not None
                and parsed.path == PROFILE_CONFIRMATION_PATH
            ):
                form = self.read_profile_confirmation_form()
                if form is None:
                    self.write_safe_browser_error(
                        "This profile request is not valid.",
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                if form.get("form_action") == ["confirm_profile"]:
                    if self.path != PROFILE_CONFIRMATION_PATH:
                        self.write_safe_browser_error(
                            "This page is not available.",
                            status=HTTPStatus.NOT_FOUND,
                        )
                        return
                    self.dispatch_profile_confirmation(form)
                    return
            if self.reject_exclusive_browser_fallthrough("POST", parsed.path):
                return
            if self.dispatch_profile_browser_integration("POST", parsed.path):
                return
            if parsed.path in FIND_MATCHES_PATHS:
                if form is None:
                    form = self.read_form(keep_blank_values=True)
                try:
                    run = create_match_run(
                        form,
                        registry,
                        demo_mode,
                        confirmed_profile_artifact_sink=(
                            type(self)._confirmed_profile_artifact_sink
                        ),
                        completed_profile_confirmation_authenticator=(
                            type(self)._completed_profile_confirmation_authenticator
                        ),
                        authentication_input=tuple(self.headers.raw_items()),
                    )
                    if type(run) is ConfirmedProfileCreation:
                        self.write_confirmed_profile_creation(run.artifact_offer)
                        return
                    self.redirect(
                        "/find-matches",
                        run=run.match_run_id,
                        review="1" if not run.profile_confirmed else "",
                    )
                except (ActionError, SystemExit) as exc:
                    status = exc.status if isinstance(exc, ActionError) else HTTPStatus.BAD_REQUEST
                    self.write_html(render_error(str(exc)), status=status)
                return
            if parsed.path != "/action":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            form = self.read_form(keep_blank_values=True)
            return_to = safe_action_context_value(form, "return_to") or "preview-recommendations"
            run_id = safe_action_context_value(form, "match_run_id")
            wants_json = (
                "application/json" in self.headers.get("Accept", "").lower()
                or self.headers.get(INLINE_ACTION_HEADER, "") == "1"
            )
            try:
                validate_action_form(form)
                return_to = action_form_value(form, "return_to")
                run_id = action_form_value(form, "match_run_id")
                run = require_match_run(registry, run_id)
                result = handle_action(form, run)
                if wants_json:
                    self.write_json(action_json_payload(result, run, form))
                else:
                    self.redirect(
                        "/find-matches",
                        fragment=return_to,
                        run=run.match_run_id,
                        message=result["message"],
                    )
            except ActionError as exc:
                self.write_action_error(exc, wants_json, run_id, return_to)
            except SystemExit as exc:
                self.write_action_error(
                    ActionError(str(exc)),
                    wants_json,
                    run_id,
                    return_to,
                )
            except Exception:
                self.write_action_error(
                    ActionError(
                        "The action could not be completed safely.",
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    ),
                    wants_json,
                    run_id,
                    return_to,
                )

        def dispatch_profile_confirmation(self, form):
            if (
                not exclusive_browser_integration
                or type(self)._confirmed_profile_artifact_sink is None
                or self.path != PROFILE_CONFIRMATION_PATH
                or type(form) is not dict
            ):
                return False
            if form.get("form_action") != ["confirm_profile"]:
                return False
            try:
                result = confirm_profile_review(
                    form,
                    registry,
                    confirmed_profile_artifact_sink=(
                        type(self)._confirmed_profile_artifact_sink
                    ),
                    completed_profile_confirmation_authenticator=(
                        type(self)._completed_profile_confirmation_authenticator
                    ),
                    authentication_input=tuple(self.headers.raw_items()),
                    _allow_matching=False,
                )
                if type(result) is not ConfirmedProfileCreation:
                    raise RuntimeError("profile_confirmation_unavailable")
                self.write_confirmed_profile_creation(result.artifact_offer)
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except ActionError as exc:
                status = exc.status
                exc = None
                self.write_safe_browser_error(
                    "This profile confirmation could not be completed safely.",
                    status=status,
                )
            except Exception as exc:
                exc = None
                self.write_safe_browser_error(
                    "This profile confirmation could not be completed safely.",
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
            return True

        def read_profile_confirmation_form(self):
            items = self.profile_confirmation_request_headers(
                require_same_origin=True
            )
            if items is None:
                return None
            values = lambda name: tuple(
                value for candidate, value in items if candidate.lower() == name
            )
            content_types = values("content-type")
            lengths = values("content-length")
            if (
                len(content_types) != 1
                or content_types[0].lower()
                != "application/x-www-form-urlencoded"
                or len(lengths) != 1
                or re.fullmatch(r"(?:0|[1-9][0-9]{0,5})", lengths[0]) is None
                or values("transfer-encoding")
            ):
                return None
            length = int(lengths[0])
            if length < 1 or length > MAX_PROFILE_CONFIRMATION_BODY_BYTES:
                return None
            try:
                body = self.rfile.read(length)
                if type(body) is not bytes or len(body) != length:
                    return None
                return _strict_urlencoded_multimap(body)
            except Exception:
                return None

        def profile_confirmation_request_headers(self, *, require_same_origin=False):
            if (
                type(require_same_origin) is not bool
                or type(self)._confirmed_profile_artifact_sink is None
                or confirmation_authority is None
            ):
                return None
            try:
                items = tuple(self.headers.raw_items())
            except Exception:
                return None
            items = persistent_profile_browser_security._validated_header_items(items)
            if items is None or not persistent_profile_browser_security._trusted_host_headers(
                items,
                confirmation_authority,
            ):
                return None
            if require_same_origin and not persistent_profile_browser_security._trusted_same_origin(
                items,
                confirmation_origin,
            ):
                return None
            return items

        def do_PUT(self):
            if self.dispatch_durable_google_login_browser_integration(
                "PUT", urlparse(self.path).path
            ):
                return
            if self.reject_exclusive_browser_fallthrough("PUT"):
                return
            if self.dispatch_profile_browser_integration("PUT", urlparse(self.path).path):
                return
            self.send_error(HTTPStatus.NOT_IMPLEMENTED, "Unsupported method ('PUT')")

        def do_PATCH(self):
            if self.dispatch_durable_google_login_browser_integration(
                "PATCH", urlparse(self.path).path
            ):
                return
            if self.reject_exclusive_browser_fallthrough("PATCH"):
                return
            if self.dispatch_profile_browser_integration("PATCH", urlparse(self.path).path):
                return
            self.send_error(HTTPStatus.NOT_IMPLEMENTED, "Unsupported method ('PATCH')")

        def do_DELETE(self):
            if self.dispatch_durable_google_login_browser_integration(
                "DELETE", urlparse(self.path).path
            ):
                return
            if self.reject_exclusive_browser_fallthrough("DELETE"):
                return
            if self.dispatch_profile_browser_integration("DELETE", urlparse(self.path).path):
                return
            self.send_error(HTTPStatus.NOT_IMPLEMENTED, "Unsupported method ('DELETE')")

        def do_OPTIONS(self):
            path = urlparse(self.path).path
            if self.dispatch_durable_google_login_browser_integration("OPTIONS", path):
                return
            if self.reject_exclusive_browser_fallthrough("OPTIONS"):
                return
            if self.dispatch_profile_browser_integration("OPTIONS", path):
                return
            self.send_error(HTTPStatus.NOT_IMPLEMENTED, "Unsupported method ('OPTIONS')")

        def do_TRACE(self):
            path = urlparse(self.path).path
            if self.dispatch_durable_google_login_browser_integration("TRACE", path):
                return
            if self.reject_exclusive_browser_fallthrough("TRACE"):
                return
            if self.dispatch_profile_browser_integration("TRACE", path):
                return
            self.send_error(HTTPStatus.NOT_IMPLEMENTED, "Unsupported method ('TRACE')")

        def dispatch_durable_google_login_browser_integration(
            self,
            method,
            path,
            *,
            force=False,
        ):
            integration = type(
                self
            )._durable_google_login_browser_integration
            if integration is None:
                return False
            response = None
            matches = None
            validation = None
            try:
                if not force:
                    matches = _match_durable_browser_route_worker(
                        integration,
                        path,
                    )
                    if matches is _BROWSER_DEPENDENCY_SANITIZE_FAILED:
                        self.sanitize_browser_control_flow_request()
                        return True
                    if matches is _BROWSER_DEPENDENCY_FAILED:
                        self.clear_pending_browser_headers()
                        self.write_safe_browser_error(
                            "The account page could not be loaded safely.",
                            status=HTTPStatus.SERVICE_UNAVAILABLE,
                        )
                        return True
                    if matches is not True:
                        return False
                response = _handle_durable_browser_request_worker(
                    integration,
                    method,
                    self.path,
                    self.headers,
                    self.rfile,
                )
                if response is _BROWSER_DEPENDENCY_SANITIZE_FAILED:
                    response = None
                    self.clear_pending_browser_headers()
                    self.sanitize_browser_control_flow_request()
                    return True
                if response is _BROWSER_DEPENDENCY_FAILED:
                    response = None
                    self.clear_pending_browser_headers()
                    self.write_safe_browser_error(
                        "The account page could not be loaded safely.",
                        status=HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                    return True
                validation = _validate_durable_browser_response_worker(
                    self.validate_profile_browser_response,
                    response,
                )
                if validation in (
                    _BROWSER_DEPENDENCY_FAILED,
                    _BROWSER_DEPENDENCY_SANITIZE_FAILED,
                ):
                    cleanup_sanitized = self.fail_browser_response_delivery(
                        response
                    )
                    self.clear_pending_browser_headers()
                    response = None
                    if (
                        validation is _BROWSER_DEPENDENCY_SANITIZE_FAILED
                        or not cleanup_sanitized
                    ):
                        self.sanitize_browser_control_flow_request()
                        integration = None
                        return True
                    self.write_safe_browser_error(
                        "The account page could not be loaded safely.",
                        status=HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                    return True
            except _BROWSER_CONTROL_FLOW as exc:
                self.fail_browser_response_delivery(response)
                self.clear_pending_browser_headers()
                response = None
                matches = None
                validation = None
                propagated = exc
                exc = None
                self.sanitize_browser_control_flow_request()
                method = None
                path = None
                force = None
                integration = None
                self = None
                _detach_browser_handler_exception(propagated)
                raise propagated from None
            try:
                self.write_profile_browser_response(response, head=method == "HEAD")
            except _BROWSER_CONTROL_FLOW as exc:
                response = None
                propagated = exc
                exc = None
                self.sanitize_browser_control_flow_request()
                method = None
                path = None
                force = None
                integration = None
                self = None
                _detach_browser_handler_exception(propagated)
                raise propagated from None
            return True

        def reject_exclusive_browser_fallthrough(self, method, path=None):
            if not exclusive_browser_integration:
                return False
            path = urlparse(self.path).path if path is None else path
            if path == PROFILE_CONFIRMATION_PATH and method in {"GET", "POST"}:
                if type(self)._confirmed_profile_artifact_sink is not None:
                    return False
                self.write_safe_browser_error(
                    "Profile creation is temporarily unavailable.",
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return True
            return self.dispatch_durable_google_login_browser_integration(
                method,
                path,
                force=True,
            )

        def dispatch_profile_browser_integration(self, method, path):
            if profile_browser_integration is None:
                return False
            try:
                matches = profile_browser_integration.matches_route(path)
            except Exception:
                self.write_html(
                    render_error("The profile page could not be loaded safely."),
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return True
            if matches is not True:
                return False
            try:
                response = profile_browser_integration.handle(
                    method,
                    self.path,
                    self.headers,
                )
            except Exception:
                self.write_html(
                    render_error("The profile page could not be loaded safely."),
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return True
            try:
                self.validate_profile_browser_response(response)
            except Exception:
                self.write_html(
                    render_error("The profile page could not be loaded safely."),
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return True
            self.write_profile_browser_response(response, head=method == "HEAD")
            return True

        @staticmethod
        def validate_profile_browser_response(response):
            status = getattr(response, "status", None)
            body = getattr(response, "body", None)
            headers = getattr(response, "headers", None)
            if (
                type(status) is not int
                or not 100 <= status <= 599
                or type(body) is not bytes
                or len(body) > 1_048_576
                or type(headers) is not tuple
            ):
                raise ValueError("invalid_profile_browser_response")
            header_bytes = 0
            for header in headers:
                if (
                    type(header) is not tuple
                    or len(header) != 2
                ):
                    raise ValueError("invalid_profile_browser_response")
                name, value = header
                if (
                    type(name) is not str
                    or _HTTP_METHOD_TOKEN.fullmatch(name) is None
                    or type(value) is not str
                    or _BROWSER_HEADER_VALUE_FORBIDDEN.search(value)
                    is not None
                ):
                    raise ValueError("invalid_profile_browser_response")
                try:
                    header_bytes += len(name.encode("ascii"))
                    header_bytes += len(value.encode("latin-1"))
                except UnicodeError:
                    raise ValueError(
                        "invalid_profile_browser_response"
                    ) from None
            if header_bytes > 16_384:
                raise ValueError("invalid_profile_browser_response")
            acknowledge = getattr(response, "acknowledge_delivery", None)
            fail = getattr(response, "fail_delivery", None)
            if (acknowledge is None) != (fail is None) or (
                acknowledge is not None
                and (not callable(acknowledge) or not callable(fail))
            ):
                raise ValueError("invalid_profile_browser_response")

        def write_profile_browser_response(self, response, *, head=False):
            delivery_aware = False
            name = None
            value = None
            acknowledge = None
            fail = None
            try:
                acknowledge = getattr(
                    response,
                    "acknowledge_delivery",
                    None,
                )
                fail = getattr(response, "fail_delivery", None)
                delivery_aware = callable(acknowledge) and callable(fail)
            except BaseException as exc:
                cleanup_sanitized = self.fail_browser_response_delivery(
                    response
                )
                self.clear_pending_browser_headers()
                response = None
                acknowledge = None
                fail = None
                if isinstance(exc, _BROWSER_CONTROL_FLOW):
                    propagated = exc
                    exc = None
                    self.sanitize_browser_control_flow_request()
                    _detach_browser_handler_exception(propagated)
                    raise propagated from None
                sanitized = _detach_browser_handler_exception(exc)
                exc = None
                if not sanitized or not cleanup_sanitized:
                    self.sanitize_browser_control_flow_request()
                return
            acknowledge = None
            fail = None
            try:
                self.send_response(response.status)
                for name, value in response.headers:
                    self.send_header(name, value)
                self.end_headers()
            except BaseException as exc:
                cleanup_sanitized = True
                if delivery_aware:
                    cleanup_sanitized = self.fail_browser_response_delivery(
                        response
                    )
                self.clear_pending_browser_headers()
                response = None
                name = None
                value = None
                if isinstance(exc, _BROWSER_CONTROL_FLOW):
                    propagated = exc
                    exc = None
                    self.sanitize_browser_control_flow_request()
                    _detach_browser_handler_exception(propagated)
                    raise propagated from None
                sanitized = _detach_browser_handler_exception(exc)
                exc = None
                if not sanitized or not cleanup_sanitized:
                    self.sanitize_browser_control_flow_request()
                return
            name = None
            value = None
            if delivery_aware:
                try:
                    response.acknowledge_delivery()
                except BaseException as exc:
                    response = None
                    if isinstance(exc, _BROWSER_CONTROL_FLOW):
                        propagated = exc
                        exc = None
                        self.sanitize_browser_control_flow_request()
                        _detach_browser_handler_exception(propagated)
                        raise propagated from None
                    sanitized = _detach_browser_handler_exception(exc)
                    exc = None
                    if not sanitized:
                        self.sanitize_browser_control_flow_request()
                    return
            if not head:
                payload = None
                body_loaded = False
                try:
                    payload = response.body
                    body_loaded = True
                    response = None
                    self.wfile.write(payload)
                except BaseException as exc:
                    response = None
                    payload = None
                    if isinstance(exc, _BROWSER_CONTROL_FLOW):
                        propagated = exc
                        exc = None
                        self.sanitize_browser_control_flow_request()
                        _detach_browser_handler_exception(propagated)
                        raise propagated from None
                    if not body_loaded or isinstance(exc, OSError):
                        sanitized = _detach_browser_handler_exception(exc)
                        exc = None
                        if not sanitized:
                            self.sanitize_browser_control_flow_request()
                        return
                    propagated = exc
                    exc = None
                    self.sanitize_browser_control_flow_request()
                    _detach_browser_handler_exception(propagated)
                    raise propagated from None
                payload = None

        @staticmethod
        def fail_browser_response_delivery(response):
            fail = None
            sanitized = True
            try:
                fail = getattr(response, "fail_delivery", None)
                if callable(fail):
                    fail()
            except BaseException as exc:
                sanitized = _detach_browser_handler_exception(exc)
                exc = None
            finally:
                response = None
                fail = None
            return sanitized

        def clear_pending_browser_headers(self):
            pending = getattr(self, "_headers_buffer", None)
            if type(pending) is list:
                try:
                    pending.clear()
                except BaseException as exc:
                    _detach_browser_handler_exception(exc)
                    exc = None
            pending = None

        def sanitize_browser_control_flow_request(self):
            self.clear_pending_browser_headers()
            for name, value in (
                ("headers", ()),
                ("path", ""),
                ("requestline", ""),
                ("raw_requestline", b""),
                ("command", ""),
            ):
                try:
                    setattr(self, name, value)
                except BaseException as exc:
                    _detach_browser_handler_exception(exc)
                    exc = None
            reader = getattr(self, "rfile", None)
            close = getattr(reader, "close", None)
            if callable(close):
                try:
                    close()
                except BaseException as exc:
                    _detach_browser_handler_exception(exc)
                    exc = None
            try:
                self.rfile = None
            except BaseException as exc:
                _detach_browser_handler_exception(exc)
                exc = None
            reader = None
            close = None
            try:
                self.close_connection = True
            except BaseException as exc:
                _detach_browser_handler_exception(exc)
                exc = None

        def write_safe_browser_error(self, message, *, status):
            self.clear_pending_browser_headers()
            payload = render_error(message).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
                )
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
            except _BROWSER_CONTROL_FLOW as exc:
                payload = None
                self.clear_pending_browser_headers()
                propagated = exc
                exc = None
                self.sanitize_browser_control_flow_request()
                _detach_browser_handler_exception(propagated)
                raise propagated from None
            except Exception as exc:
                payload = None
                self.clear_pending_browser_headers()
                sanitized = _detach_browser_handler_exception(exc)
                exc = None
                if not sanitized:
                    self.sanitize_browser_control_flow_request()
                return

        def read_form(self, *, keep_blank_values=False):
            length = int(self.headers.get("Content-Length", "0"))
            return parse_qs(
                self.rfile.read(length).decode("utf-8"),
                keep_blank_values=keep_blank_values,
            )

        def write_action_error(self, exc, wants_json, run_id, return_to):
            if wants_json:
                self.write_json({"ok": False, "error": str(exc)}, status=exc.status)
            elif isinstance(exc, MalformedActionRequest):
                self.write_html(render_error(str(exc)), status=exc.status)
            elif run_id:
                self.redirect(
                    "/find-matches",
                    fragment=return_to,
                    run=run_id,
                    error=str(exc),
                )
            else:
                self.write_html(render_error(str(exc)), status=exc.status)

        def redirect(self, path, fragment="", **params):
            query = urlencode({key: value for key, value in params.items() if value})
            location = path + (f"?{query}" if query else "")
            if fragment:
                location += f"#{fragment}"
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.end_headers()

        def write_html(self, content, status=HTTPStatus.OK):
            payload = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def write_confirmed_profile_creation(self, offer):
            if not _is_confirmed_profile_artifact_offer(offer):
                raise ValueError("invalid_confirmed_profile_creation")
            content = render_confirmed_profile_creation(offer)
            payload = content.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
            )
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def write_text(self, content, status=HTTPStatus.OK):
            payload = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def write_json(self, content, status=HTTPStatus.OK):
            payload = json.dumps(content, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            return

    return ProductAppHandler


def require_owner_profile(profile_id):
    with get_connection() as conn:
        product_state.require_profile(conn, profile_id)


def profile_draft_fingerprint(profile):
    """Digest the documented identity-free canonical bytes for browser review."""

    if type(profile) is not IdentityFreeCanonicalProfileV1:
        raise ValueError("invalid_identity_free_profile_draft")
    return hashlib.sha256(profile.canonical_bytes).hexdigest()


def _is_sha256_digest(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _identity_free_display_name(
    raw_input,
    *,
    experience,
    skills,
    domains,
    languages,
):
    supplied = re.sub(r"\s+", " ", str(raw_input).strip())
    if not supplied:
        raise ValueError("Profile input is empty.")
    semantic_labels = unique_strings(
        list(experience.get("job_titles") or [])
        + list(experience.get("recent_roles") or [])
        + list(skills or [])
        + list(domains or [])
        + [
            language.get("language")
            for language in languages
            if type(language) is dict
        ]
    )
    if semantic_labels:
        return " / ".join(semantic_labels[:3])[:160].rstrip()
    words = re.findall(r"[^\W_]+", supplied, flags=re.UNICODE)[:6]
    summary = " ".join(words).strip()
    if not summary:
        raise ValueError("Profile input is empty.")
    return (summary + " profile")[:160].rstrip()


def _identity_free_matcher_text(canonical):
    blocks = [
        canonical["identity"].get("display_name"),
        canonical["location"].get("country"),
        canonical["location"].get("region"),
        canonical["location"].get("city"),
        canonical["education"].get("education_level"),
        *(canonical["education"].get("degrees") or []),
        *(canonical["education"].get("fields_or_domains") or []),
        *(canonical["experience"].get("recent_roles") or []),
        *(canonical["experience"].get("occupational_families") or []),
        *(canonical["experience"].get("job_titles") or []),
        *(canonical["experience"].get("professional_domains") or []),
        *(canonical["experience"].get("industries") or []),
        *(canonical["experience"].get("specialties") or []),
        *(canonical["skills"].get("normalized") or []),
        *(canonical["preferences"].get("target_opportunity_types") or []),
        *(canonical["preferences"].get("preferred_task_types") or []),
        *(canonical["preferences"].get("work_preferences") or []),
    ]
    for language in canonical["languages"]:
        blocks.extend(
            (
                language.get("language"),
                language.get("locale"),
                language.get("proficiency"),
            )
        )
    if canonical["preferences"].get("remote") is True:
        blocks.append("remote")
    if canonical["preferences"].get("flexible") is True:
        blocks.append("flexible")
    years = canonical["experience"].get("total_years")
    if years not in (None, ""):
        blocks.append(f"{years} years experience")
    return ". ".join(unique_strings(blocks))


def _identity_free_matcher_projection(canonical):
    identity = canonical["identity"]
    location = canonical["location"]
    education = canonical["education"]
    experience = canonical["experience"]
    skills = canonical["skills"]
    preferences = canonical["preferences"]
    constraints = canonical["constraints"]
    domains = unique_strings(
        list(education.get("fields_or_domains") or [])
        + list(experience.get("professional_domains") or [])
    )
    work_preferences = unique_strings(
        list(preferences.get("work_preferences") or [])
        + list(preferences.get("employment_types") or [])
        + (["remote"] if preferences.get("remote") is True else [])
        + (["flexible"] if preferences.get("flexible") is True else [])
    )
    hard_constraints = list(constraints.get("hard_constraints") or [])
    soft_preferences = list(constraints.get("soft_preferences") or [])
    return {
        "display_name": identity["display_name"],
        "summary": _identity_free_matcher_text(canonical),
        "education_level": str(education.get("education_level") or "not_specified"),
        "degrees_or_domains": domains,
        "languages": [
            str(language.get("language") or "").strip()
            for language in canonical["languages"]
            if str(language.get("language") or "").strip()
        ],
        "language_proficiency": {
            str(language.get("language") or "").strip(): str(
                language.get("proficiency") or UNKNOWN
            )
            for language in canonical["languages"]
            if str(language.get("language") or "").strip()
        },
        "skills": unique_strings(skills.get("normalized") or []),
        "work_preferences": work_preferences,
        "constraints": unique_strings(hard_constraints + soft_preferences),
        "target_opportunity_types": unique_strings(
            preferences.get("target_opportunity_types") or []
        ),
        "notes": "",
        "avoid_keywords": unique_strings(constraints.get("avoid_keywords") or []),
        "signals": [
            [signal["reason"], signal["keywords"], signal["points"]]
            for signal in canonical["derived_matcher_signals"]["signals"]
        ],
        "location": str(location.get("country") or location.get("residence") or ""),
        "country": str(location.get("country") or ""),
        "residence": str(location.get("residence") or ""),
        "city": str(location.get("city") or ""),
        "region": str(location.get("region") or ""),
        "recent_roles": unique_strings(experience.get("recent_roles") or []),
        "specialties": unique_strings(experience.get("specialties") or []),
        "total_years": experience.get("total_years"),
        "seniority": str(experience.get("seniority") or UNKNOWN),
        "certifications": unique_strings(
            canonical["credentials"].get("certifications") or []
        ),
        "licenses": unique_strings(canonical["credentials"].get("licenses") or []),
        "credential_status": str(
            canonical["credentials"].get("credential_status") or UNKNOWN
        ),
        "phone_preference": str(preferences.get("phone_preference") or UNKNOWN),
        "availability": str(preferences.get("availability") or UNKNOWN),
        "schedule": unique_strings(preferences.get("schedule") or []),
        "negative_constraints": unique_strings(
            constraints.get("negative_constraints") or []
        ),
    }


def normalize_identity_free_profile_input(raw_input, input_style):
    """Build review material directly, without ever selecting a profile identity."""

    raw_input = str(raw_input or "")
    text = profile_normalizer.normalize_profile_text(raw_input)
    languages = profile_normalizer.detect_profile_languages(raw_input)
    location = profile_normalizer.detect_location(text)
    education = profile_normalizer.detect_education(text)
    credentials = profile_normalizer.detect_credentials(text)
    experience = profile_normalizer.detect_experience(text)
    domains = profile_normalizer.detect_domains(text)
    skills = profile_normalizer.detect_skills(text, domains, input_style=input_style)
    preferences = profile_normalizer.detect_preferences(text, domains)
    constraints = profile_normalizer.detect_constraints(text)
    signals = profile_normalizer.signals_for_domains(domains, skills, languages)
    missing_fields = profile_normalizer.missing_fields_for_baseline(
        languages,
        location,
        credentials,
        experience,
    )
    ambiguous_fields = profile_normalizer.ambiguous_fields_for_baseline(
        text,
        input_style,
        languages,
    )
    canonical = {
        "schema_version": SCHEMA_VERSION,
        "identity": {
            "display_name": _identity_free_display_name(
                raw_input,
                experience=experience,
                skills=skills,
                domains=domains,
                languages=languages,
            ),
            "source_inputs": [{"type": input_style}],
        },
        "languages": languages,
        "location": location,
        "education": education,
        "credentials": credentials,
        "experience": experience,
        "skills": profile_normalizer.skills_block(skills),
        "preferences": preferences,
        "constraints": constraints,
        "derived_matcher_signals": {
            "signals": signals,
            "derived_domains": domains,
            "derived_target_work_types": preferences["target_opportunity_types"],
            "avoid_keywords": constraints["avoid_keywords"],
        },
        "matcher_compatible_profile": {},
        "provenance": {
            "extracted_from": input_style,
            "evidence_snippets": [raw_input.strip()],
            "original_text": raw_input.strip(),
            "confidence": "low" if input_style == "messy_sparse_input" else "medium",
            "missing_fields": missing_fields,
            "ambiguous_fields": ambiguous_fields,
        },
    }
    canonical["matcher_compatible_profile"] = _identity_free_matcher_projection(
        canonical
    )
    canonical["provenance"]["field_sources"] = field_sources_for_profile(
        canonical,
        PROFILE_SOURCE_PARSED_TEXT,
        explicit=False,
    )
    return IdentityFreeCanonicalProfileV1.from_mapping(canonical)


def apply_identity_free_profile_review(profile, updates):
    """Apply authoritative review updates while identity remains absent."""

    if type(profile) is not IdentityFreeCanonicalProfileV1:
        raise ValueError("invalid_identity_free_profile_draft")
    original = profile.to_mapping()
    canonical = deepcopy(original)

    location = canonical["location"]
    country = normalize_country(canonical_review.text(updates.get("country")), allow_missing=True)
    eligible_countries = [
        normalize_country(value)
        for value in canonical_review.string_list(updates.get("eligible_countries"))
    ]
    location.update(
        {
            "country": country,
            "region": canonical_review.text(updates.get("region")),
            "city": canonical_review.text(updates.get("city")),
            "residence": country,
            "work_authorization": canonical_review.text(
                updates.get("work_authorization")
            )
            or UNKNOWN,
            "eligible_countries": eligible_countries,
            "remote_eligibility": "explicit" if updates.get("remote") else "unknown",
            "restrictions": canonical_review.string_list(
                updates.get("geographic_restrictions")
            ),
            "geographic_work_restrictions": canonical_review.string_list(
                updates.get("geographic_restrictions")
            ),
        }
    )
    canonical["languages"] = canonical_review.reviewed_languages(
        updates.get("languages") or []
    )

    education = canonical["education"]
    level = canonical_review.text(updates.get("education_level")) or "not_specified"
    if level not in EDUCATION_LEVELS:
        raise ValueError("Choose a supported education level.")
    education.update(
        {
            "education_level": level,
            "degrees": canonical_review.string_list(updates.get("degrees")),
            "fields_or_domains": canonical_review.string_list(
                updates.get("education_fields")
            ),
            "institutions": canonical_review.string_list(updates.get("institutions")),
            "completion_status": canonical_review.text(updates.get("education_status"))
            or UNKNOWN,
        }
    )

    credentials = canonical["credentials"]
    credential_status = canonical_review.text(updates.get("credential_status")) or UNKNOWN
    if credential_status not in CREDENTIAL_STATUSES:
        raise ValueError("Choose a supported credential status.")
    credentials.update(
        {
            "certifications": canonical_review.string_list(updates.get("certifications")),
            "licenses": canonical_review.string_list(updates.get("licenses")),
            "jurisdictions": canonical_review.string_list(updates.get("jurisdictions")),
            "security_clearances": canonical_review.string_list(
                updates.get("security_clearances")
            ),
            "credential_status": credential_status,
        }
    )

    experience = canonical["experience"]
    experience.update(
        {
            "total_years": canonical_review.optional_years(updates.get("total_years")),
            "seniority": canonical_review.text(updates.get("seniority")) or UNKNOWN,
            "recent_roles": canonical_review.string_list(updates.get("job_titles")),
            "job_titles": canonical_review.string_list(updates.get("job_titles")),
            "occupational_families": canonical_review.string_list(
                updates.get("occupational_families")
            ),
            "professional_domains": canonical_review.string_list(
                updates.get("professional_domains")
            ),
            "industries": canonical_review.string_list(updates.get("industries")),
            "contribution_type": canonical_review.text(updates.get("contribution_type"))
            or UNKNOWN,
            "specialties": canonical_review.string_list(updates.get("specialties")),
        }
    )

    skills = canonical_review.string_list(updates.get("skills"))
    canonical["skills"] = {
        "normalized": skills,
        "free_text_labels": skills,
        "entries": [
            {
                "skill": skill,
                "evidence": [],
                "confidence": "high",
                "provenance": PROFILE_SOURCE_USER_CORRECTION,
            }
            for skill in skills
        ],
        "technical": canonical_review.string_list(updates.get("technical_skills")),
        "software_tools": canonical_review.string_list(updates.get("software_tools")),
        "writing_research": canonical_review.string_list(
            updates.get("writing_research_skills")
        ),
        "administrative_support": canonical_review.string_list(
            updates.get("administrative_support_skills")
        ),
        "domain_specific": canonical_review.string_list(
            updates.get("domain_specific_skills")
        ),
    }

    preferences = canonical["preferences"]
    employment_types = canonical_review.reviewed_enum_list(
        updates.get("employment_types"),
        canonical_review.EMPLOYMENT_TYPES,
        "employment type",
    )
    target_types = canonical_review.string_list(updates.get("target_opportunity_types"))
    remote = bool(updates.get("remote"))
    flexible = bool(updates.get("flexible"))
    preferences.update(
        {
            "remote": remote,
            "flexible": flexible,
            "employment_types": employment_types,
            "synchronous_preference": canonical_review.reviewed_enum(
                updates.get("synchronous_preference"),
                canonical_review.SYNCHRONOUS_PREFERENCES,
                "synchronous preference",
            ),
            "phone_preference": canonical_review.reviewed_enum(
                updates.get("phone_preference"),
                canonical_review.PHONE_PREFERENCES,
                "phone preference",
            ),
            "schedule": canonical_review.reviewed_enum_list(
                updates.get("schedule"),
                canonical_review.SCHEDULE_PREFERENCES,
                "schedule preference",
            ),
            "availability": canonical_review.reviewed_enum(
                updates.get("availability"),
                canonical_review.AVAILABILITY_STATUSES,
                "availability",
            ),
            "target_opportunity_types": target_types,
            "preferred_task_types": target_types,
            "work_preferences": unique_strings(
                employment_types
                + (["remote"] if remote else [])
                + (["flexible"] if flexible else [])
            ),
        }
    )

    constraints = canonical["constraints"]
    hard = canonical_review.string_list(updates.get("hard_constraints"))
    if updates.get("no_degree"):
        hard.append("no college degree")
    if updates.get("no_experience"):
        hard.append("no prior experience")
    if updates.get("no_specialized_credentials"):
        hard.append("no specialized credentials")
    excluded_domains = canonical_review.string_list(updates.get("excluded_domains"))
    accessibility = canonical_review.string_list(updates.get("accessibility_constraints"))
    constraints.update(
        {
            "hard_constraints": unique_strings(hard),
            "soft_preferences": canonical_review.string_list(
                updates.get("soft_preferences")
            ),
            "avoid_keywords": unique_strings(
                canonical_review.string_list(updates.get("avoid_keywords"))
                + excluded_domains
            ),
            "negative_constraints": unique_strings(excluded_domains + accessibility),
            "excluded_domains": excluded_domains,
            "accessibility_constraints": accessibility,
        }
    )

    domains = unique_strings(
        education["fields_or_domains"] + experience["professional_domains"]
    )
    signals = profile_normalizer.signals_for_domains(
        domains,
        skills,
        canonical["languages"],
    )
    canonical["derived_matcher_signals"] = {
        "signals": signals,
        "derived_domains": domains,
        "derived_target_work_types": target_types,
        "avoid_keywords": constraints["avoid_keywords"],
    }
    provenance = canonical["provenance"]
    canonical["identity"]["display_name"] = _identity_free_display_name(
        provenance.get("original_text"),
        experience=experience,
        skills=skills,
        domains=domains,
        languages=canonical["languages"],
    )
    display_name_changed = (
        canonical["identity"]["display_name"]
        != original["identity"]["display_name"]
    )
    provenance["reviewed"] = True
    provenance["missing_fields"] = canonical_review.reviewed_missing_fields(canonical)
    provenance["ambiguous_fields"] = []
    existing_sources = {
        path: detail
        for path, detail in (provenance.get("field_sources") or {}).items()
        if path.startswith("identity.")
    }
    confirmed_sources = field_sources_for_profile(
        canonical,
        PROFILE_SOURCE_USER_CONFIRMATION,
        explicit=True,
    )
    changed_roots = {
        root
        for root in (
            "languages",
            "location",
            "education",
            "credentials",
            "experience",
            "skills",
            "preferences",
            "constraints",
        )
        if canonical.get(root) != original.get(root)
    }
    existing_sources.update(
        {
            path: (
                {"source": PROFILE_SOURCE_USER_CORRECTION, "explicit": True}
                if path.split(".", 1)[0].split("[", 1)[0] in changed_roots
                else detail
            )
            for path, detail in confirmed_sources.items()
            if not path.startswith("identity.")
        }
    )
    if display_name_changed:
        existing_sources["identity.display_name"] = {
            "source": PROFILE_SOURCE_USER_CORRECTION,
            "explicit": True,
        }
    provenance["field_sources"] = existing_sources
    canonical["matcher_compatible_profile"] = _identity_free_matcher_projection(
        canonical
    )
    return IdentityFreeCanonicalProfileV1.from_mapping(canonical)


def _reviewed_confirmation_digest(run, reviewed_profile, updates):
    if type(run) is not MatchRun or type(reviewed_profile) is not IdentityFreeCanonicalProfileV1:
        raise ValueError("invalid_profile_confirmation_digest")
    reviewed_inputs = json.dumps(
        {
            "input_style": run.input_style,
            "normalized_updates": updates,
            "raw_input": run.raw_input,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    preimage = (
        len(reviewed_profile.canonical_bytes).to_bytes(8, "big")
        + reviewed_profile.canonical_bytes
        + len(reviewed_inputs).to_bytes(8, "big")
        + reviewed_inputs
    )
    return hashlib.sha256(preimage).hexdigest()


def _confirmation_authentication_digest(authentication_input):
    if type(authentication_input) is not tuple:
        raise ValueError("invalid_profile_confirmation_authentication")
    header_items = persistent_profile_browser_security._validated_header_items(
        authentication_input
    )
    if header_items is None:
        raise ValueError("invalid_profile_confirmation_authentication")
    if any(
        ord(character) < 32 or ord(character) == 127
        for name, value in header_items
        for character in name + value
    ):
        raise ValueError("invalid_profile_confirmation_authentication")
    hosts = persistent_profile_browser_security._header_values(header_items, "host")
    if len(hosts) != 1 or not persistent_profile_browser_security._trusted_host_headers(
        header_items,
        hosts[0],
    ):
        raise ValueError("invalid_profile_confirmation_authentication")
    session_token, session_valid = persistent_profile_browser_security._security_cookie(
        header_items,
        persistent_profile_browser_security.SESSION_COOKIE_NAME,
        persistent_profile_browser_security._OPAQUE_CREDENTIAL,
    )
    csrf_secret, csrf_valid = persistent_profile_browser_security._security_cookie(
        header_items,
        persistent_profile_browser_security.SESSION_CSRF_COOKIE_NAME,
        persistent_profile_browser_security._OPAQUE_CREDENTIAL,
    )
    if not session_valid or not csrf_valid:
        raise ValueError("invalid_profile_confirmation_authentication")
    session_credential = browser_session_security._extract_session_credential(
        header_items
    )
    if session_credential is None:
        raise ValueError("invalid_profile_confirmation_authentication")
    try:
        gateway_session_token = session_credential.consume()
    except Exception as exc:
        exc = None
        raise ValueError("invalid_profile_confirmation_authentication") from None
    if type(gateway_session_token) is not str or not secrets.compare_digest(
        gateway_session_token,
        session_token,
    ):
        raise ValueError("invalid_profile_confirmation_authentication")
    material = json.dumps(
        {
            "host": hosts[0],
            "session_token": session_token,
            "csrf_secret": csrf_secret,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _confirmation_identity(
    run,
    original_digest,
    reviewed_digest,
    authentication_input,
):
    material = json.dumps(
        {
            "match_run_id": run.match_run_id,
            "original_draft_digest": original_digest,
            "request_authentication_digest": _confirmation_authentication_digest(
                authentication_input
            ),
            "review_token": run.review_token,
            "reviewed_request_digest": reviewed_digest,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _bind_identity_free_profile_for_legacy_matching(profile, owner_profile_id):
    if (
        type(profile) is not IdentityFreeCanonicalProfileV1
        or type(owner_profile_id) is not str
        or not owner_profile_id
        or owner_profile_id != owner_profile_id.strip()
    ):
        raise ValueError("invalid_legacy_matching_profile_identity")
    canonical = profile.to_mapping()
    canonical["identity"]["profile_id"] = owner_profile_id
    canonical["matcher_compatible_profile"]["profile_id"] = owner_profile_id
    field_sources = canonical["provenance"]["field_sources"]
    identity_source = field_sources.get("identity.display_name")
    if type(identity_source) is not dict:
        raise ValueError("invalid_legacy_matching_profile_provenance")
    field_sources["identity.profile_id"] = deepcopy(identity_source)
    validate_canonical_profile(canonical)
    return canonical


def create_match_run(
    form,
    registry,
    demo_mode=False,
    *,
    confirmed_profile_artifact_sink=None,
    completed_profile_confirmation_authenticator=None,
    authentication_input=None,
):
    if "form_action" in form:
        return confirm_profile_review(
            form,
            registry,
            confirmed_profile_artifact_sink=confirmed_profile_artifact_sink,
            completed_profile_confirmation_authenticator=(
                completed_profile_confirmation_authenticator
            ),
            authentication_input=authentication_input,
            _allow_matching=True,
        )

    validate_profile_input_form(form)
    edit_run_id = strict_optional_form_value(form, "edit_run_id")
    parent_run = require_match_run(registry, edit_run_id) if edit_run_id else None
    edit_review_token = strict_optional_form_value(form, "edit_review_token")
    if parent_run:
        if not edit_review_token or not secrets.compare_digest(
            edit_review_token, parent_run.review_token
        ):
            raise ActionError("This profile edit is not authorized.", HTTPStatus.FORBIDDEN)
    elif edit_review_token:
        raise MalformedProfileReview()
    demo_persona = first_value(form, "sample") if demo_mode else ""
    sample = PREVIEW_SAMPLES.get(demo_persona) if demo_persona else None
    if parent_run:
        owner_profile_id = parent_run.owner_profile_id
        demo_persona = parent_run.demo_persona or ""
        raw_input = first_value(form, "input_text")
        input_style = first_value(form, "input_style") or parent_run.input_style
    elif sample:
        owner_profile_id = sample["owner_profile_id"]
        raw_input = sample["text"]
        input_style = sample["style"]
    else:
        demo_persona = ""
        owner_profile_id = NORMAL_OWNER_PROFILE_ID
        raw_input = first_value(form, "input_text")
        input_style = first_value(form, "input_style") or "short_paragraph"
    if not raw_input:
        raise ActionError("Add a short background before finding matches.")
    if input_style not in profile_preview.INPUT_STYLES:
        input_style = "short_paragraph"
    if confirmed_profile_artifact_sink is None:
        require_owner_profile(owner_profile_id)
    identity_free_profile = normalize_identity_free_profile_input(
        raw_input,
        input_style,
    )
    return registry.create(
        owner_profile_id=owner_profile_id,
        raw_input=raw_input,
        input_style=input_style,
        demo_persona=demo_persona or None,
        recommendation_context=None,
        canonical_profile=identity_free_profile,
        profile_confirmed=False,
    )


def confirm_profile_review(
    form,
    registry,
    *,
    confirmed_profile_artifact_sink,
    completed_profile_confirmation_authenticator=None,
    authentication_input,
    _allow_matching=False,
):
    """Validate and confirm one existing reviewed draft at a bounded boundary."""

    run, updates = validate_profile_review_submission(form, registry)
    if run.canonical_profile is None:
        raise ActionError("This profile review has expired. Start again.")
    try:
        canonical = apply_identity_free_profile_review(run.canonical_profile, updates)
    except ValueError as exc:
        raise ActionError(str(exc)) from exc
    if confirmed_profile_artifact_sink is None and _allow_matching is True:
        context = build_current_structured_preview_context(
            canonical,
            run.owner_profile_id,
            run.raw_input,
            run.input_style,
            PREVIEW_MATCH_LIMIT,
            preview_data_signature(),
        )
        confirmed = registry.confirm_profile(run.match_run_id, canonical, context)
        if confirmed is None:
            raise ActionError(
                "That match run is unknown or has expired. Start again.",
                HTTPStatus.GONE,
            )
        return confirmed
    if not callable(confirmed_profile_artifact_sink):
        raise ActionError(
            "Profile confirmation is temporarily unavailable.",
            HTTPStatus.SERVICE_UNAVAILABLE,
        )

    original_digest = profile_draft_fingerprint(run.canonical_profile)
    reviewed_digest = _reviewed_confirmation_digest(run, canonical, updates)
    confirmation_identity = _confirmation_identity(
        run,
        original_digest,
        reviewed_digest,
        authentication_input,
    )
    acquisition = []
    claimed = None
    witness = None
    settled = False
    try:
        claimed = registry.acquire_profile_confirmation(
            original_run=run,
            original_draft_digest=original_digest,
            reviewed_request_digest=reviewed_digest,
            confirmation_identity=confirmation_identity,
            reviewed_profile=canonical,
            acquisition=acquisition,
        )
        if type(claimed) is _CompletedProfileConfirmationReplay:
            if not callable(completed_profile_confirmation_authenticator):
                raise ActionError(
                    "Profile confirmation is temporarily unavailable.",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
            try:
                authorized = completed_profile_confirmation_authenticator(
                    authentication_input=authentication_input,
                    authority_binding=claimed.authority_binding,
                )
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except Exception as exc:
                exc = None
                raise ActionError(
                    "Profile confirmation is temporarily unavailable.",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                ) from None
            if authorized is not True:
                raise ActionError(
                    "This profile confirmation is not authorized.",
                    HTTPStatus.FORBIDDEN,
                )
            completed = registry.replay_completed_profile_confirmation(claimed)
            if completed is None:
                raise ActionError(
                    "This profile draft is no longer current.",
                    HTTPStatus.FORBIDDEN,
                )
            return completed
        if type(claimed) is not _ProfileConfirmationLease:
            raise RuntimeError("invalid_profile_confirmation_claim")
        witness = _ConfirmationIssuanceWitness(claimed)
        offer = confirmed_profile_artifact_sink(
            reviewed_profile=claimed.reviewed_run.canonical_profile,
            raw_about_you=claimed.reviewed_run.raw_input,
            normalized_updates=updates,
            profile_confirmed=claimed.reviewed_run.profile_confirmed,
            authentication_input=authentication_input,
            _confirmation_identity=claimed.confirmation_identity,
            _confirmation_witness=witness,
            _confirmation_recovery_only=claimed.recovery_only,
        )
        if not _is_confirmed_profile_artifact_offer(offer):
            witness.mark_artifact_may_exist()
            raise ActionError(
                "Profile confirmation is temporarily unavailable.",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        result = witness.record_valid_offer(offer)
        settled = True
        return result
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except ActionError:
        raise
    except Exception as exc:
        exc = None
        raise ActionError(
            "Profile confirmation is temporarily unavailable.",
            HTTPStatus.SERVICE_UNAVAILABLE,
        ) from None
    finally:
        lease = claimed if type(claimed) is _ProfileConfirmationLease else None
        if lease is None and acquisition:
            candidate = acquisition[0]
            if type(candidate) is _ProfileConfirmationLease:
                lease = candidate
        if lease is not None and not settled:
            if witness is not None:
                recorded = witness.valid_offer
                if recorded is not None:
                    try:
                        witness.record_valid_offer(recorded)
                    except BaseException:
                        pass
                artifact_may_exist = witness.artifact_may_exist
            else:
                artifact_may_exist = lease.recovery_only
            try:
                registry.fail_profile_confirmation(
                    lease,
                    definite_absence=not artifact_may_exist,
                )
            except BaseException:
                pass


def render_confirmed_profile_creation(offer):
    if not _is_confirmed_profile_artifact_offer(offer):
        raise ValueError("invalid_confirmed_profile_creation")
    artifact = html.escape(offer.artifact_reference, quote=True)
    csrf = html.escape(offer.csrf_proof, quote=True)
    return f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Create my persistent profile | Wahojobs</title>
</head>
<body><main>
  <section>
    <h1>Your reviewed profile is ready</h1>
    <p>Create the first persistent profile for this account using the details you confirmed.</p>
    <form method='post' action='/account/profile'>
      <input type='hidden' name='artifact' value='{artifact}'>
      <input type='hidden' name='csrf' value='{csrf}'>
      <button type='submit'>Create my profile</button>
    </form>
  </section>
</main></body>
</html>"""


def _is_confirmed_profile_artifact_offer(value):
    from wahojobs.persistent_profile_creation import ConfirmedProfileArtifactOffer
    from wahojobs.persistent_profile_corrections import (
        ConfirmedProfileCorrectionArtifactOffer,
    )

    return type(value) in {
        ConfirmedProfileArtifactOffer,
        ConfirmedProfileCorrectionArtifactOffer,
    }


def profile_review_updates_from_form(form, language_slots):
    languages = []
    for index in range(language_slots):
        language = strict_review_value(form, f"language_{index}")
        if not language:
            continue
        languages.append(
            {
                "language": language,
                "proficiency": strict_review_value(form, f"language_proficiency_{index}")
                or "unspecified",
                "locale": strict_review_value(form, f"language_locale_{index}"),
            }
        )
    list_fields = (
        "eligible_countries",
        "geographic_restrictions",
        "degrees",
        "education_fields",
        "institutions",
        "certifications",
        "licenses",
        "jurisdictions",
        "security_clearances",
        "job_titles",
        "occupational_families",
        "professional_domains",
        "industries",
        "specialties",
        "skills",
        "technical_skills",
        "software_tools",
        "writing_research_skills",
        "administrative_support_skills",
        "domain_specific_skills",
        "employment_types",
        "schedule",
        "target_opportunity_types",
        "hard_constraints",
        "soft_preferences",
        "avoid_keywords",
        "excluded_domains",
        "accessibility_constraints",
    )
    updates = {field: strict_review_value(form, field) for field in list_fields}
    for field in (
        "country",
        "region",
        "city",
        "work_authorization",
        "education_level",
        "education_status",
        "credential_status",
        "total_years",
        "seniority",
        "contribution_type",
        "synchronous_preference",
        "phone_preference",
        "availability",
    ):
        updates[field] = strict_review_value(form, field)
    updates.update(
        {
            "languages": languages,
            "remote": strict_review_checkbox(form, "remote"),
            "flexible": strict_review_checkbox(form, "flexible"),
            "no_degree": strict_review_checkbox(form, "no_degree"),
            "no_experience": strict_review_checkbox(form, "no_experience"),
            "no_specialized_credentials": strict_review_checkbox(
                form, "no_specialized_credentials"
            ),
        }
    )
    return updates


def profile_review_form_fields(canonical, match_run_id, review_token):
    """Return the default submitted values for one rendered review form."""
    location = canonical.get("location") or {}
    education = canonical.get("education") or {}
    credentials = canonical.get("credentials") or {}
    experience = canonical.get("experience") or {}
    skills = canonical.get("skills") or {}
    preferences = canonical.get("preferences") or {}
    constraints = canonical.get("constraints") or {}
    fields = {
        "form_action": "confirm_profile",
        "edit_run_id": match_run_id,
        "review_token": review_token,
        "schema_version": SCHEMA_VERSION,
        "profile_draft_fingerprint": profile_draft_fingerprint(canonical),
        "credentials_confirmed": "1",
        "country": location.get("country") or "",
        "region": location.get("region") or "",
        "city": location.get("city") or "",
        "work_authorization": location.get("work_authorization") or "",
        "eligible_countries": review_csv(location.get("eligible_countries")),
        "geographic_restrictions": review_csv(
            location.get("geographic_work_restrictions") or location.get("restrictions")
        ),
        "education_level": education.get("education_level") or "not_specified",
        "degrees": review_csv(education.get("degrees")),
        "education_fields": review_csv(education.get("fields_or_domains")),
        "institutions": review_csv(education.get("institutions")),
        "education_status": education.get("completion_status") or "unknown",
        "credential_status": credentials.get("credential_status") or "unknown",
        "certifications": review_csv(credentials.get("certifications")),
        "licenses": review_csv(credentials.get("licenses")),
        "jurisdictions": review_csv(credentials.get("jurisdictions")),
        "security_clearances": review_csv(credentials.get("security_clearances")),
        "total_years": "" if experience.get("total_years") is None else str(experience["total_years"]),
        "seniority": experience.get("seniority") or "unknown",
        "job_titles": review_csv(experience.get("job_titles") or experience.get("recent_roles")),
        "occupational_families": review_csv(experience.get("occupational_families")),
        "professional_domains": review_csv(experience.get("professional_domains")),
        "industries": review_csv(experience.get("industries")),
        "contribution_type": experience.get("contribution_type") or "unknown",
        "specialties": review_csv(experience.get("specialties")),
        "skills": review_csv(skills.get("normalized")),
        "technical_skills": review_csv(skills.get("technical")),
        "software_tools": review_csv(skills.get("software_tools")),
        "writing_research_skills": review_csv(skills.get("writing_research")),
        "administrative_support_skills": review_csv(skills.get("administrative_support")),
        "domain_specific_skills": review_csv(skills.get("domain_specific")),
        "employment_types": review_csv(preferences.get("employment_types")),
        "synchronous_preference": preferences.get("synchronous_preference") or "unknown",
        "phone_preference": preferences.get("phone_preference") or "unknown",
        "schedule": review_csv(preferences.get("schedule")),
        "availability": preferences.get("availability") or "unknown",
        "target_opportunity_types": review_csv(preferences.get("target_opportunity_types")),
        "hard_constraints": review_csv(constraints.get("hard_constraints")),
        "soft_preferences": review_csv(constraints.get("soft_preferences")),
        "avoid_keywords": review_csv(constraints.get("avoid_keywords")),
        "excluded_domains": review_csv(constraints.get("excluded_domains")),
        "accessibility_constraints": review_csv(constraints.get("accessibility_constraints")),
    }
    if preferences.get("remote") is True:
        fields["remote"] = "1"
    if preferences.get("flexible") is True:
        fields["flexible"] = "1"
    hard_constraints = constraints.get("hard_constraints") or []
    if education.get("education_level") == "no_degree" or "no college degree" in hard_constraints:
        fields["no_degree"] = "1"
    if "no prior experience" in hard_constraints:
        fields["no_experience"] = "1"
    if any("credential" in str(value).lower() for value in hard_constraints):
        fields["no_specialized_credentials"] = "1"
    language_slots = profile_review_language_slots(canonical)
    languages = list(canonical.get("languages") or [])
    for index in range(language_slots):
        language = languages[index] if index < len(languages) else {}
        fields[f"language_{index}"] = language.get("language") or ""
        proficiency = language.get("proficiency") or "unspecified"
        if proficiency not in LANGUAGE_PROFICIENCIES:
            proficiency = "professional" if proficiency == "advanced" else "unspecified"
        fields[f"language_proficiency_{index}"] = proficiency
        fields[f"language_locale_{index}"] = language.get("locale") or ""
    return fields


PROFILE_REVIEW_TEXT_FIELDS = {
    "country", "region", "city", "work_authorization", "eligible_countries",
    "geographic_restrictions", "education_level", "degrees", "education_fields",
    "institutions", "education_status", "credential_status", "certifications",
    "licenses", "jurisdictions", "security_clearances", "total_years", "seniority",
    "job_titles", "occupational_families", "professional_domains", "industries",
    "contribution_type", "specialties", "skills", "technical_skills", "software_tools",
    "writing_research_skills", "administrative_support_skills", "domain_specific_skills",
    "employment_types", "synchronous_preference", "phone_preference", "schedule",
    "availability", "target_opportunity_types", "hard_constraints", "soft_preferences",
    "avoid_keywords", "excluded_domains", "accessibility_constraints",
}
PROFILE_REVIEW_CHECKBOX_FIELDS = {
    "remote", "flexible", "no_degree", "no_experience",
    "no_specialized_credentials", "credentials_confirmed",
}
PROFILE_REVIEW_CONTROL_FIELDS = {
    "form_action", "edit_run_id", "review_token", "schema_version",
    "profile_draft_fingerprint",
}


def profile_review_language_slots(canonical):
    return min(8, max(4, len(canonical.get("languages") or [])))


def validate_profile_review_submission(form, registry):
    if strict_review_value(form, "form_action") != "confirm_profile":
        raise MalformedProfileReview()
    run_id = strict_review_value(form, "edit_run_id")
    if not run_id:
        raise ActionError("Missing match run. Return to Matches and try again.")
    run = registry.confirmation_draft(run_id)
    if run is None:
        raise ActionError(
            "That match run is unknown or has expired. Start a new search.",
            HTTPStatus.GONE,
        )
    if run.canonical_profile is None:
        raise ActionError("This profile review has expired. Start again.", HTTPStatus.GONE)
    language_slots = profile_review_language_slots(run.canonical_profile)
    language_fields = {
        f"{prefix}_{index}"
        for index in range(language_slots)
        for prefix in ("language", "language_proficiency", "language_locale")
    }
    allowed = (
        PROFILE_REVIEW_TEXT_FIELDS
        | PROFILE_REVIEW_CHECKBOX_FIELDS
        | PROFILE_REVIEW_CONTROL_FIELDS
        | language_fields
    )
    unsupported = sorted(set(form) - allowed)
    if unsupported:
        raise MalformedProfileReview()
    required = PROFILE_REVIEW_TEXT_FIELDS | PROFILE_REVIEW_CONTROL_FIELDS | language_fields
    for field in required:
        strict_review_value(form, field)
    for field in PROFILE_REVIEW_CHECKBOX_FIELDS:
        strict_review_checkbox(form, field)
    if strict_review_value(form, "schema_version") != SCHEMA_VERSION:
        raise MalformedProfileReview("Unsupported profile schema version.")
    review_token = strict_review_value(form, "review_token")
    if not secrets.compare_digest(review_token, run.review_token):
        raise ActionError("This profile review is not authorized.", HTTPStatus.FORBIDDEN)
    fingerprint = strict_review_value(form, "profile_draft_fingerprint")
    if not _is_sha256_digest(fingerprint):
        raise MalformedProfileReview()
    if not secrets.compare_digest(
        fingerprint, profile_draft_fingerprint(run.canonical_profile)
    ):
        raise ActionError("This profile draft is no longer current.", HTTPStatus.FORBIDDEN)
    if not strict_review_checkbox(form, "credentials_confirmed"):
        raise ActionError(
            "Confirm that the licenses and certifications shown are accurate."
        )
    return run, profile_review_updates_from_form(form, language_slots)


def strict_review_value(form, field):
    values = form.get(field)
    if type(values) is not list or len(values) != 1:
        raise MalformedProfileReview()
    value = values[0]
    if type(value) is not str or value != value.strip():
        raise MalformedProfileReview()
    return value


def strict_review_checkbox(form, field):
    if field not in form:
        return False
    if strict_review_value(form, field) != "1":
        raise MalformedProfileReview()
    return True


def strict_optional_form_value(form, field):
    if field not in form:
        return ""
    values = form[field]
    if type(values) is not list or len(values) != 1 or type(values[0]) is not str:
        raise MalformedProfileReview()
    if values[0] != values[0].strip():
        raise MalformedProfileReview()
    return values[0]


def validate_profile_input_form(form):
    allowed = {"input_text", "input_style", "sample", "edit_run_id", "edit_review_token"}
    if set(form) - allowed:
        raise MalformedProfileReview()
    for field in form:
        strict_optional_form_value(form, field)


def resolve_tracker_match_run(params, registry, demo_mode=False):
    run_id = first_value(params, "run")
    if run_id:
        return require_match_run(registry, run_id)
    demo_persona = first_value(params, "persona") if demo_mode else ""
    sample = PREVIEW_SAMPLES.get(demo_persona) if demo_persona else None
    owner_profile_id = sample["owner_profile_id"] if sample else NORMAL_OWNER_PROFILE_ID
    require_owner_profile(owner_profile_id)
    return registry.create(
        owner_profile_id=owner_profile_id,
        raw_input="",
        input_style="short_paragraph",
        demo_persona=demo_persona or None,
        recommendation_context=None,
    )


def require_match_run(registry, run_id):
    if not run_id:
        raise ActionError("Missing match run. Return to Matches and try again.")
    run = registry.get(run_id)
    if run is None:
        raise ActionError(
            "That match run is unknown or has expired. Start a new search.",
            HTTPStatus.GONE,
        )
    return run


def opportunity_key(source, title, url):
    identity = "\x1f".join(
        re.sub(r"\s+", " ", str(value or "").strip().lower())
        for value in (source, title, url)
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def match_opportunity_key(match):
    return opportunity_key(match.get("source"), match.get("display_title"), match.get("url"))


def iter_match_run_opportunities(run):
    context = run.recommendation_context or {}
    for matches in (context.get("matches") or {}).values():
        for match in matches:
            yield match


def resolve_run_opportunity(run, requested_key):
    if not requested_key:
        raise ActionError("Missing opportunity reference.")
    for match in iter_match_run_opportunities(run):
        if match_opportunity_key(match) == requested_key:
            if browser_match_rejection_reasons(match):
                raise ActionError(
                    "That opportunity is not safe to open or track from this match run.",
                    HTTPStatus.FORBIDDEN,
                )
            return {
                "source": match.get("source") or "",
                "title": match.get("display_title") or match.get("title") or "",
                "url": match.get("url") or "",
            }
    raise ActionError(
        "That opportunity is not available in this match run.",
        HTTPStatus.FORBIDDEN,
    )


def handle_action(form, run):
    action = first_value(form, "action")
    if action not in ACTION_STATUSES:
        raise ActionError(f"Unknown action: {action}")

    pipeline_id = first_value(form, "pipeline_item_id")
    idempotency_key = required_form_value(form, "idempotency_key")
    note = action_note(action)
    owner_profile_id = run.owner_profile_id

    with get_connection() as conn:
        try:
            pipeline_records.require_pipeline_state_schema(conn)
            require_browser_pipeline_ready(conn)
            call = {
                "action": action,
                "owner_profile_id": owner_profile_id,
                "idempotency_key": idempotency_key,
                "match_run_id": run.match_run_id,
                "note": note,
            }
            if action == "remind_later":
                reminder_date = (
                    datetime.now(timezone.utc).date() + timedelta(days=7)
                ).isoformat()
                call["reminder_at"] = f"{reminder_date}T00:00:00+00:00"
            if pipeline_id:
                persisted = conn.execute(
                    "SELECT profile_id FROM user_pipeline_items WHERE pipeline_item_id = ?",
                    (pipeline_id,),
                ).fetchone()
                if persisted is None:
                    raise ActionError("That tracker item was not found.", HTTPStatus.NOT_FOUND)
                if persisted["profile_id"] != owner_profile_id:
                    raise ActionError(
                        "That tracker item is unavailable for this profile.",
                        HTTPStatus.FORBIDDEN,
                    )
                record = normalized_browser_record(
                    pipeline_records.load_pipeline_record(
                        conn,
                        pipeline_id,
                        owner_profile_id=owner_profile_id,
                        mutation_grade=True,
                    )
                )
                requested_opportunity = first_value(form, "opportunity_key")
                if requested_opportunity:
                    opportunity = resolve_run_opportunity(run, requested_opportunity)
                    if not same_record_opportunity(record, opportunity):
                        raise ActionError(
                            "That action does not match this opportunity.",
                            HTTPStatus.FORBIDDEN,
                        )
                expected_version = required_expected_version(form)
                orchestrator_action = normalized_browser_action(action, form, record)
                call.update(
                    action=orchestrator_action,
                    pipeline_item_id=pipeline_id,
                    expected_version=expected_version,
                )
            else:
                if "expected_version" in form:
                    raise ActionError(
                        "A new opportunity must not submit a state version."
                    )
                if action not in {"save", "applied", "not_interested"}:
                    raise ActionError("That action requires a tracked opportunity.")
                opportunity = resolve_run_opportunity(
                    run, first_value(form, "opportunity_key")
                )
                call.update(
                    source=opportunity["source"],
                    title=opportunity["title"],
                    url=opportunity["url"],
                )
            operation = pipeline_actions.perform_pipeline_action(conn, **call)
            loaded_record = pipeline_records.load_pipeline_record(
                    conn,
                    operation.pipeline_item["pipeline_item_id"],
                    owner_profile_id=owner_profile_id,
                    mutation_grade=True,
                )
            record = normalized_browser_record(loaded_record)
            all_records = [
                normalized_browser_record(current)
                for current in pipeline_records.list_pipeline_records(
                    conn, owner_profile_id, mutation_grade=True
                )
            ]
        except ActionError:
            raise
        except pipeline_state.OwnershipError as exc:
            raise ActionError(
                "That tracker item is unavailable for this profile.",
                HTTPStatus.FORBIDDEN,
            ) from exc
        except (pipeline_state.StaleStateVersion, pipeline_state.IdempotencyConflict) as exc:
            message = (
                "This item changed since the page was loaded. Refresh and try again."
                if isinstance(exc, pipeline_state.StaleStateVersion)
                else "This action conflicts with an earlier request. Refresh and try again."
            )
            raise ActionError(message, HTTPStatus.CONFLICT) from exc
        except (pipeline_actions.UnresolvedLegacyWorkflow, pipeline_state.InvalidTransition) as exc:
            raise ActionError(str(exc), HTTPStatus.CONFLICT) from exc
        except (
            pipeline_records.PipelineRecordInvariant,
            pipeline_actions.PipelineInvariantError,
            pipeline_state.ProjectionNotInitialized,
        ) as exc:
            raise ActionError(
                "Pipeline state needs reconciliation before this action can continue.",
                HTTPStatus.SERVICE_UNAVAILABLE,
            ) from exc
        except pipeline_actions.PipelineActionValidationError as exc:
            raise ActionError(str(exc), HTTPStatus.BAD_REQUEST) from exc

    return {
        "message": (
            reminder_success_message(record["reminder_date"])
            if action == "remind_later"
            else action_success_message(action)
        ),
        "item": record,
        "source": record["source"],
        "title": record["title"],
        "url": record["url"],
        "replayed": operation.replayed,
        "all_records": all_records,
    }


def require_browser_pipeline_ready(conn):
    report = pipeline_reconciliation.reconcile_pipeline_state(conn)
    if report["blocking"]:
        raise pipeline_records.PipelineRecordInvariant(
            "Normalized pipeline state requires reconciliation."
        )


def require_normalized_browser_read_ready(conn):
    report = pipeline_reconciliation.reconcile_pipeline_state(conn)
    if not pipeline_reconciliation.is_safe_for_normalized_reads(report):
        raise pipeline_records.PipelineRecordInvariant(
            "Normalized pipeline state requires reconciliation."
        )


def required_expected_version(form):
    raw = action_form_value(form, "expected_version")
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)", raw, flags=re.ASCII):
        raise MalformedActionRequest()
    return int(raw)


def normalized_browser_action(action, form, record):
    resolution_mode = first_value(form, "resolution_mode")
    if resolution_mode:
        if action != "show_again" or resolution_mode != "as_saved":
            raise ActionError("Invalid workflow resolution request.")
        return "show_again_as_saved"
    return action


def same_record_opportunity(record, opportunity):
    return (
        record["source"] == opportunity["source"]
        and record["title"] == opportunity["title"]
        and record["url"] == (opportunity["url"] or "")
    )


def normalized_browser_record(record, *, normalized_state=None, compatibility=None):
    state = normalized_state or record.normalized_state
    if state is None:
        raise pipeline_records.PipelineRecordInvariant(
            "Pipeline item is missing normalized state."
        )
    workflow = state["workflow_status"]
    visibility = state["visibility"]
    status = (
        "not_interested"
        if visibility == "hidden"
        else workflow if workflow is not None else "workflow_unknown"
    )
    reminder_at = state.get("reminder_at")
    result = {
        "id": record.pipeline_item["id"],
        "pipeline_item_id": record.pipeline_item["pipeline_item_id"],
        "profile_id": record.persisted_owner["profile_id"],
        "source": record.opportunity["source"],
        "title": record.opportunity["title"],
        "url": record.opportunity["url"],
        "status": status,
        "workflow_status": workflow,
        "workflow_status_provenance": state["workflow_status_provenance"],
        "visibility": visibility,
        "reminder_at": reminder_at,
        "reminder_date": reminder_at[:10] if reminder_at else "",
        "state_version": state["version"],
        "status_date": (compatibility or record.compatibility)["status_date"] or "",
        "notes": record.display["notes"] or "",
        "user_priority": record.display["user_priority"] or "medium",
        "last_user_action": (compatibility or record.compatibility)["last_user_action"] or "",
        "updated_at": state.get("updated_at") or "",
        "match_score": None,
        "unresolved_workflow": workflow is None,
        "integrity_error": not record.diagnostics["mutation_grade"],
    }
    result["next_action"] = lightweight_next_action(result)
    result["match_key"] = preview_pipeline_match_key(result)
    return result


def build_card_index(matches, pipeline_records, tracked, explore_market=None):
    index = {
        "exact": {},
        "near": {},
        "fallback": {
            "live": "#best-matches",
            "new": "#new-matches",
            "evergreen": "#always-open",
            "pipeline": "#application-tracker",
        },
    }
    for bucket_name, bucket in matches.items():
        for match in bucket[:8]:
            record = demo.tracked_record_for_match(match, tracked)
            href = f"#{card_id_for_match(match, record)}"
            add_card_index_entry(
                index,
                match["source"],
                match["display_title"],
                href,
                bucket_name,
            )
    for record in pipeline_records:
        add_card_index_entry(
            index,
            record["source"],
            record["title"],
            f"#{card_id_for_record(record)}",
            "pipeline",
        )
    for bucket_name, bucket in (explore_market or {}).items():
        if bucket_name == "summary" or not isinstance(bucket, list):
            continue
        for match in bucket:
            record = demo.tracked_record_for_match(match, tracked)
            add_card_index_entry(
                index,
                match["source"],
                match["display_title"],
                f"#{card_id_for_explore_match(match, record)}",
                "explore",
            )
    return index


def add_card_index_entry(index, source, title, href, bucket_name):
    exact_key = source_title_key(source, title)
    near_key = source_near_title_key(source, title)
    index["exact"].setdefault(exact_key, href)
    index["near"].setdefault(near_key, href)
    index["fallback"].setdefault(bucket_name, href)


def action_href(action, card_index):
    source = action.get("source") or ""
    title = action.get("title") or ""
    exact_key = source_title_key(source, title)
    if exact_key in card_index["exact"]:
        return card_index["exact"][exact_key]
    near_key = source_near_title_key(source, title)
    if near_key in card_index["near"]:
        return card_index["near"][near_key]
    text = demo.normalize_text(action.get("action"))
    if "always-open" in text or "application" in text:
        return card_index["fallback"].get("evergreen", "#always-open")
    if "new match" in text:
        return card_index["fallback"].get("new", "#new-matches")
    if "assessment" in text or "watch" in text:
        return card_index["fallback"].get("pipeline", "#application-tracker")
    return card_index["fallback"].get("live", "#best-matches")


def card_id_for_match(match, record=None):
    if record:
        return card_id_for_record(record)
    return opportunity_card_id(
        match["source"],
        match["display_title"],
        match.get("url") or "",
    )


def card_id_for_explore_match(match, record=None):
    return "explore-" + card_id_for_match(match, record)


def card_id_for_record(record):
    stable_value = record.get("pipeline_item_id") or record.get("id") or record.get("url") or record["title"]
    return opportunity_card_id(record["source"], record["title"], str(stable_value))


def opportunity_card_id(source, title, stable_value):
    label = slugify(f"{source} {title}")[:72].strip("-")
    digest = hashlib.sha1(stable_value.encode("utf-8")).hexdigest()[:8]
    return f"opp-{label}-{digest}" if label else f"opp-{digest}"


def source_title_key(source, title):
    return (demo.normalize(source), demo.normalize(title))


def source_near_title_key(source, title):
    return (demo.normalize(source), demo.normalize_action_target(title))


def slugify(value):
    text = demo.normalize_text(value)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def visible_matches(matches, tracked):
    result = []
    for match in matches:
        record = demo.tracked_record_for_match(match, tracked)
        if record and record["status"] in MAIN_RECOMMENDATION_EXCLUDED_STATUSES:
            continue
        result.append(match)
    return result


def visible_actions(actions, tracked):
    result = []
    for action in actions:
        source = demo.normalize(action.get("source"))
        title = demo.normalize(action.get("title"))
        record = tracked["by_source_title"].get((source, title))
        if record and record["status"] in MAIN_RECOMMENDATION_EXCLUDED_STATUSES:
            continue
        result.append(action)
    return result


def load_profile_options():
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT profile_id, display_name
            FROM user_profiles
            ORDER BY display_name, profile_id
            """
        ).fetchall()


def load_lightweight_tracker_context(requested_profile_id):
    profiles = load_profile_options()
    available_ids = {profile["profile_id"] for profile in profiles}
    if requested_profile_id not in available_ids:
        raise ActionError(f"Unknown tracker owner: {requested_profile_id}.")
    profile_id = requested_profile_id
    display_name = profile_display_name(profiles, profile_id)
    with get_connection() as conn:
        require_normalized_browser_read_ready(conn)
        records = [
            normalized_browser_record(record)
            for record in pipeline_records.list_pipeline_records(
                conn, profile_id, mutation_grade=True
            )
        ]
    return {
        "profile": {
            "profile_id": profile_id,
            "display_name": display_name,
        },
        "profiles": profiles,
        "records": records,
    }


def normalize_dashboard_pipeline_context(context, profile_id):
    with get_connection() as conn:
        require_normalized_browser_read_ready(conn)
        normalized_records = [
            normalized_browser_record(record)
            for record in pipeline_records.list_pipeline_records(
                conn,
                profile_id,
                mutation_grade=True,
            )
        ]

    legacy_report = context["pipeline_report"]
    legacy_by_id = {
        record.get("pipeline_item_id"): record
        for record in legacy_report["records"]
    }
    records = [
        {
            **legacy_by_id.get(record["pipeline_item_id"], {}),
            **record,
        }
        for record in normalized_records
    ]
    pipeline_report = {
        **legacy_report,
        "records": records,
        "summary": pipeline_digest.summarize_records(records),
    }
    tracked = demo.build_tracked_index(records)
    action_plan = demo.build_demo_action_plan(
        context["profile"],
        pipeline_report,
        context["matches"],
        tracked,
        context["generated_at"].date().isoformat(),
    )
    return {
        **context,
        "pipeline_source": "SQLite normalized pipeline state",
        "pipeline_report": pipeline_report,
        "tracked": tracked,
        "next_actions": action_plan["primary"],
        "also_worth_reviewing": action_plan["secondary"],
        "daily_action_status": action_plan["daily_status"],
    }


def lightweight_next_action(record):
    if record.get("workflow_status") is None:
        if record.get("visibility") == "hidden":
            return "Hidden. The previous workflow stage is unknown."
        if record.get("reminder_at"):
            return "Workflow needs confirmation; your reminder is still active."
        return "Pipeline state needs reconciliation before another action."
    status = record["status"]
    labels = {
        "recommended": "Review this opportunity when you are ready.",
        "saved": "Saved for review; apply when it feels like a good fit.",
        "remind_later": "This will stay in your tracker until its reminder is due.",
        "applied": "Watch for an assessment or next-step message.",
        "waiting": "Waiting for an update from the source.",
        "assessment_invited": "Start the assessment when you have focused time.",
        "assessment_started": "Continue the assessment already in progress.",
        "assessment_completed": "Waiting for the assessment result or next step.",
        "accepted": "Accepted opportunity.",
        "active_worker": "Active work relationship.",
        "paid_task_received": "Paid task received.",
        "not_interested": "Hidden from recommendations until you show it again.",
        "rejected": "Closed after a rejection or selection decision.",
        "expired": "This job is no longer available.",
        "workflow_unknown": "Confirm whether this was saved or applied before continuing.",
    }
    return labels.get(status, "Review the current tracker status.")


def profile_display_name(profiles, profile_id):
    for profile in profiles:
        if profile["profile_id"] == profile_id:
            return profile["display_name"]
    return profile_id.replace("_", " ").title()


def render_dashboard(context, match_run_id, demo_mode=False, message=None, error=None):
    profile = context["profile"]
    pipeline_report = context["pipeline_report"]
    applicant_signals = context["applicant_signals"]
    matches = context["matches"]
    tracked = context["tracked"]
    visible_match_buckets = {
        key: visible_matches(bucket, tracked)
        for key, bucket in matches.items()
    }
    actions = visible_actions(context["next_actions"], tracked)
    secondary_actions = visible_actions(context.get("also_worth_reviewing", []), tracked)
    explore_market = context.get("explore_market", {})
    card_index = build_card_index(
        visible_match_buckets,
        pipeline_report["records"],
        tracked,
        explore_market=explore_market,
    )

    parts = [
        "<!doctype html>",
        "<html lang='en'>",
        "<head>",
        "<meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>Wahojobs - {e(profile['display_name'])}</title>",
        f"<style>{CSS}</style>",
        "</head>",
        "<body>",
        render_product_nav(match_run_id, current="tracker"),
        "<main class='app-main'>",
        render_header(context, match_run_id),
        f'<div id="action-feedback" aria-live="polite">{render_notice(message, error)}</div>',
        render_actions(actions, card_index, context.get("daily_action_status", {})),
        render_secondary_actions(secondary_actions, card_index),
        render_matches(
            "Today's Best Matches",
            "best-matches",
            visible_match_buckets["live"],
            tracked,
            match_run_id,
            card_index,
            include_actions=True,
            empty="No strong live matches found right now.",
        ),
        render_explore_market(explore_market, tracked, match_run_id),
        render_pipeline(pipeline_report["records"], match_run_id),
        render_matches(
            "New Matches This Week",
            "new-matches",
            visible_match_buckets["new"],
            tracked,
            match_run_id,
            card_index,
            include_actions=True,
            empty="No especially relevant new matches this week.",
        ),
        render_matches(
            "Always-Open Applications",
            "always-open",
            visible_match_buckets["evergreen"],
            tracked,
            match_run_id,
            card_index,
            include_actions=True,
            empty="No profile-relevant always-open applications surfaced today.",
        ),
        render_applicant_signals(applicant_signals),
        render_disclaimer(),
        render_inline_action_script(),
        "</main>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts)


def render_lightweight_tracker(
    context,
    match_run_id,
    tracker_view="all",
    demo_mode=False,
    message=None,
    error=None,
):
    profile = context["profile"]
    parts = [
        "<!doctype html>",
        "<html lang='en'>",
        "<head>",
        "<meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>Wahojobs Tracker - {e(profile['display_name'])}</title>",
        f"<style>{CSS}</style>",
        "</head>",
        "<body>",
        render_product_nav(match_run_id, current="tracker"),
        "<main class='app-main'>",
        render_lightweight_tracker_header(context["records"]),
        f'<div id="action-feedback" aria-live="polite">{render_notice(message, error)}</div>',
        render_my_jobs_workspace(context["records"], match_run_id, tracker_view),
        render_inline_action_script(),
        "</main>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts)


def render_lightweight_tracker_header(records):
    active_count = sum(
        1
        for record in records
        if record["visibility"] == "visible"
        and record["workflow_status"] in TRACKER_FILTER_STATUSES["in_progress"]
    )
    reminder_count = sum(
        1
        for record in records
        if record.get("reminder_at") is not None
    )
    job_label = "1 job" if len(records) == 1 else f"{len(records)} jobs"
    progress_label = "1 in progress" if active_count == 1 else f"{active_count} in progress"
    reminder_label = "1 reminder" if reminder_count == 1 else f"{reminder_count} reminders"
    return f"""
    <section class="my-jobs-header">
      <p class="eyebrow">Workspace</p>
      <h1>My Jobs</h1>
      <p class="lead">Track saved jobs, applications, assessments, and follow-ups.</p>
      <p class="my-jobs-summary"><span>{e(job_label)}</span><span>{e(progress_label)}</span><span>{e(reminder_label)}</span></p>
    </section>
    """


def render_preview_from_params(params, registry, demo_mode=False):
    run_id = first_value(params, "run")
    run = require_match_run(registry, run_id) if run_id else None
    sample_id = run.demo_persona if run else (first_value(params, "sample") if demo_mode else "")
    sample = PREVIEW_SAMPLES.get(sample_id, {}) if demo_mode else {}
    input_text = run.raw_input if run else sample.get("text", "")
    input_style = run.input_style if run else sample.get("style", "short_paragraph")
    context = run.recommendation_context if run else None
    if context:
        context = profile_preview.refresh_preview_context_freshness(
            context, current_utc_time()
        )
    reviewing = bool(
        run
        and run.canonical_profile
        and (
            not run.profile_confirmed
            or first_value(params, "review") == "1"
            or first_value(params, "edit") == "1"
        )
        and first_value(params, "edit_text") != "1"
    )
    editing_text = bool(run and first_value(params, "edit_text") == "1")
    owner_profile_id = (
        run.owner_profile_id
        if run
        else sample.get("owner_profile_id", NORMAL_OWNER_PROFILE_ID)
    )
    tracked = load_preview_tracked(owner_profile_id)
    return render_profile_preview_page(
        input_text=input_text,
        input_style=input_style,
        sample_id=sample_id,
        context=context,
        message=first_value(params, "message"),
        error=first_value(params, "error"),
        owner_profile_id=owner_profile_id,
        match_run_id=run_id,
        demo_mode=demo_mode,
        editing=editing_text,
        reviewing=reviewing,
        canonical_profile=run.canonical_profile if run else None,
        review_token=run.review_token if run else "",
        tracked=tracked,
    )


def preview_data_signature():
    with get_connection() as conn:
        jobs = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(is_active), 0), COALESCE(MAX(updated_at), '')
            FROM jobs
            """
        ).fetchone()
        canonical = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(is_active), 0), COALESCE(MAX(updated_at), '')
            FROM canonical_opportunities
            """
        ).fetchone()
    overlay_path = profile_preview.DEFAULT_OVERLAY_PATH
    overlay_mtime = overlay_path.stat().st_mtime_ns if overlay_path.exists() else 0
    return tuple(jobs) + tuple(canonical) + (overlay_mtime,)


@lru_cache(maxsize=16)
def build_cached_preview_context(input_text, input_style, limit, _data_signature):
    return profile_preview.build_preview_context(
        input_text,
        input_style,
        limit=limit,
    )


@lru_cache(maxsize=16)
def build_cached_structured_preview_context(
    canonical_json,
    raw_input,
    input_style,
    limit,
    _data_signature,
):
    canonical = json.loads(canonical_json)
    return profile_preview.build_preview_context_from_canonical(
        canonical,
        raw_input=raw_input,
        input_style=input_style,
        limit=limit,
        normalizer_name="reviewed_profile",
        normalization_warnings=[],
        extraction_quality="reviewed",
    )


def current_utc_time():
    return datetime.now(timezone.utc)


def build_current_preview_context(
    input_text,
    input_style,
    limit,
    data_signature,
    *,
    evaluated_at=None,
):
    cached = build_cached_preview_context(
        input_text,
        input_style,
        limit,
        data_signature,
    )
    now = evaluated_at or current_utc_time()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return profile_preview.refresh_preview_context_freshness(cached, now)


def build_current_structured_preview_context(
    canonical,
    owner_profile_id,
    raw_input,
    input_style,
    limit,
    data_signature,
    *,
    evaluated_at=None,
):
    bound = _bind_identity_free_profile_for_legacy_matching(
        canonical,
        owner_profile_id,
    )
    bound_bytes = json.dumps(
        bound,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    cached = build_cached_structured_preview_context(
        bound_bytes,
        raw_input,
        input_style,
        limit,
        data_signature,
    )
    now = evaluated_at or current_utc_time()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return profile_preview.refresh_preview_context_freshness(cached, now)


def build_ranked_presentation_matches(context, limit=PRESENTATION_MATCH_LIMIT):
    ranked = []
    matches_by_section = (context or {}).get("matches") or {}
    for section in ACTIONABLE_PRESENTATION_SECTIONS:
        for match in matches_by_section.get(section, []):
            if not match.get("primary_recommendation_eligible", True):
                continue
            if match.get("affirmative_fit_status") != "supported":
                continue
            if match.get("opportunity_trust_status") != "trusted":
                continue
            presented = dict(match)
            presented["presentation_rank"] = len(ranked) + 1
            presented["presentation_source_section"] = section
            ranked.append(presented)
            if len(ranked) >= limit:
                return ranked
    return ranked


def build_browser_presentation_matches(context, limit=PRESENTATION_MATCH_LIMIT):
    """Return verified matches plus a bounded recent-cache fallback."""
    ranked = []
    seen_identities = set()
    matches_by_section = (context or {}).get("matches") or {}
    for data_status in ("recently_verified", "recently_cached"):
        for section in ACTIONABLE_PRESENTATION_SECTIONS:
            for match in matches_by_section.get(section, []):
                if data_status == "recently_verified":
                    eligible = (
                        match.get("opportunity_trust_status") == "trusted"
                        and match.get("primary_recommendation_eligible", True)
                    )
                else:
                    eligible = recent_cached_match_is_usable(match)
                if (
                    not eligible
                    or browser_match_rejection_reasons(match)
                    or match.get("affirmative_fit_status") != "supported"
                ):
                    continue
                identity = stable_opportunity_identity(match)
                if data_status == "recently_cached" and identity in seen_identities:
                    continue
                seen_identities.add(identity)
                presented = dict(match)
                presented["presentation_rank"] = len(ranked) + 1
                presented["presentation_source_section"] = section
                presented["presentation_data_status"] = data_status
                ranked.append(presented)
                if len(ranked) >= limit:
                    return ranked
    return ranked


def recent_cached_match_is_usable(match):
    if match.get("opportunity_trust_status") != "stale_source":
        return False
    if browser_match_rejection_reasons(match):
        return False
    age_hours = (match.get("opportunity_trust") or {}).get("source_age_hours")
    try:
        return 0 <= float(age_hours) <= RECENT_CACHED_MATCH_MAX_AGE_HOURS
    except (TypeError, ValueError):
        return False


def browser_match_rejection_reasons(match):
    reasons = []
    if stable_opportunity_identity(match) is None:
        reasons.append("invalid_stable_identity")
    if safe_job_url(match.get("url")) is None:
        reasons.append("invalid_job_url")
    if not match.get("eligible_for_personalized", True):
        reasons.append("personalized_eligibility_failed")
    if match.get("professional_domain_hard_gate_applied"):
        reasons.append("professional_domain_hard_gate")
    if match.get("location_eligibility_status") == "incompatible":
        reasons.append("incompatible_location")
    if match.get("affirmative_fit_status") != "supported":
        reasons.append("affirmative_fit_not_supported")
    cap_reasons = set(match.get("actionability_cap_reasons") or [])
    if cap_reasons - {"opportunity_trust_stale_source", "opportunity_trust_unverified_source"}:
        reasons.append("non_freshness_actionability_cap")
    trust = match.get("opportunity_trust") or {}
    if (
        match.get("job_is_active") is False
        or match.get("canonical_is_active") is False
        or trust.get("job_is_active") is False
        or trust.get("canonical_is_active") is False
    ):
        reasons.append("inactive_opportunity")
    return tuple(dict.fromkeys(reasons))


def stable_opportunity_identity(match):
    canonical_raw = match.get("canonical_opportunity_id")
    if canonical_raw not in (None, ""):
        canonical_id = positive_integer_identity(canonical_raw)
        return ("canonical", canonical_id) if canonical_id is not None else None
    job_id = positive_integer_identity(match.get("job_id"))
    if job_id is not None:
        return ("job", job_id)
    selected_variant_id = positive_integer_identity(
        (match.get("opportunity_trust") or {}).get("selected_variant_id")
    )
    return (
        ("job", selected_variant_id)
        if selected_variant_id is not None
        else None
    )


def positive_integer_identity(value):
    if isinstance(value, bool) or value in (None, ""):
        return None
    text = str(value)
    if text != text.strip() or not re.fullmatch(r"[1-9][0-9]*", text):
        return None
    return int(text)


def safe_job_url(value):
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            return None
        if any(character.isspace() for character in parsed.netloc):
            return None
        parsed.port
    except (TypeError, ValueError):
        return None
    return value


def load_preview_tracked(profile_id):
    try:
        with get_connection() as conn:
            require_normalized_browser_read_ready(conn)
            records = [
                normalized_browser_record(record)
                for record in pipeline_records.list_pipeline_records(
                    conn, profile_id, mutation_grade=True
                )
            ]
    except sqlite3.OperationalError as exc:
        raise pipeline_records.PipelineRecordInvariant(
            "Normalized pipeline state is unavailable."
        ) from exc
    return demo.build_tracked_index(records)


def preview_pipeline_match_key(record):
    if record.get("url"):
        return re.sub(r"\s+", " ", str(record["url"]).strip().lower())
    return "|".join(
        re.sub(r"\s+", " ", str(part or "").strip().lower())
        for part in (record.get("source"), record.get("title"))
    )


def render_profile_preview_page(
    input_text,
    input_style,
    sample_id="",
    context=None,
    message="",
    error="",
    owner_profile_id=NORMAL_OWNER_PROFILE_ID,
    match_run_id="",
    demo_mode=False,
    editing=False,
    reviewing=False,
    canonical_profile=None,
    review_token="",
    tracked=None,
):
    tracked = tracked or demo.build_tracked_index([])
    parts = [
        "<!doctype html>",
        "<html lang='en'>",
        "<head>",
        "<meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Wahojobs - Find Matches</title>",
        f"<style>{CSS}</style>",
        "</head>",
        "<body>",
        render_product_nav(match_run_id, current="matches"),
        "<main class='app-main'>",
        render_demo_owner_panel(owner_profile_id, sample_id) if demo_mode else "",
        f'<div id="action-feedback" aria-live="polite">{render_notice(message, error)}</div>',
    ]
    if reviewing and canonical_profile:
        parts.extend(
            [
                render_profile_review_intro(match_run_id, bool(context)),
                render_structured_profile_review(
                    canonical_profile,
                    match_run_id,
                    review_token,
                ),
            ]
        )
    elif editing:
        parts.extend(
            [
                render_preview_edit_intro(match_run_id),
                render_preview_form(
                    input_text,
                    input_style,
                    sample_id,
                    demo_mode,
                    edit_run_id=match_run_id,
                    edit_review_token=review_token,
                ),
            ]
        )
    elif context:
        parts.extend(
            [
                render_preview_results_header(context, demo_mode=demo_mode),
                render_preview_profile_context(context, match_run_id),
                render_ranked_preview_matches(context, tracked, match_run_id),
                render_demo_persona_switcher(sample_id) if demo_mode else "",
                render_preview_qa_details(context) if demo_mode else "",
            ]
        )
    else:
        parts.extend(
            [
                render_preview_header(demo_mode=demo_mode),
                render_preview_form(input_text, input_style, sample_id, demo_mode),
            ]
        )
    parts.extend([render_inline_action_script(), "</main>", "</body>", "</html>"])
    return "\n".join(parts)


def render_product_nav(match_run_id, current):
    matches_url = "/find-matches"
    tracker_url = "/tracker"
    if match_run_id:
        query = urlencode({"run": match_run_id})
        matches_url += f"?{query}"
        tracker_url += f"?{query}"
    return f"""
    <header class="app-header">
      <div class="app-header-inner">
        <a class="wordmark" href="{e(matches_url)}" aria-label="Wahojobs Matches">Wahojobs</a>
        <nav class="product-nav" aria-label="Product navigation">
          <a href="{e(matches_url)}" {'aria-current="page"' if current == 'matches' else ''}>Matches</a>
          <a href="{e(tracker_url)}" {'aria-current="page"' if current == 'tracker' else ''}>My Jobs</a>
        </nav>
      </div>
    </header>
    """


def render_preview_header(demo_mode=False):
    return f"""
    <section class="page-intro onboarding-intro">
      <p class="eyebrow">Matches</p>
      <h1>Find AI work that fits you</h1>
      <p class="lead">Tell us about your background. We'll show the best current opportunities for you.</p>
      {f'<p class="muted">Demo mode keeps development personas and quality checks available below.</p>' if demo_mode else ''}
    </section>
    """


def render_preview_results_header(context, demo_mode=False):
    matches = build_browser_presentation_matches(context)
    total_label = "1 match" if len(matches) == 1 else f"{len(matches)} matches"
    return f"""
    <section class="results-header">
      <div>
        <p class="eyebrow">Matches</p>
        <h1>Your matches</h1>
        <p class="results-summary"><strong>{e(total_label)}</strong></p>
        {f'<p class="muted">Demo mode keeps development personas and quality checks available below.</p>' if demo_mode else ''}
      </div>
    </section>
    """


def render_demo_owner_panel(owner_profile_id, sample_id):
    sample = PREVIEW_SAMPLES.get(sample_id) or {}
    label = sample.get("label") or "Custom local-user input"
    return f"""
    <section class="notice tracker-profile-panel" id="tracking-profile">
      <div>
        <p class="eyebrow">Development persona</p>
        <h2>{e(label)}</h2>
        <p>Owner: <code>{e(owner_profile_id)}</code>. Persona text, parser mode, identity, and owner move together.</p>
      </div>
    </section>
    """


def render_preview_edit_intro(match_run_id):
    return f"""
    <section class="preview-edit-intro">
      <p class="eyebrow">Edit profile</p>
      <h1>Update what we should match against</h1>
      <p class="muted">Your current results stay unchanged. Submitting these edits creates a new match run.</p>
      <p><a class="back-link" href="{e('/find-matches?' + urlencode({'run': match_run_id}))}">Cancel and return to matches</a></p>
    </section>
    """


def render_profile_review_intro(match_run_id, has_results=False):
    return f"""
    <section class="page-intro profile-review-intro">
      <p class="eyebrow">Review your profile</p>
      <h1>Make sure we understood you</h1>
      <p class="lead">Correct anything that is missing or inaccurate before we match you with opportunities.</p>
      <p class="muted">Items were prefilled from your description. Your corrections become authoritative.</p>
      {f'<p><a class="back-link" href="{e("/find-matches?" + urlencode({"run": match_run_id}))}">Back to current matches</a></p>' if has_results else ''}
    </section>
    """


def render_structured_profile_review(
    canonical,
    match_run_id,
    review_token,
    *,
    form_action="/find-matches",
    back_url=None,
    submit_label="Find my matches",
    include_draft_fingerprint=True,
):
    location = canonical.get("location") or {}
    education = canonical.get("education") or {}
    credentials = canonical.get("credentials") or {}
    experience = canonical.get("experience") or {}
    skills = canonical.get("skills") or {}
    preferences = canonical.get("preferences") or {}
    constraints = canonical.get("constraints") or {}
    languages = list(canonical.get("languages") or [])
    while len(languages) < 4:
        languages.append({})
    language_rows = "".join(render_review_language_row(row, index) for index, row in enumerate(languages[:8]))
    source_notes = {
        root: profile_review_source_label(canonical, root)
        for root in ("location", "languages", "experience", "education", "skills", "preferences", "credentials")
    }
    if back_url is None:
        back_url = "/find-matches?" + urlencode(
            {"run": match_run_id, "edit_text": "1"}
        )
    draft_fingerprint = (
        '<input type="hidden" name="profile_draft_fingerprint" '
        f'value="{e(profile_draft_fingerprint(canonical))}">'
        if include_draft_fingerprint
        else ""
    )
    return f"""
    <form method="post" action="{e(form_action)}" class="profile-review-form" id="profile-review-form">
      <input type="hidden" name="form_action" value="confirm_profile">
      <input type="hidden" name="edit_run_id" value="{e(match_run_id)}">
      <input type="hidden" name="review_token" value="{e(review_token)}">
      <input type="hidden" name="schema_version" value="{e(SCHEMA_VERSION)}">
      {draft_fingerprint}

      <section class="review-section review-section-primary">
        <div class="review-section-heading"><div><h2>Location</h2><p>Used only to avoid showing work you cannot access.</p></div>{source_notes['location']}</div>
        <div class="review-grid review-grid-three">
          {review_text_field('country', 'Country', location.get('country'))}
          {review_text_field('region', 'Region or state', location.get('region'))}
          {review_text_field('city', 'City', location.get('city'))}
        </div>
        <div class="review-checks">
          {review_checkbox('remote', 'I prefer remote work', preferences.get('remote') is True)}
          {review_checkbox('flexible', 'I prefer flexible or asynchronous work', preferences.get('flexible') is True)}
        </div>
        <details class="review-more">
          <summary>Work eligibility and geographic restrictions</summary>
          <div class="review-grid">
            {review_text_field('work_authorization', 'Work authorization', location.get('work_authorization'))}
            {review_text_field('eligible_countries', 'Countries where you can work', review_csv(location.get('eligible_countries')))}
            {review_text_field('geographic_restrictions', 'Other geographic restrictions', review_csv(location.get('geographic_work_restrictions') or location.get('restrictions')))}
          </div>
        </details>
      </section>

      <section class="review-section review-section-primary">
        <div class="review-section-heading"><div><h2>Languages</h2><p>Add, remove, or correct proficiency and locale.</p></div>{source_notes['languages']}</div>
        <div class="language-review-list">{language_rows}</div>
        <p class="field-help">Clear a language name to remove it. Blank rows let you add another language.</p>
      </section>

      <section class="review-section">
        <div class="review-section-heading"><div><h2>Experience</h2><p>Your recent work and transferable background.</p></div>{source_notes['experience']}</div>
        <div class="review-grid review-grid-three">
          {review_text_field('job_titles', 'Recent job titles', review_csv(experience.get('job_titles') or experience.get('recent_roles')))}
          {review_text_field('occupational_families', 'Kinds of work', review_csv(experience.get('occupational_families')))}
          {review_text_field('total_years', 'Years of experience', experience.get('total_years'), input_type='number', extra='min="0" max="80"')}
          {review_text_field('professional_domains', 'Professional domains', review_csv(experience.get('professional_domains')))}
          {review_text_field('industries', 'Industries', review_csv(experience.get('industries')))}
          {review_text_field('specialties', 'Specialties', review_csv(experience.get('specialties')))}
        </div>
        <details class="review-more">
          <summary>Seniority and contribution style</summary>
          <div class="review-grid">
            {review_text_field('seniority', 'Seniority', experience.get('seniority'))}
            {review_text_field('contribution_type', 'Management or individual contributor', experience.get('contribution_type'))}
          </div>
        </details>
      </section>

      <section class="review-section">
        <div class="review-section-heading"><div><h2>Education</h2><p>Only confirmed education is used for specialist roles.</p></div>{source_notes['education']}</div>
        <div class="review-grid review-grid-three">
          {review_select('education_level', 'Highest level', education.get('education_level'), EDUCATION_LEVELS)}
          {review_text_field('degrees', 'Degree names', review_csv(education.get('degrees')))}
          {review_text_field('education_fields', 'Fields of study', review_csv(education.get('fields_or_domains')))}
          {review_text_field('institutions', 'Institution', review_csv(education.get('institutions')))}
          {review_text_field('education_status', 'Completed or in progress', education.get('completion_status'))}
        </div>
      </section>

      <section class="review-section">
        <div class="review-section-heading"><div><h2>Skills</h2><p>Keep only skills you would be comfortable using at work.</p></div>{source_notes['skills']}</div>
        {review_text_field('skills', 'Skills', review_csv(skills.get('normalized')))}
        <details class="review-more">
          <summary>Organize skills by type</summary>
          <div class="review-grid">
            {review_text_field('technical_skills', 'Technical skills', review_csv(skills.get('technical')))}
            {review_text_field('software_tools', 'Software and tools', review_csv(skills.get('software_tools')))}
            {review_text_field('writing_research_skills', 'Writing and research', review_csv(skills.get('writing_research')))}
            {review_text_field('administrative_support_skills', 'Administrative and support', review_csv(skills.get('administrative_support')))}
            {review_text_field('domain_specific_skills', 'Domain-specific skills', review_csv(skills.get('domain_specific')))}
          </div>
        </details>
      </section>

      <section class="review-section">
        <div class="review-section-heading"><div><h2>Work preferences</h2><p>Tell us which opportunities feel practical and worthwhile.</p></div>{source_notes['preferences']}</div>
        <div class="review-grid review-grid-three">
          {review_text_field('target_opportunity_types', 'Work you want', review_csv(preferences.get('target_opportunity_types')))}
          {review_text_field('employment_types', 'Employment types', review_csv(preferences.get('employment_types')))}
          {review_text_field('phone_preference', 'Phone preference', preferences.get('phone_preference'))}
          {review_text_field('synchronous_preference', 'Schedule style', preferences.get('synchronous_preference'))}
          {review_text_field('availability', 'Availability', preferences.get('availability'))}
          {review_text_field('schedule', 'Schedule details', review_csv(preferences.get('schedule')))}
        </div>
      </section>

      <section class="review-section sensitive-review">
        <div class="review-section-heading"><div><h2>Credentials and constraints</h2><p>Please confirm these details. We never infer a license from job interests.</p></div>{source_notes['credentials']}</div>
        <div class="review-grid review-grid-three">
          {review_select('credential_status', 'Credential status', credentials.get('credential_status'), CREDENTIAL_STATUSES)}
          {review_text_field('certifications', 'Certifications', review_csv(credentials.get('certifications')))}
          {review_text_field('licenses', 'Professional licenses', review_csv(credentials.get('licenses')))}
          {review_text_field('jurisdictions', 'License jurisdictions', review_csv(credentials.get('jurisdictions')))}
          {review_text_field('security_clearances', 'Security clearances', review_csv(credentials.get('security_clearances')))}
          {review_text_field('excluded_domains', 'Work to exclude', review_csv(constraints.get('excluded_domains')))}
          {review_text_field('accessibility_constraints', 'Accessibility or working constraints', review_csv(constraints.get('accessibility_constraints')))}
          {review_text_field('hard_constraints', 'Other firm constraints', review_csv(constraints.get('hard_constraints')))}
          {review_text_field('soft_preferences', 'Other preferences', review_csv(constraints.get('soft_preferences')))}
          {review_text_field('avoid_keywords', 'Keywords to avoid', review_csv(constraints.get('avoid_keywords')))}
        </div>
        <div class="review-checks">
          {review_checkbox('no_degree', 'I do not have a university degree', 'no college degree' in (constraints.get('hard_constraints') or []) or education.get('education_level') == 'no_degree')}
          {review_checkbox('no_experience', 'I do not have prior work experience', 'no prior experience' in (constraints.get('hard_constraints') or []))}
          {review_checkbox('no_specialized_credentials', 'I do not have specialized credentials', any('credential' in str(value).lower() for value in (constraints.get('hard_constraints') or [])))}
          {review_checkbox('credentials_confirmed', 'I confirm that the licenses and certifications above are accurate', False, required=True)}
        </div>
      </section>

      <div class="review-actions">
        <a class="open button-secondary" href="{e(back_url)}">Back</a>
        <button type="submit" id="confirm-profile-button">{e(submit_label)}</button>
      </div>
    </form>
    """


def render_review_language_row(language, index):
    proficiency = str(language.get("proficiency") or "unspecified")
    if proficiency not in LANGUAGE_PROFICIENCIES:
        proficiency = "professional" if proficiency == "advanced" else "unspecified"
    return f"""
    <div class="language-review-row">
      {review_text_field(f'language_{index}', 'Language', language.get('language'))}
      {review_select(f'language_proficiency_{index}', 'Proficiency', proficiency, LANGUAGE_PROFICIENCIES)}
      {review_text_field(f'language_locale_{index}', 'Locale, if relevant', language.get('locale'))}
    </div>
    """


def profile_review_source_label(canonical, root):
    sources = canonical.get("provenance", {}).get("field_sources") or {}
    relevant = [
        detail
        for path, detail in sources.items()
        if path == root or path.startswith(root + ".") or path.startswith(root + "[")
    ]
    if relevant and all(detail.get("explicit") is True for detail in relevant):
        label = "Confirmed by you"
    elif relevant:
        label = "Inferred - please confirm"
    else:
        label = "Needs your input"
    return f'<span class="inference-label">{e(label)}</span>'


def review_text_field(name, label, value, *, input_type="text", extra=""):
    return f"""
    <label class="review-field" for="{e(name)}"><span>{e(label)}</span>
      <input id="{e(name)}" name="{e(name)}" type="{e(input_type)}" value="{e('' if value is None else value)}" {extra}>
    </label>
    """


def review_select(name, label, value, options):
    current = str(value or "")
    choices = "".join(
        f'<option value="{e(option)}" {"selected" if option == current else ""}>{e(profile_review_option_label(option))}</option>'
        for option in options
    )
    return f'<label class="review-field" for="{e(name)}"><span>{e(label)}</span><select id="{e(name)}" name="{e(name)}">{choices}</select></label>'


def review_checkbox(name, label, checked, *, required=False):
    return f'<label class="review-checkbox"><input type="checkbox" name="{e(name)}" value="1" {"checked" if checked else ""} {"required" if required else ""}> <span>{e(label)}</span></label>'


def review_csv(values):
    if isinstance(values, str):
        return values
    return ", ".join(str(value) for value in (values or []) if str(value).strip())


def profile_review_option_label(value):
    labels = {
        "not_specified": "Not specified",
        "no_degree": "No university degree",
        "professional_degree": "Professional degree",
        "explicit": "I hold a credential or license",
        "in_progress": "In progress",
        "absent": "None",
        "unknown": "Not specified",
        "unspecified": "Not specified",
    }
    return labels.get(value, str(value).replace("_", " ").title())


def render_preview_form(
    input_text,
    input_style,
    sample_id,
    demo_mode=False,
    edit_run_id="",
    edit_review_token="",
):
    sample_controls = ""
    if demo_mode and not edit_run_id:
        sample_buttons = "".join(
            f"<button type='button' class='sample-loader' "
            f"data-sample-id='{e(key)}' data-sample-style='{e(sample['style'])}' "
            f"data-sample-text='{e(sample['text'])}'>{e(sample['label'])}</button>"
            for key, sample in PREVIEW_SAMPLES.items()
        )
        fallback_sample_buttons = "".join(
            f"<button type='submit' name='sample' value='{e(key)}'>{e(sample['label'])}</button>"
            for key, sample in PREVIEW_SAMPLES.items()
        )
        sample_controls = f"""
        <p class="sample-label">Development personas</p>
        <div class="sample-actions" aria-label="Development personas">{sample_buttons}</div>
        <p id="sample-loaded-note" class="muted sample-loaded-note" role="status" aria-live="polite" hidden></p>
        <noscript>
          <form method="get" action="/find-matches" class="sample-actions">{fallback_sample_buttons}</form>
        </noscript>
        """
    if demo_mode:
        style_options = "".join(
            f"<option value='{e(style)}' {'selected' if style == input_style else ''}>{e(style.replace('_', ' '))}</option>"
            for style in sorted(profile_preview.INPUT_STYLES)
        )
        input_style_control = f"""
        <details class="advanced-options">
          <summary>Advanced QA parser mode</summary>
          <p class="muted">Optional internal control for testing how the baseline parser reads different input styles.</p>
          <label for="input_style">Parser input style</label>
          <select id="input_style" name="input_style">{style_options}</select>
        </details>
        """
    else:
        input_style_control = (
            f'<input type="hidden" id="input_style" name="input_style" value="{e(input_style)}">'
        )
    submit_label = "Update matches" if edit_run_id else "Find matches"
    if not edit_run_id:
        submit_label = "Find my matches"
    return f"""
    <section class="preview-input" id="profile-preview-input">
      {sample_controls}
      <form method="post" action="/find-matches" class="preview-form" id="find-matches-form">
        <label for="input_text">About you</label>
        <p id="profile-input-help" class="field-help">Include your location, languages, experience, skills, and the type of work you want.</p>
        <textarea id="input_text" name="input_text" rows="6" aria-describedby="profile-input-help">{e(input_text)}</textarea>
        {input_style_control}
        <input type="hidden" name="sample" value="{e(sample_id)}">
        <input type="hidden" name="edit_run_id" value="{e(edit_run_id)}">
        <input type="hidden" name="edit_review_token" value="{e(edit_review_token)}">
        <button type="submit" id="find-matches-button">{e(submit_label)}</button>
      </form>
      <script>
      (() => {{
        const input = document.getElementById("input_text");
        const inputStyle = document.getElementById("input_style");
        const sampleId = document.querySelector('.preview-form input[name="sample"]');
        const note = document.getElementById("sample-loaded-note");
        const findForm = document.getElementById("find-matches-form");
        const findButton = document.getElementById("find-matches-button");
        document.querySelectorAll(".sample-loader").forEach((button) => {{
          button.addEventListener("click", () => {{
            input.value = button.dataset.sampleText || "";
            inputStyle.value = button.dataset.sampleStyle || "short_paragraph";
            sampleId.value = button.dataset.sampleId || "";
            if (note) {{
              note.textContent = "Development persona loaded. Click Find matches to create its owned run.";
              note.hidden = false;
            }}
            input.focus();
          }});
        }});
        findForm.addEventListener("submit", () => {{
          findButton.disabled = true;
          findButton.textContent = "{e('Updating matches...' if edit_run_id else 'Finding matches...')}";
        }});
      }})();
      </script>
    </section>
    """


def render_preview_profile_context(context, match_run_id):
    canonical = context["canonical_profile"]
    summary_items = profile_summary_items(canonical)
    summary = "".join(f"<li>{e(item)}</li>" for item in summary_items)
    if not summary:
        summary = '<li class="muted">Add more profile details to improve your matches.</li>'
    edit_url = "/find-matches?" + urlencode({"run": match_run_id, "edit": "1"})
    return f"""
    <section class="profile-context" id="profile-context">
      <div>
        <p class="profile-context-label">Based on your profile</p>
        <ul class="profile-summary-line" aria-label="Profile used for these matches">{summary}</ul>
      </div>
      <a class="open secondary-link" href="{e(edit_url)}">Edit profile</a>
    </section>
    """


def profile_summary_items(canonical):
    items = []
    location = canonical.get("location") or {}
    location_label = location.get("country") or location.get("region") or location.get("city")
    if location_label:
        items.append(str(location_label))

    languages = canonical.get("languages") or []
    for language in languages:
        name = str(language.get("language") or "").strip()
        if not name:
            continue
        details = []
        locale = str(language.get("locale") or "").strip()
        proficiency = str(language.get("proficiency") or "").strip().lower()
        if locale:
            details.append(locale)
        if proficiency and proficiency not in {"unknown", "not specified", "not_specified"}:
            details.append(proficiency.replace("_", " "))
        items.append(f"{name} ({', '.join(details)})" if details else name)

    domains = []
    for value in (
        *((canonical.get("education") or {}).get("fields_or_domains") or []),
        *((canonical.get("experience") or {}).get("professional_domains") or []),
    ):
        normalized = str(value or "").strip()
        if not normalized or normalized.lower() in {"language", "languages"}:
            continue
        label = normalized.replace("_", " ").replace("-", " ").title()
        if label not in domains:
            domains.append(label)
    items.extend(domains)

    preferences = canonical.get("preferences") or {}
    if preferences.get("remote") is True:
        items.append("Remote preferred")
    return items


def render_ranked_preview_matches(context, tracked, match_run_id):
    matches = build_browser_presentation_matches(context)
    cards = "".join(
        render_ranked_preview_card(match, tracked, match_run_id)
        for match in matches
    )
    if not cards:
        cards = """
        <div class="notice no-match-state">
          <p><strong>No clear matches surfaced from this profile yet.</strong></p>
          <p class="muted">Try adding your location, languages, credentials, or the kinds of work you want.</p>
        </div>
        """
    return f"""
    <section id="your-best-matches" class="ranked-matches">
      <div class="stack">{cards}</div>
    </section>
    """


def supported_candidates_need_refresh(context):
    return supported_candidates_needing_verification(context) > 0


def supported_candidates_needing_verification(context):
    return sum(
        1
        for section in ACTIONABLE_PRESENTATION_SECTIONS
        for match in ((context or {}).get("matches") or {}).get(section, [])
        if match.get("affirmative_fit_status") == "supported"
        and match.get("opportunity_trust_status") in {"stale_source", "unverified_source"}
        and not browser_match_rejection_reasons(match)
    )


def supported_candidates_withheld_for_refresh(context):
    """Compatibility alias for older tests and callers."""
    return supported_candidates_needing_verification(context)


def render_ranked_preview_card(match, tracked, match_run_id):
    record = demo.tracked_record_for_match(match, tracked)
    section = match["presentation_source_section"]
    caution = product_caution_note(match)
    fit_reason = profile_preview.user_fit_reason(match)
    url = safe_job_url(match.get("url")) or ""
    card_classes = "card preview-card ranked-card"
    card_id = f"ranked-{match_opportunity_key(match)}"
    status = (
        f'<p class="pill card-status js-card-status">{e(readable_status(record["status"]))}</p>'
        if record
        else '<p class="pill card-status js-card-status"></p>'
    )
    controls = render_preview_card_actions(match, record, match_run_id, section, card_id)
    return f"""
    <article class="{card_classes}" id="{e(card_id)}" data-action-card>
      <div class="match-rank" aria-label="Rank {match['presentation_rank']}">#{match['presentation_rank']}</div>
      <div class="card-main">
        <p class="source">{e(match['source'])}</p>
        <h3>{e(match['display_title'])}</h3>
        <p class="muted">{e(match.get('location') or 'Location not listed')}</p>
        <p><strong>Why it fits:</strong> {e(fit_reason)}</p>
        {f'<p class="caution"><strong>Before applying:</strong> {e(caution)}</p>' if caution else ''}
        {status}
      </div>
      <div class="card-actions">
        {f'<a class="open button-primary" href="{e(url)}" target="_blank" rel="noreferrer">View job</a>' if url else ''}
        <div class="js-card-controls">{controls}</div>
      </div>
    </article>
    """


def product_caution_note(match):
    """Return qualification cautions without exposing internal freshness state."""
    caution = profile_preview.user_caution_note(match)
    freshness_messages = (
        "This opportunity has not been verified by a recent source refresh.",
        "Current source verification is unavailable for this opportunity.",
    )
    for message in freshness_messages:
        caution = caution.replace(message, "").strip()
    return caution


def render_demo_persona_switcher(sample_id):
    forms = "".join(
        f"""
        <form method="post" action="/find-matches">
          <input type="hidden" name="sample" value="{e(key)}">
          <button type="submit" {'disabled' if key == sample_id else ''}>{e(sample['label'])}</button>
        </form>
        """
        for key, sample in PREVIEW_SAMPLES.items()
    )
    return f"""
    <section class="demo-persona-switcher">
      <p class="eyebrow">Development personas</p>
      <div class="sample-actions">{forms}</div>
    </section>
    """


def render_preview_qa_details(context):
    bucket_rows = "".join(
        f"<tr><td>{e(profile_preview.SECTION_LABELS[section])}</td><td>{len(context['matches'].get(section, []))}</td></tr>"
        for section in profile_preview.SECTION_ORDER
    )
    return f"""
    <details class="explore-details preview-diagnostic qa-details">
      <summary><span>QA details</span><small>profile parsing and internal result buckets</small></summary>
      {render_preview_profile_summary(context)}
      <section>
        <h3>Internal result buckets</h3>
        <table><thead><tr><th>Bucket</th><th>Count</th></tr></thead><tbody>{bucket_rows}</tbody></table>
      </section>
      {render_primary_omissions(context)}
      {render_opportunity_trust_qa(context)}
      {render_affirmative_fit_qa(context)}
      {render_preview_diagnostics(context)}
    </details>
    """


def render_primary_omissions(context):
    omitted = [
        (section, match)
        for section in ACTIONABLE_PRESENTATION_SECTIONS
        for match in context["matches"].get(section, [])
        if not match.get("primary_recommendation_eligible", True)
    ]
    if not omitted:
        return "<section><h3>Omitted from primary list</h3><p class='muted'>None.</p></section>"
    rows = "".join(
        "<tr>"
        f"<td>{e(match.get('source') or '-')}</td>"
        f"<td>{e(match.get('display_title') or match.get('title') or '-')}</td>"
        f"<td>{e(profile_preview.SECTION_LABELS.get(section, section))}</td>"
        f"<td>{e(', '.join(match.get('primary_admission_reasons') or match.get('actionability_cap_reasons') or []))}</td>"
        "</tr>"
        for section, match in omitted[:30]
    )
    return f"""
    <section>
      <h3>Omitted from primary list</h3>
      <p class="muted">Guardrail-demoted rows remain available here for demo QA.</p>
      <table>
        <thead><tr><th>Source</th><th>Opportunity</th><th>Internal bucket</th><th>Admission reason</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
    """


def render_opportunity_trust_qa(context):
    rows = []
    for section in profile_preview.SECTION_ORDER:
        for match in context["matches"].get(section, []):
            trust = match.get("opportunity_trust") or {}
            variants = match.get("considered_canonical_variants") or []
            variant_summary = "; ".join(
                f"{variant.get('job_id')}: {variant.get('location') or '-'} / "
                f"{variant.get('opportunity_trust_status') or '-'}"
                for variant in variants
            ) or "-"
            age = trust.get("source_age_hours")
            rows.append(
                "<tr>"
                f"<td>{e(match.get('source') or '-')}</td>"
                f"<td>{e(match.get('display_title') or '-')}</td>"
                f"<td>{e(trust.get('status') or '-')}</td>"
                f"<td>{e('; '.join(trust.get('reasons') or []) or '-')}</td>"
                f"<td>{e('yes' if trust.get('job_is_active') else 'no')}</td>"
                f"<td>{e('-' if trust.get('canonical_is_active') is None else ('yes' if trust.get('canonical_is_active') else 'no'))}</td>"
                f"<td>{e(trust.get('job_last_seen_at') or '-')}</td>"
                f"<td>{e(trust.get('latest_successful_source_run_at') or '-')}</td>"
                f"<td>{e('-' if age is None else age)}</td>"
                f"<td>{e(trust.get('inventory_model') or '-')}</td>"
                f"<td>{e(trust.get('market_count_policy') or '-')}</td>"
                f"<td>{e(trust.get('freshness_max_age_hours') if trust.get('freshness_max_age_hours') is not None else '-')}</td>"
                f"<td>{e(trust.get('source_run_id') or '-')}</td>"
                f"<td>{e('yes' if trust.get('source_run_qualifies') else 'no')}</td>"
                f"<td>{e(match.get('location_eligibility_status') or '-')}</td>"
                f"<td>{e(variant_summary)}</td>"
                f"<td>{e(trust.get('selected_variant_id') or '-')}</td>"
                f"<td>{e('yes' if match.get('primary_recommendation_eligible') else 'no')}</td>"
                "</tr>"
            )
    if not rows:
        return "<section><h3>Opportunity trust</h3><p class='muted'>No opportunity rows.</p></section>"
    return f"""
    <section>
      <h3>Opportunity trust</h3>
      <p class="muted">Demo-only lifecycle, freshness, location, and variant-selection diagnostics.</p>
      <div class="table-scroll">
        <table>
          <thead><tr><th>Source</th><th>Opportunity</th><th>Trust</th><th>Reasons</th><th>Job active</th><th>Canonical active</th><th>Last seen</th><th>Latest qualifying run</th><th>Age hours</th><th>Inventory</th><th>Count policy</th><th>Max age</th><th>Run ID</th><th>Run qualifies</th><th>Location</th><th>Considered variants</th><th>Selected variant</th><th>Admitted</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </section>
    """


def render_affirmative_fit_qa(context):
    rows = []
    for section in profile_preview.SECTION_ORDER:
        for match in context["matches"].get(section, []):
            assessment = match.get("affirmative_fit") or {}
            required = "; ".join(
                f"{item.get('label', '-') } [{item.get('mode', '-')}]"
                for item in assessment.get("required_groups") or []
            ) or "-"
            supported = "; ".join(
                f"{item.get('requirement', '-')}: {item.get('profile_evidence', '-')}"
                for item in assessment.get("supported_evidence") or []
            ) or "-"
            rows.append(
                "<tr>"
                f"<td>{e(match.get('source') or '-')}</td>"
                f"<td>{e(match.get('display_title') or match.get('title') or '-')}</td>"
                f"<td>{e(match.get('affirmative_fit_status') or '-')}</td>"
                f"<td>{e(required)}</td>"
                f"<td>{e('; '.join(assessment.get('satisfied_groups') or []) or '-')}</td>"
                f"<td>{e(supported)}</td>"
                f"<td>{e('; '.join(assessment.get('adjacencies_used') or []) or '-')}</td>"
                f"<td>{e('; '.join(assessment.get('missing_requirements') or []) or '-')}</td>"
                f"<td>{e('; '.join(assessment.get('unmodeled_requirements') or []) or '-')}</td>"
                f"<td>{e('; '.join(assessment.get('conflicting_requirements') or []) or '-')}</td>"
                f"<td>{e('; '.join(assessment.get('location_and_locale_evidence') or []) or '-')}</td>"
                f"<td>{e('; '.join(assessment.get('why_fit_statements') or []) or '-')}</td>"
                f"<td>{e('yes' if match.get('primary_recommendation_eligible') else 'no')}</td>"
                "</tr>"
            )
    if not rows:
        return "<section><h3>Affirmative fit evidence</h3><p class='muted'>No opportunity rows.</p></section>"
    return f"""
    <section>
      <h3>Affirmative fit evidence</h3>
      <p class="muted">Preview-only evidence and admission diagnostics for every result row.</p>
      <div class="table-scroll">
        <table>
          <thead><tr><th>Source</th><th>Opportunity</th><th>Status</th><th>Required groups</th><th>Satisfied</th><th>Evidence</th><th>Adjacency</th><th>Missing</th><th>Unmodeled</th><th>Conflicts</th><th>Location/locale</th><th>Why</th><th>Admitted</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </section>
    """


def render_preview_profile_summary(context):
    canonical = context["canonical_profile"]
    profile_chips = profile_preview.html_profile_chips(canonical)
    missing_items = profile_preview.html_missing_items(context)
    return f"""
    <section id="profile-understood">
      <h2>Profile Understood</h2>
      <p class="muted">Here is what Wahojobs can use so far. This is a first-pass reading, not a final resume profile.</p>
      <div class="chips">{profile_chips}</div>
      <div class="preview-grid">
        <div class="profile-box"><strong>Languages</strong><br>{e(profile_preview.join_languages(canonical))}</div>
        <div class="profile-box"><strong>Remote preference</strong><br>{e(profile_preview.remote_preference_label(canonical))}</div>
        <div class="profile-box"><strong>Domains</strong><br>{e(', '.join(canonical['education'].get('fields_or_domains') or []) or '-')}</div>
        <div class="profile-box"><strong>Credentials/licenses</strong><br>{e(profile_preview.credentials_label(canonical))}</div>
      </div>
    </section>
    <section id="profile-questions">
      <h2>What We Still Need To Know</h2>
      <p class="muted">To improve your matches, you can add these details when available.</p>
      <ul class="question-list">{missing_items}</ul>
    </section>
    """


def render_preview_matches(context, tracked, match_run_id):
    sections = []
    for section in profile_preview.SECTION_ORDER:
        matches = context["matches"].get(section, [])
        sections.append(render_preview_section(section, matches, tracked, match_run_id))
    return f"""
    <section id="preview-recommendations">
      <h2>Recommended Opportunities</h2>
      <p class="muted">Your plan stays intentionally small. Browse the lower sections only when you want more context.</p>
      <p class="snapshot-note">Links come from the latest local tracker snapshot and may change.</p>
      <p class="muted">Actions are saved to My Jobs.</p>
      {''.join(sections)}
    </section>
    """


def render_preview_section(section, matches, tracked, match_run_id):
    label = profile_preview.SECTION_LABELS[section]
    visible_limit = profile_preview.HTML_SECTION_LIMITS.get(section, 8)
    visible = matches[:visible_limit]
    cards = "".join(
        render_preview_card(match, section, tracked, match_run_id)
        for match in visible
    ) or "<p class='empty'>None in this preview.</p>"
    more = len(matches) - len(visible)
    more_note = f"<p class='muted'>Showing {len(visible)} of {len(matches)}. {more} more kept out of the main view.</p>" if more > 0 else ""
    if section in {"explore_only", "excluded"}:
        return f"""
        <details class="explore-details preview-diagnostic">
          <summary><span>{e(label)}</span><small>{len(matches)} diagnostic/broader results</small></summary>
          <p class="muted">{e(preview_section_note(section))}</p>
          <div class="stack">{cards}</div>
          {more_note}
        </details>
        """
    return f"""
    <section class="preview-section" id="preview-{e(section)}">
      <h3>{e(label)} ({len(matches)})</h3>
      <p class="muted">{e(preview_section_note(section))}</p>
      <div class="stack">{cards}</div>
      {more_note}
    </section>
    """


def render_preview_card(match, section, tracked, match_run_id):
    record = demo.tracked_record_for_match(match, tracked)
    caution = profile_preview.user_caution_note(match)
    fit_reason = profile_preview.user_fit_reason(match)
    url = safe_job_url(match.get("url")) or ""
    open_link = f'<a class="open" href="{e(url)}" target="_blank" rel="noreferrer">View job</a>' if url else ""
    diagnostics = "; ".join(match.get("preview_diagnostics") or []) or "-"
    reasons = "; ".join(match.get("reasons") or []) or "-"
    status = readable_status(record["status"]) if record else profile_preview.SECTION_LABELS.get(section, section)
    card_id = f"preview-{match_opportunity_key(match)}"
    controls = render_preview_card_actions(
        match,
        record,
        match_run_id,
        section,
        card_id,
    )
    return f"""
    <article class="card preview-card" id="{e(card_id)}" data-action-card>
      <div class="card-main">
        <p class="source">{e(match['source'])}</p>
        <h3>{e(match['display_title'])}</h3>
        <p class="muted">Location: {e(match.get('location') or 'Unknown')} &middot; Area: {e(match.get('expertise') or 'Unknown')}</p>
        <p><strong>Why it may fit:</strong> {e(fit_reason)}</p>
        {f'<p class="caution"><strong>Check first:</strong> {e(caution)}</p>' if caution else ''}
        <p class="pill js-card-status">{e(status)}</p>
      </div>
      <div class="card-actions">
        {open_link}
        <div class="js-card-controls">{controls}</div>
        <details class="technical-details">
          <summary>Technical details</summary>
          <p class="muted">Score: {e(match.get('score'))}</p>
          <p class="muted">Reasons: {e(reasons)}</p>
          <p class="muted">Diagnostics: {e(diagnostics)}</p>
        </details>
      </div>
    </article>
    """


def render_preview_card_actions(match, record, match_run_id, section, return_to):
    if section in {"explore_only", "excluded"}:
        return terminal_status_label(record["status"]) if record else ""
    if section == "also_worth_reviewing":
        return render_preview_light_forms(match, record, match_run_id, return_to, section)
    return render_preview_full_forms(match, record, match_run_id, return_to, section)


def render_preview_full_forms(match, record, match_run_id, return_to, section):
    pipeline_id = record.get("pipeline_item_id") if record else ""
    actions = actions_for_record(record)
    if not actions:
        return terminal_status_label(record["status"]) if record else ""
    return " ".join(
        action_form(
            action,
            action_label_for_record(action, record),
            match_run_id,
            opportunity_key=match_opportunity_key(match),
            pipeline_id=pipeline_id,
            expected_version=record.get("state_version") if record else None,
            resolution_mode=resolution_mode_for_record(action, record),
            return_to=return_to,
            section=section,
        )
        for action in actions
    )


def render_preview_light_forms(match, record, match_run_id, return_to, section):
    actions = explore_actions_for_record(record)
    if not actions:
        return terminal_status_label(record["status"]) if record else ""
    return " ".join(
        action_form(
            action,
            action_label_for_record(action, record),
            match_run_id,
            opportunity_key=match_opportunity_key(match),
            pipeline_id=record.get("pipeline_item_id", "") if record else "",
            expected_version=record.get("state_version") if record else None,
            resolution_mode=resolution_mode_for_record(action, record),
            return_to=return_to,
            section=section,
        )
        for action in actions
    )


def preview_section_note(section):
    notes = {
        "do_these_first": "Start with these. They look the most actionable based on the information provided.",
        "best_matches": "Strong options to review next.",
        "also_worth_reviewing": "Potential fits that may need one extra check.",
        "explore_only": "Useful for broader browsing and QA, but not primary recommendations.",
        "excluded": "Not personalized for this profile. Kept collapsed for QA and trust checks.",
    }
    return notes.get(section, "")


def render_preview_diagnostics(context):
    warnings = "; ".join(context.get("warnings") or []) or "-"
    missing = ", ".join(context.get("missing_fields") or []) or "-"
    ambiguous = ", ".join(context.get("ambiguous_fields") or []) or "-"
    overlay = context.get("metadata_overlay") or {}
    return f"""
    <details class="explore-details preview-diagnostic">
      <summary><span>Advanced diagnostics</span><small>optional QA details</small></summary>
      <p class="muted">Normalizer: {e(context.get('normalizer'))} ({e(context.get('extraction_quality'))})</p>
      <p class="muted">Metadata overlay: {e('enabled' if overlay.get('enabled') else 'disabled')} | records={e(overlay.get('records_loaded'))} | rows enriched={e(overlay.get('rows_enriched'))}</p>
      <p class="muted">Warnings: {e(warnings)}</p>
      <p class="muted">Missing fields: {e(missing)}</p>
      <p class="muted">Ambiguous fields: {e(ambiguous)}</p>
    </details>
    """


def render_header(context, match_run_id):
    profile = context["profile"]
    market = context["market_summary"]
    profile_summary = [
        ("Profile", profile["display_name"]),
        ("Looking for", profile["summary"]),
        ("Languages", demo.join_values(profile["languages"])),
        ("Skills", demo.join_values(profile["skills"])),
    ]
    rows = "".join(
        f"<p><strong>{e(label)}:</strong> {e(value)}</p>"
        for label, value in profile_summary
    )
    find_matches_url = f"/find-matches?{urlencode({'run': match_run_id})}"
    return f"""
    <section class="hero">
      <div>
        <p class="eyebrow">Application Tracker</p>
        <h1>Application Tracker</h1>
        <p class="lead">Manage saved opportunities, applications, tests, and follow-ups.</p>
        <p><a class="jump-link" href="{e(find_matches_url)}">Find new matches</a></p>
      </div>
      <div class="profile-box">
        <p class="eyebrow">Active tracker profile</p>
        {rows}
        <p><strong>Live opportunities tracked:</strong> {market['estimated_market_opportunities']}</p>
      </div>
    </section>
    """


def render_notice(message, error):
    if error:
        return f"<div class='notice error' role='alert'>{e(error)}</div>"
    if message:
        return f"<div class='notice success' role='status'>{e(message)}</div>"
    return ""


def render_actions(actions, card_index, daily_action_status=None):
    daily_action_status = daily_action_status or {}
    handled_today = daily_action_status.get("handled_today_count", 0)
    remaining_budget = daily_action_status.get("remaining_budget", 4)
    note = ""
    if handled_today:
        noun = "item" if handled_today == 1 else "items"
        note = f"<p class='muted'>You've handled {handled_today} {noun} today. Nice.</p>"
    if remaining_budget == 0:
        items = "<li>You've completed today's main plan. More leads are available below if you want to keep going.</li>"
    else:
        items = "".join(render_action_item(action, card_index) for action in actions[:remaining_budget])
        if not items:
            items = "<li>No urgent new applications today. We'll keep watching for strong matches.</li>"
    return f"""
    <section id="do-these-first">
      <h2>Do These First</h2>
      <p class="muted">A short daily plan. You do not need to act on everything today.</p>
      {note}
      <ol class="actions">{items}</ol>
    </section>
    """


def render_secondary_actions(actions, card_index):
    items = "".join(render_action_item(action, card_index) for action in actions[:4])
    if not items:
        items = "<li>You're caught up on today's shortlist. You can still browse Explore Market.</li>"
    return f"""
    <section id="also-worth-reviewing">
      <h2>Also Worth Reviewing</h2>
      <p class="muted">Good matches, but not today's top priority. Worth reviewing when you have more time.</p>
      <ol class="actions secondary-actions">{items}</ol>
    </section>
    """


def render_action_item(action, card_index):
    href = action_href(action, card_index)
    text = demo.make_action_user_facing(action["action"])
    return (
        f"<li>{e(text)} "
        f"<a class='jump-link' href='{e(href)}'>Go to opportunity</a></li>"
    )


def render_matches(title, section_id, matches, tracked, match_run_id, card_index, include_actions, empty):
    cards = []
    for match in matches[:8]:
        record = demo.tracked_record_for_match(match, tracked)
        reasons = "; ".join(demo.plain_reasons(match, record)[:3])
        status = readable_status(record["status"]) if record else "Not tracked yet"
        card_id = card_id_for_match(match, record)
        cards.append(
            f"""
            <article class="card" id="{e(card_id)}" data-action-card>
              <div class="card-main">
                <p class="source">{e(match['source'])}</p>
                <h3>{e(match['display_title'])}</h3>
                <p>{e(match['location'])} &middot; {e(match['expertise'])}</p>
                <p class="muted">{e(reasons)}</p>
                <p class="pill js-card-status">{e(status)}</p>
                <p><a class="back-link" href="#do-these-first">Back to Do These First</a></p>
              </div>
              <div class="card-actions">
                <a class="open" href="{e(match['url'])}" target="_blank" rel="noreferrer">View job</a>
                <div class="js-card-controls">{render_match_forms(match, record, match_run_id, card_id) if include_actions else ""}</div>
              </div>
            </article>
            """
        )
    if not cards:
        cards.append(f"<p class='empty'>{e(empty)}</p>")
    return f"""
    <section id="{e(section_id)}">
      <h2>{e(title)}</h2>
      <div class="stack">{''.join(cards)}</div>
    </section>
    """


def render_explore_market(explore_market, tracked, match_run_id):
    groups = (
        ("Strong fit for you", "strong_fit", "High-fit live opportunities from the broader tracker."),
        ("Possible fit", "possible_fit", "Relevant live opportunities that may be worth browsing."),
        ("Broader market", "broader_market", "A small sample of the wider live AI-work market."),
        ("Already tracked", "already_tracked", "Items already saved, hidden, applied, or otherwise in your tracker."),
        ("Always-open and public leads", "supplemental", "Useful public leads and always-open application paths."),
    )
    group_html = []
    for title, key, note in groups:
        matches = explore_market.get(key, [])
        if not matches:
            continue
        cards = "".join(
            render_explore_card(match, demo.tracked_record_for_match(match, tracked), match_run_id)
            for match in matches
        )
        group_html.append(
            f"""
            <div class="explore-group">
              <h3>{e(title)}</h3>
              <p class="muted">{e(note)}</p>
              <div class="stack">{cards}</div>
            </div>
            """
        )
    if not group_html:
        group_html.append("<p class='empty'>No broader market examples surfaced for this profile today.</p>")
    total_shown = sum(
        len(explore_market.get(key, []))
        for _, key, _ in groups
    )
    return f"""
    <section id="explore-market">
      <details class="explore-details">
        <summary>
          <span>Explore Market</span>
          <small>Browse all tracked AI-work opportunities ({total_shown} shown)</small>
        </summary>
        <p class="muted explore-copy">Your daily plan is intentionally small. Explore Market shows the wider tracked market when you want to browse. These are not tasks for today.</p>
        {''.join(group_html)}
      </details>
    </section>
    """


def render_explore_card(match, record, match_run_id):
    reasons = "; ".join(demo.plain_reasons(match, record)[:3])
    card_id = card_id_for_explore_match(match, record)
    label = explore_match_label(match, record)
    return f"""
    <article class="card explore-card" id="{e(card_id)}" data-action-card>
      <div class="card-main">
        <p class="source">{e(match['source'])}</p>
        <h3>{e(match['display_title'])}</h3>
        <p>{e(match['location'])} &middot; {e(match['expertise'])}</p>
        <p class="muted">{e(reasons)}</p>
        <p class="pill js-card-status">{e(label)}</p>
        <p><a class="back-link" href="#do-these-first">Back to Do These First</a></p>
      </div>
      <div class="card-actions compact-actions">
        <a class="open" href="{e(match['url'])}" target="_blank" rel="noreferrer">View job</a>
        <div class="js-card-controls">{render_explore_forms(match, record, match_run_id, card_id)}</div>
      </div>
    </article>
    """


def explore_match_label(match, record):
    pieces = []
    if record:
        pieces.append("Already in your tracker")
        pieces.append(readable_status(record["status"]))
    else:
        pieces.append(demo.opportunity_type_label(match))
    if match.get("variant_count", 1) > 1:
        pieces.append(f"{match['variant_count']} related postings grouped")
    return " · ".join(pieces)


def normalize_tracker_view(view):
    valid_views = {key for key, _ in TRACKER_FILTERS} | {"hidden"}
    return view if view in valid_views else "all"


def tracker_records_for_view(records, view):
    view = normalize_tracker_view(view)
    if view == "all":
        return [record for record in records if record["visibility"] == "visible"]
    if view == "hidden":
        return [record for record in records if record["visibility"] == "hidden"]
    statuses = TRACKER_FILTER_STATUSES[view]
    return [
        record
        for record in records
        if record["visibility"] == "visible"
        and record["workflow_status"] in statuses
    ]


def tracker_filter_href(match_run_id, view):
    params = {"run": match_run_id}
    if view != "all":
        params["view"] = view
    return "/tracker?" + urlencode(params)


def tracker_filter_current(view, candidate):
    return ' aria-current="true"' if view == candidate else ""


def render_my_jobs_workspace(records, match_run_id, tracker_view="all"):
    view = normalize_tracker_view(tracker_view)
    if not records:
        find_matches_url = "/find-matches?" + urlencode({"run": match_run_id})
        return f"""
        <section id="my-jobs-list" class="my-jobs-workspace">
          <div class="my-jobs-empty">
            <p>You haven&apos;t saved any jobs yet.</p>
            <a class="open button-primary" href="{e(find_matches_url)}">Find matches</a>
          </div>
        </section>
        """

    filtered = tracker_records_for_view(records, view)
    hidden_count = sum(1 for record in records if record["visibility"] == "hidden")
    filters = "".join(
        f'<a class="tracker-filter" href="{e(tracker_filter_href(match_run_id, key))}"'
        f'{tracker_filter_current(view, key)}>{e(label)}</a>'
        for key, label in TRACKER_FILTERS
    )
    hidden_link = ""
    if hidden_count:
        hidden_label = f"Show hidden ({hidden_count})" if view != "hidden" else f"Hidden ({hidden_count})"
        hidden_link = (
            f'<a class="show-hidden" href="{e(tracker_filter_href(match_run_id, "hidden"))}"'
            f'{tracker_filter_current(view, "hidden")}>{e(hidden_label)}</a>'
        )
    cards = "".join(
        render_my_jobs_card(record, match_run_id, tracker_view=view)
        for record in filtered
    )
    if not cards:
        cards = '<p class="empty my-jobs-filter-empty">No jobs in this view.</p>'
    return f"""
    <section id="my-jobs-list" class="my-jobs-workspace">
      <div class="my-jobs-filter-row">
        <nav class="tracker-filters" aria-label="Filter My Jobs">{filters}</nav>
        {hidden_link}
      </div>
      <div class="stack my-jobs-stack">{cards}</div>
    </section>
    """


def render_my_jobs_card(record, match_run_id, tracker_view="all"):
    status = record["status"]
    card_id = card_id_for_record(record)
    reminder = render_reminder_note(record)
    next_action = record.get("next_action") or ""
    if status == "expired":
        next_action = "This job is no longer available."
    controls = render_my_jobs_forms(
        record, match_run_id, card_id, tracker_view=tracker_view
    )
    view_class = (
        "button-primary"
        if status in TRACKER_FILTER_STATUSES["saved"]
        else "button-secondary"
    )
    return f"""
    <article class="card tracker my-job-card" id="{e(card_id)}" data-action-card data-job-status="{e(status)}" data-state-version="{e(record.get('state_version'))}">
      <div class="card-main">
        <p class="source">{e(record['source'])}</p>
        <h3>{e(record['title'])}</h3>
        <p class="pill card-status js-card-status" aria-label="Current status: {e(readable_status(status))}"><span class="visually-hidden">Current status: </span>{e(readable_status(status))}</p>
        {reminder}
        {f'<p class="muted next-step">{e(next_action)}</p>' if next_action else ''}
      </div>
      <div class="card-actions my-job-actions">
        {f'<a class="open {view_class}" href="{e(record["url"])}" target="_blank" rel="noreferrer">View job</a>' if record["url"] else ''}
        <div class="js-card-controls">{controls}</div>
      </div>
    </article>
    """


def render_my_jobs_forms(record, match_run_id, return_to, tracker_view="all"):
    actions = actions_for_record(record)
    if not actions:
        return ""
    return " ".join(
        action_form(
            action,
            action_label_for_record(action, record),
            match_run_id,
            pipeline_id=record.get("pipeline_item_id", ""),
            expected_version=record.get("state_version"),
            resolution_mode=resolution_mode_for_record(action, record),
            return_to=return_to,
            section="tracker",
            tracker_view=tracker_view,
            visual_variant=my_jobs_action_variant(record["status"], action),
        )
        for action in actions
    )


def my_jobs_action_variant(status, action):
    if status in {"applied", "waiting", "assessment_invited"} and action == "assessment_started":
        return "primary"
    if status == "assessment_started" and action == "assessment_completed":
        return "primary"
    if status == "not_interested" and action == "show_again":
        return "secondary"
    return action_visual_variant(action)


def render_pipeline(records, match_run_id, tracker_only=False):
    groups = pipeline_groups(records)
    active_body = render_pipeline_group(
        groups["active"],
        match_run_id,
        "No active application items yet.",
        tracker_only=tracker_only,
    )
    accepted_body = render_pipeline_group(
        groups["accepted"],
        match_run_id,
        "No accepted or active work items yet.",
        tracker_only=tracker_only,
    )
    hidden_body = render_pipeline_group(
        groups["hidden"],
        match_run_id,
        "No hidden opportunities.",
        tracker_only=tracker_only,
    )
    closed_body = render_pipeline_group(
        groups["closed"],
        match_run_id,
        "No closed or expired items.",
        tracker_only=tracker_only,
    )
    return f"""
    <section id="application-tracker">
      <h2>Your Application Tracker</h2>
      <div class="stack">{active_body}</div>
      <h3 class="tracker-heading">Active / Accepted</h3>
      <div class="stack">{accepted_body}</div>
      <h3 class="tracker-heading">Hidden / Not Interested</h3>
      <div class="stack">{hidden_body}</div>
      <h3 class="tracker-heading">Closed / Expired</h3>
      <div class="stack">{closed_body}</div>
    </section>
    """


def pipeline_groups(records):
    groups = {
        "active": [],
        "accepted": [],
        "hidden": [],
        "closed": [],
    }
    for record in sorted(records, key=demo.pipeline_sort_key):
        status = record["status"]
        if status in ACCEPTED_STATUSES:
            groups["accepted"].append(record)
        elif status in HIDDEN_STATUSES:
            groups["hidden"].append(record)
        elif status in CLOSED_STATUSES:
            groups["closed"].append(record)
        else:
            groups["active"].append(record)
    return groups


def render_pipeline_group(records, match_run_id, empty, tracker_only=False):
    if not records:
        return f"<p class='empty'>{e(empty)}</p>"
    return "".join(
        render_pipeline_card(record, match_run_id, tracker_only=tracker_only)
        for record in records
    )


def render_reminder_note(record):
    if record.get("reminder_date"):
        return f"<p class='reminder-note'>Reminder set for {e(record['reminder_date'])}.</p>"
    return ""


def render_pipeline_card(record, match_run_id, tracker_only=False):
    navigation = (
        f'<p><a class="back-link" href="{e("/find-matches?" + urlencode({"run": match_run_id}))}">Back to Matches</a></p>'
        if tracker_only
        else '<p><a class="back-link" href="#do-these-first">Back to Do These First</a></p>'
    )
    return f"""
    <article class="card tracker" id="{e(card_id_for_record(record))}" data-action-card>
      <div class="card-main">
        <p class="source">{e(record['source'])}</p>
        <h3>{e(record['title'])}</h3>
        <p class="pill js-card-status">{e(readable_status(record['status']))}</p>
        {render_reminder_note(record)}
        <p class="muted">{e(record['next_action'])}</p>
        {navigation}
      </div>
      <div class="card-actions">
        {f'<a class="open" href="{e(record["url"])}" target="_blank" rel="noreferrer">View job</a>' if record["url"] else ""}
        <div class="js-card-controls">{render_pipeline_forms(record, match_run_id, card_id_for_record(record))}</div>
      </div>
    </article>
    """


def render_applicant_signals(applicant_signals):
    summary = applicant_signals["summary"]
    rows = applicant_signals["source_signals"][:5]
    if rows:
        items = "".join(
            f"""
            <tr>
              <td>{e(row['source'])}</td>
              <td>{row['reports']}</td>
              <td>{row['assessment_reports']}</td>
              <td>{e(demo.readable_signal(row['signal_label']))}</td>
            </tr>
            """
            for row in rows
        )
        table = f"""
        <table>
          <thead><tr><th>Source</th><th>Reports</th><th>Assessments</th><th>Signal</th></tr></thead>
          <tbody>{items}</tbody>
        </table>
        """
    else:
        table = "<p class='empty'>No relevant applicant signals yet.</p>"
    return f"""
    <section id="applicant-signals">
      <h2>Applicant Signals</h2>
      <p class="muted">Directional sample signals from similar tracked activity. They are not guarantees.</p>
      <p><strong>{summary['total_updates']}</strong> relevant reports &middot; <strong>{summary['assessment_updates']}</strong> assessment-related reports</p>
      {table}
    </section>
    """


def render_disclaimer():
    return """
    <section class="disclaimer">
      <h2>Prototype Notes</h2>
      <p>This is a local prototype using sample/product-state data on this machine. Applicant signals are directional and mock-like for product exploration. Actions update only local product-state tables.</p>
    </section>
    """


def render_tracker_disclaimer():
    return """
    <section class="disclaimer">
      <h2>Local Prototype</h2>
      <p>Tracker pages are read-only until you explicitly click an action. Actions update only local product-state tables for the active tracker profile.</p>
    </section>
    """


def render_match_forms(match, record, match_run_id, return_to):
    pipeline_id = record.get("pipeline_item_id") if record else ""
    actions = actions_for_record(record)
    if not actions:
        return terminal_status_label(record["status"]) if record else ""
    return " ".join(
        action_form(
            action,
            action_label_for_record(action, record),
            match_run_id,
            opportunity_key=match_opportunity_key(match),
            pipeline_id=pipeline_id,
            expected_version=record.get("state_version") if record else None,
            resolution_mode=resolution_mode_for_record(action, record),
            return_to=return_to,
            section="dashboard",
        )
        for action in actions
    )


def render_explore_forms(match, record, match_run_id, return_to):
    actions = explore_actions_for_record(record)
    if not actions:
        return terminal_status_label(record["status"]) if record else ""
    return " ".join(
        action_form(
            action,
            action_label_for_record(action, record),
            match_run_id,
            opportunity_key=match_opportunity_key(match),
            pipeline_id=record.get("pipeline_item_id", "") if record else "",
            expected_version=record.get("state_version") if record else None,
            resolution_mode=resolution_mode_for_record(action, record),
            return_to=return_to,
            section="explore",
        )
        for action in actions
    )


def render_pipeline_forms(record, match_run_id, return_to):
    actions = actions_for_record(record)
    if not actions:
        return terminal_status_label(record["status"])
    return " ".join(
        action_form(
            action,
            action_label_for_record(action, record),
            match_run_id,
            pipeline_id=record.get("pipeline_item_id", ""),
            expected_version=record.get("state_version"),
            resolution_mode=resolution_mode_for_record(action, record),
            return_to=return_to,
            section="tracker",
        )
        for action in actions
    )


def action_form(
    action,
    label,
    match_run_id,
    opportunity_key="",
    pipeline_id="",
    expected_version=None,
    resolution_mode="",
    return_to="preview-recommendations",
    section="",
    tracker_view="",
    visual_variant=None,
):
    visual_variant = visual_variant or action_visual_variant(action)
    version_field = (
        f'<input type="hidden" name="expected_version" value="{e(expected_version)}">'
        if pipeline_id and expected_version is not None
        else ""
    )
    resolution_field = (
        f'<input type="hidden" name="resolution_mode" value="{e(resolution_mode)}">'
        if resolution_mode
        else ""
    )
    return f"""
    <form method="post" action="/action" class="js-inline-action action-form action-form-{e(action)} action-{e(visual_variant)}">
      <input type="hidden" name="match_run_id" value="{e(match_run_id)}">
      <input type="hidden" name="action" value="{e(action)}">
      <input type="hidden" name="idempotency_key" value="{e(secrets.token_urlsafe(24))}">
      <input type="hidden" name="opportunity_key" value="{e(opportunity_key)}">
      <input type="hidden" name="pipeline_item_id" value="{e(str(pipeline_id or ''))}">
      {version_field}
      {resolution_field}
      <input type="hidden" name="return_to" value="{e(return_to)}">
      <input type="hidden" name="section" value="{e(section)}">
      <input type="hidden" name="tracker_view" value="{e(tracker_view)}">
      <button type="submit" class="action-button">{e(label)}</button>
    </form>
    """


def action_visual_variant(action):
    if action in {"save", "applied", "assessment_started", "assessment_completed"}:
        return "secondary"
    return "tertiary"


def action_json_payload(result, run, form):
    item = result["item"]
    section = first_value(form, "section")
    tracker_view = first_value(form, "tracker_view") or "all"
    return_to = first_value(form, "return_to") or "preview-recommendations"
    actions = actions_for_record(item)
    labels = ACTION_LABELS
    if section in {"also_worth_reviewing", "explore"}:
        actions = explore_actions_for_record(item)
    if section in profile_preview.SECTION_ORDER:
        labels = ACTION_LABELS
    controls = " ".join(
        action_form(
            action,
            action_label_for_record(action, item),
            run.match_run_id,
            opportunity_key=first_value(form, "opportunity_key"),
            pipeline_id=item["pipeline_item_id"],
            expected_version=item["state_version"],
            resolution_mode=resolution_mode_for_record(action, item),
            return_to=return_to,
            section=section,
            tracker_view=tracker_view if section == "tracker" else "",
            visual_variant=(
                my_jobs_action_variant(item["status"], action)
                if section == "tracker"
                else None
            ),
        )
        for action in actions
    )
    if not controls:
        controls = terminal_status_label(item["status"])
    remains_in_view = record_remains_in_browser_view(
        item,
        section=section,
        tracker_view=tracker_view,
    )
    all_records = result["all_records"]
    hidden_count = sum(
        1 for record in all_records if record["visibility"] == "hidden"
    )
    current_view_count = (
        len(tracker_records_for_view(all_records, tracker_view))
        if section == "tracker"
        else visible_match_run_count(run, all_records)
    )
    payload = {
        "ok": True,
        "message": result["message"],
        "card_id": return_to,
        "opportunity_key": first_value(form, "opportunity_key"),
        "source": result["source"],
        "title": result["title"],
        "pipeline_item_id": item["pipeline_item_id"],
        "status": item["status"],
        "status_label": readable_status(item["status"]),
        "controls_html": controls,
        "state_version": item["state_version"],
        "replayed": result["replayed"],
        "reminder_date": item["reminder_date"],
        "next_action": item["next_action"],
        "remains_in_view": remains_in_view,
        "remove_card": not remains_in_view,
        "hidden_count": hidden_count,
        "current_view_count": current_view_count,
    }
    if section == "tracker":
        payload["workspace_html"] = render_my_jobs_workspace(
            all_records,
            run.match_run_id,
            tracker_view,
        )
        payload["tracker_header_html"] = render_lightweight_tracker_header(
            all_records
        )
    return payload


def record_remains_in_browser_view(record, *, section, tracker_view):
    if section == "tracker":
        return bool(tracker_records_for_view([record], tracker_view))
    return record["status"] not in MAIN_RECOMMENDATION_EXCLUDED_STATUSES


def visible_match_run_count(run, records):
    tracked = demo.build_tracked_index(records)
    return sum(
        1
        for match in build_browser_presentation_matches(run.recommendation_context)
        if (
            (record := demo.tracked_record_for_match(match, tracked)) is None
            or record["status"] not in MAIN_RECOMMENDATION_EXCLUDED_STATUSES
        )
    )


def render_inline_action_script():
    return """
    <script>
    (() => {
      const genericFailure = "We couldn't update this job. Try again.";
      const userFacingError = (message) => {
        const error = new Error(message);
        error.userFacing = true;
        return error;
      };
      const showCardMessage = (card, message, isError = false) => {
        let notice = card.querySelector(".js-action-feedback");
        if (!notice) {
          notice = document.createElement("p");
          notice.className = "js-action-feedback action-feedback";
          (card.querySelector(".card-main") || card).append(notice);
        }
        notice.className = `js-action-feedback action-feedback ${isError ? "error" : "success"}`;
        notice.setAttribute("role", isError ? "alert" : "status");
        notice.setAttribute("aria-live", isError ? "assertive" : "polite");
        notice.textContent = message;
      };
      const showPageMessage = (message, isError = false) => {
        const region = document.querySelector("#action-feedback");
        if (!region) return;
        region.innerHTML = "";
        const notice = document.createElement("div");
        notice.className = `notice ${isError ? "error" : "success"}`;
        notice.setAttribute("role", isError ? "alert" : "status");
        notice.textContent = message;
        region.append(notice);
      };
      document.addEventListener("submit", async (event) => {
        const form = event.target.closest("form.js-inline-action");
        if (!form) return;
        event.preventDefault();
        const card = form.closest("[data-action-card]");
        if (!card || card.dataset.actionPending === "true") return;
        card.dataset.actionPending = "true";
        const buttons = [...card.querySelectorAll(".js-card-controls button")];
        buttons.forEach((button) => { button.disabled = true; });
        try {
          const endpoint = form.getAttribute("action") || "/action";
          const response = await fetch(endpoint, {
            method: "POST",
            headers: {
              "Accept": "application/json",
              "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
              "X-Wahojobs-Inline-Action": "1",
            },
            body: new URLSearchParams(new FormData(form)),
            redirect: "error",
          });
          const contentType = response.headers.get("Content-Type") || "";
          if (!contentType.toLowerCase().includes("application/json")) {
            const responseText = await response.text();
            console.error("Inline action expected JSON but received another response type.", {
              status: response.status,
              url: response.url,
              contentType,
              bodyPrefix: responseText.slice(0, 240),
            });
            throw userFacingError(genericFailure);
          }
          let payload;
          try {
            payload = await response.json();
          } catch (parseError) {
            console.error("Inline action returned invalid JSON.", parseError);
            throw userFacingError(genericFailure);
          }
          if (!response.ok) {
            throw userFacingError(payload.error || genericFailure);
          }
          if (!payload.ok) {
            throw userFacingError(payload.error || "The opportunity was not updated. Please try again.");
          }
          if (payload.workspace_html) {
            const workspace = document.querySelector("#my-jobs-list");
            const trackerHeader = document.querySelector(".my-jobs-header");
            if (workspace) workspace.outerHTML = payload.workspace_html;
            if (trackerHeader && payload.tracker_header_html) {
              trackerHeader.outerHTML = payload.tracker_header_html;
            }
            showPageMessage(payload.message);
            return;
          }
          if (payload.remove_card) {
            const container = card.parentElement;
            card.remove();
            const count = document.querySelector(".results-summary strong");
            if (count) {
              const noun = payload.current_view_count === 1 ? "match" : "matches";
              count.textContent = `${payload.current_view_count} ${noun}`;
            }
            if (container && !container.querySelector("[data-action-card]")) {
              const empty = document.createElement("p");
              empty.className = "empty";
              empty.textContent = "No personalized opportunities remain in this view.";
              container.append(empty);
            }
            showPageMessage(payload.message);
            return;
          }
          const status = card.querySelector(".js-card-status");
          const controls = card.querySelector(".js-card-controls");
          if (status) {
            status.textContent = payload.status_label;
            status.setAttribute("aria-label", `Current status: ${payload.status_label}`);
          }
          card.dataset.stateVersion = String(payload.state_version);
          let reminder = card.querySelector(".reminder-note");
          if (payload.reminder_date) {
            if (!reminder) {
              reminder = document.createElement("p");
              reminder.className = "reminder-note";
              (card.querySelector(".card-main") || card).append(reminder);
            }
            reminder.textContent = `Reminder set for ${payload.reminder_date}.`;
          } else if (reminder) {
            reminder.remove();
          }
          const nextStep = card.querySelector(".next-step");
          if (nextStep) nextStep.textContent = payload.next_action || "";
          if (controls) controls.innerHTML = payload.controls_html;
          showCardMessage(card, payload.message);
        } catch (error) {
          buttons.forEach((button) => { button.disabled = false; });
          if (!error || !error.userFacing) {
            console.error("Inline action request failed.", error);
          }
          const message = error && error.userFacing ? error.message : genericFailure;
          showCardMessage(card, message, true);
        } finally {
          card.dataset.actionPending = "false";
        }
      });
    })();
    </script>
    """


def actions_for_status(status):
    return STATUS_ACTIONS.get(status, ())


def actions_for_record(record):
    if record is None:
        return STATUS_ACTIONS[None]
    if record.get("integrity_error"):
        return ()
    if record.get("visibility") == "hidden":
        return ("show_again",)
    if record.get("workflow_status") is None:
        if not record.get("reminder_at"):
            return ()
        return ("save", "applied", "remind_later", "not_interested")
    return actions_for_status(record["workflow_status"])


def action_label_for_record(action, record):
    if (
        action == "show_again"
        and record
        and record.get("workflow_status") is None
        and record.get("workflow_status_provenance") == "unknown_legacy"
    ):
        return "Show again as Saved"
    return ACTION_LABELS[action]


def resolution_mode_for_record(action, record):
    if (
        action == "show_again"
        and record
        and record.get("visibility") == "hidden"
        and record.get("workflow_status") is None
        and record.get("workflow_status_provenance") == "unknown_legacy"
    ):
        return "as_saved"
    return ""


def readable_status(status):
    return STATUS_LABELS.get(status, "Status unavailable")


def explore_actions_for_record(record):
    if record is None:
        return ("save", "not_interested")
    if record.get("visibility") == "visible" and "not_interested" in actions_for_record(record):
        return ("not_interested",)
    return ()


def terminal_status_label(status):
    return f"<p class='status-note'>{e(readable_status(status))}</p>"


def action_note(action):
    labels = {
        "show_again": "Shown again from local UI",
        "save": "Saved from local UI",
        "applied": "Marked applied from local UI",
        "assessment_started": "Marked assessment started from local UI",
        "assessment_completed": "Marked assessment completed from local UI",
        "remind_later": "Reminder set from local UI",
        "not_interested": "Marked not interested from local UI",
        "accepted": "Marked accepted from local UI",
        "rejected": "Marked rejected from local UI",
    }
    return labels[action]


def action_success_message(action):
    labels = {
        "show_again": "Shown in My Jobs again.",
        "save": "Saved to My Jobs.",
        "applied": "Marked as applied.",
        "assessment_started": "Assessment started.",
        "assessment_completed": "Assessment marked complete.",
        "remind_later": "Reminder set.",
        "not_interested": "Marked not interested.",
        "accepted": "Marked as accepted.",
        "rejected": "Marked as not selected.",
    }
    return labels[action]


def reminder_success_message(reminder_date):
    return f"Reminder set for {reminder_date}."


def render_error(message):
    return f"""
    <!doctype html>
    <html lang="en">
    <head><meta charset="utf-8"><title>Wahojobs Local UI</title><style>{CSS}</style></head>
    <body><main><section><h1>Wahojobs Local UI</h1><div class="notice error">{e(message)}</div></section></main></body>
    </html>
    """


ACTION_REQUIRED_SINGLE_FIELDS = {
    "action",
    "idempotency_key",
    "match_run_id",
    "pipeline_item_id",
    "return_to",
    "section",
}
ACTION_OPTIONAL_SINGLE_FIELDS = {
    "expected_version",
    "opportunity_key",
    "resolution_mode",
    "tracker_view",
}
ACTION_FIELD_ALIASES = {
    "item_id",
    "pipeline_id",
    "opportunity_id",
    "run_id",
    "state_version",
    "version",
    "resolution",
}


def validate_action_form(form):
    if ACTION_FIELD_ALIASES.intersection(form):
        raise MalformedActionRequest()
    for key in ACTION_REQUIRED_SINGLE_FIELDS:
        action_form_value(form, key, allow_empty=key == "pipeline_item_id")
    for key in ACTION_OPTIONAL_SINGLE_FIELDS.intersection(form):
        action_form_value(
            form,
            key,
            allow_empty=key in {"opportunity_key", "tracker_view"},
        )

    action = action_form_value(form, "action")
    pipeline_item_id = action_form_value(form, "pipeline_item_id", allow_empty=True)
    opportunity_key = optional_action_form_value(
        form, "opportunity_key", allow_empty=True
    )
    idempotency_key = action_form_value(form, "idempotency_key")
    tracker_view = optional_action_form_value(form, "tracker_view", allow_empty=True)
    section = action_form_value(form, "section")

    if action not in ACTION_STATUSES:
        raise MalformedActionRequest()
    if idempotency_key.startswith(pipeline_actions.INTERNAL_IDEMPOTENCY_PREFIX):
        raise MalformedActionRequest()
    if tracker_view and tracker_view != normalize_tracker_view(tracker_view):
        raise MalformedActionRequest()
    valid_sections = set(profile_preview.SECTION_ORDER) | {
        "dashboard",
        "explore",
        "tracker",
    }
    if section not in valid_sections:
        raise MalformedActionRequest()

    if pipeline_item_id:
        required_expected_version(form)
    elif "expected_version" in form or not opportunity_key:
        raise MalformedActionRequest()

    if "resolution_mode" in form:
        resolution_mode = action_form_value(form, "resolution_mode")
        if action != "show_again" or resolution_mode != "as_saved":
            raise MalformedActionRequest()


def action_form_value(form, key, *, allow_empty=False):
    values = form.get(key)
    if not isinstance(values, list) or len(values) != 1:
        raise MalformedActionRequest()
    value = values[0]
    if not isinstance(value, str) or value != value.strip():
        raise MalformedActionRequest()
    if not allow_empty and not value:
        raise MalformedActionRequest()
    return value


def optional_action_form_value(form, key, *, allow_empty=False):
    if key not in form:
        return ""
    return action_form_value(form, key, allow_empty=allow_empty)


def safe_action_context_value(form, key):
    try:
        return action_form_value(form, key)
    except ActionError:
        return ""


def first_value(values, key):
    value = values.get(key, [""])
    return value[0].strip() if value else ""


def required_form_value(values, key):
    value = first_value(values, key)
    if not value:
        raise SystemExit(f"Missing required form field: {key}")
    return value


def e(value):
    return html.escape(str(value or ""), quote=True)


CSS = """
:root {
  color-scheme: light;
  --bg: #F5F7F6;
  --panel: #FFFFFF;
  --surface: #FFFFFF;
  --surface-subtle: #EFF4F1;
  --ink: #17211C;
  --muted: #5B6861;
  --line: #D9E0DC;
  --accent: #176B52;
  --accent-hover: #11533F;
  --accent-soft: #E7F3EE;
  --focus: #2563EB;
  --warn: #7A3B24;
  --ok: #235D38;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 16px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.app-header {
  background: rgba(255, 255, 255, .96);
  border-bottom: 1px solid var(--line);
  position: sticky;
  top: 0;
  z-index: 20;
}
.app-header-inner {
  align-items: center;
  display: flex;
  height: 64px;
  justify-content: space-between;
  margin: 0 auto;
  padding: 0 24px;
  width: min(1080px, 100%);
}
.wordmark {
  color: var(--ink);
  font-size: 1.08rem;
  font-weight: 800;
  text-decoration: none;
}
.app-main { margin: 0 auto; padding: 36px 24px 64px; width: min(1080px, 100%); }
.app-main > #action-feedback:empty { display: none; }
.product-nav {
  align-items: center;
  display: flex;
  gap: 24px;
  height: 100%;
}
.product-nav a {
  color: var(--muted);
  font-weight: 700;
  align-items: center;
  display: inline-flex;
  height: 100%;
  padding: 2px 0 0;
  text-decoration: none;
}
.product-nav a[aria-current="page"] { box-shadow: inset 0 -2px var(--accent); color: var(--ink); }
section { margin: 24px 0; }
section, .card { scroll-margin-top: 82px; }
.hero {
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(280px, .8fr);
  gap: 18px;
  align-items: stretch;
}
.hero > div, .profile-box, .card, .notice, .disclaimer {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 18px;
}
h1, h2, h3, p { margin-top: 0; }
h1 { font-size: 36px; line-height: 1.15; margin-bottom: 12px; }
h2 { font-size: 1.4rem; line-height: 1.25; margin-bottom: 10px; }
h3 { font-size: 1.02rem; margin-bottom: 8px; }
.lead { color: var(--muted); font-size: 1.08rem; max-width: 56ch; }
.eyebrow, .source { color: var(--accent); font-size: .78rem; font-weight: 800; letter-spacing: 0; margin-bottom: 8px; text-transform: uppercase; }
.page-intro { margin: 18px 0 28px; max-width: 720px; }
.page-intro .lead { font-size: 1.12rem; }
.results-header { margin: 8px 0 20px; }
.results-header h1 { margin-bottom: 8px; }
.results-summary { color: var(--muted); margin-bottom: 0; }
.results-summary strong { color: var(--ink); }
.my-jobs-header { margin: 8px 0 22px; max-width: 720px; }
.my-jobs-header h1 { margin-bottom: 8px; }
.my-jobs-header .lead { margin-bottom: 14px; }
.my-jobs-summary { color: var(--muted); display: flex; flex-wrap: wrap; gap: 6px 0; margin-bottom: 0; }
.my-jobs-summary span:not(:last-child)::after { color: #9AA49E; content: "\\2022"; margin: 0 9px; }
.profile-switcher {
  align-items: end;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 18px;
}
.profile-switcher label {
  color: var(--muted);
  display: block;
  flex-basis: 100%;
  font-weight: 700;
}
.profile-switcher select {
  background: white;
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--ink);
  font: inherit;
  min-height: 40px;
  min-width: min(360px, 100%);
  padding: 6px 9px;
}
.tracker-profile-panel {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  justify-content: space-between;
}
.tracker-profile-panel > div { flex: 1 1 420px; }
.tracker-profile-panel .profile-switcher { flex: 0 1 380px; margin-top: 0; }
.preview-form {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  display: grid;
  gap: 12px;
  padding: 22px;
}
.preview-form label {
  color: var(--ink);
  font-weight: 700;
}
.preview-form textarea, .preview-form select {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--ink);
  font: inherit;
  padding: 11px 12px;
  width: 100%;
}
.preview-form textarea { min-height: 150px; resize: vertical; }
.preview-form > button { justify-self: start; }
.preview-input { max-width: 760px; }
.field-help { color: var(--muted); margin: -6px 0 0; }
.advanced-options {
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 10px;
}
.advanced-options summary {
  cursor: pointer;
  font-weight: 700;
}
.preview-grid {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.profile-context {
  align-items: center;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  display: flex;
  gap: 20px;
  justify-content: space-between;
  padding: 14px 16px;
}
.profile-context p { margin-bottom: 4px; }
.profile-context .secondary-link { flex: 0 0 auto; }
.profile-context-label { color: var(--muted); font-size: .82rem; font-weight: 700; }
.profile-summary-line {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 0;
  list-style: none;
  margin: 0;
  padding: 0;
}
.profile-summary-line li { color: var(--ink); font-size: .94rem; }
.profile-summary-line li:not(:last-child)::after { color: #9AA49E; content: "\\2022"; margin: 0 9px; }
.preview-edit-intro { max-width: 760px; }
.ranked-matches { margin-top: 16px; }
.match-rank {
  align-items: center;
  background: var(--accent-soft);
  border-radius: 6px;
  color: var(--accent);
  display: flex;
  font-size: 1.05rem;
  font-weight: 800;
  height: 36px;
  justify-content: center;
  width: 36px;
}
.demo-persona-switcher {
  border-top: 1px solid var(--line);
  padding-top: 18px;
}
.demo-persona-switcher form { margin: 0; }
.qa-details > section { margin-left: 0; margin-right: 0; }
.sample-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}
.sample-label {
  color: var(--muted);
  font-weight: 700;
  margin-bottom: 8px;
}
.sample-loaded-note { margin-bottom: 10px; }
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 12px 0;
}
.chip {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 999px;
  display: inline-flex;
  padding: 6px 10px;
}
.chip strong { margin-right: 4px; }
.snapshot-note {
  background: #fbfaf7;
  border: 1px solid var(--line);
  border-radius: 8px;
  color: var(--muted);
  padding: 10px 12px;
}
.stack { display: grid; gap: 10px; }
.card {
  align-items: start;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 14px;
}
.card.ranked-card {
  align-items: start;
  gap: 16px;
  grid-template-columns: 44px minmax(0, 1fr) 176px;
  padding: 16px;
}
.ranked-card .card-main { min-width: 0; }
.ranked-card .card-main > p:last-child { margin-bottom: 0; }
.card:target {
  border-color: var(--accent);
  background: #F4FBF7;
  box-shadow: 0 0 0 3px rgba(23, 107, 82, .12);
}
.card-actions { align-items: flex-start; display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; max-width: 360px; }
.card-actions form { margin: 0; }
.ranked-card .card-actions {
  align-content: start;
  display: grid;
  gap: 8px;
  grid-template-columns: 1fr;
  justify-items: stretch;
  max-width: 176px;
  width: 176px;
}
.ranked-card .js-card-controls { display: grid; gap: 8px; }
.ranked-card .action-form, .ranked-card .action-button, .ranked-card .button-primary { width: 100%; }
button, .open {
  background: var(--accent);
  border: 1px solid var(--accent);
  border-radius: 6px;
  color: white;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  font: inherit;
  font-weight: 700;
  justify-content: center;
  min-height: 40px;
  overflow-wrap: anywhere;
  padding: 8px 12px;
  text-align: center;
  white-space: normal;
  text-decoration: none;
}
button:hover, .open:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
button:disabled { cursor: wait; opacity: .58; }
.open { background: var(--accent-soft); color: var(--accent); }
.open:hover { color: white; }
.button-primary { background: var(--accent); color: white; }
.ranked-card .action-secondary .action-button {
  background: var(--panel);
  border-color: #AFC0B8;
  color: var(--accent);
}
.ranked-card .action-secondary .action-button:hover { background: var(--accent-soft); border-color: var(--accent); }
.ranked-card .action-tertiary .action-button {
  background: transparent;
  border-color: transparent;
  color: var(--muted);
}
.ranked-card .action-tertiary .action-button:hover { background: var(--surface-subtle); border-color: var(--line); color: var(--ink); }
.card-status:empty { display: none; }
.my-jobs-workspace { margin-top: 16px; }
.my-jobs-filter-row {
  align-items: center;
  display: flex;
  gap: 16px;
  justify-content: space-between;
  margin-bottom: 14px;
  min-width: 0;
}
.tracker-filters {
  display: flex;
  gap: 6px;
  max-width: 100%;
  min-width: 0;
  overflow-x: auto;
  padding: 3px;
  scrollbar-width: thin;
}
.tracker-filter, .show-hidden {
  border: 1px solid transparent;
  border-radius: 6px;
  color: var(--muted);
  flex: 0 0 auto;
  font-size: .92rem;
  font-weight: 700;
  min-height: 40px;
  padding: 8px 11px;
  text-decoration: none;
}
.tracker-filter:hover, .show-hidden:hover { background: var(--surface-subtle); color: var(--ink); }
.tracker-filter[aria-current="true"], .show-hidden[aria-current="true"] {
  background: var(--accent-soft);
  border-color: #B8D4C9;
  color: var(--accent);
}
.show-hidden { font-weight: 600; white-space: nowrap; }
.my-jobs-stack { gap: 10px; }
.card.my-job-card {
  gap: 18px;
  grid-template-columns: minmax(0, 1fr) 200px;
  padding: 16px;
}
.my-job-card .card-main { min-width: 0; }
.my-job-card .card-main h3 { font-size: 1.08rem; }
.my-job-actions {
  align-content: start;
  display: grid;
  gap: 8px;
  grid-template-columns: 1fr;
  max-width: 200px;
  width: 200px;
}
.my-job-actions .js-card-controls { display: grid; gap: 8px; }
.my-job-actions .action-form, .my-job-actions .action-button, .my-job-actions .open { width: 100%; }
.button-secondary, .my-job-card .action-secondary .action-button {
  background: var(--panel);
  border-color: #AFC0B8;
  color: var(--accent);
}
.button-secondary:hover, .my-job-card .action-secondary .action-button:hover {
  background: var(--accent-soft);
  border-color: var(--accent);
  color: var(--accent);
}
.my-job-card .action-primary .action-button { background: var(--accent); color: white; }
.my-job-card .action-primary .action-button:hover { background: var(--accent-hover); }
.my-job-card .action-tertiary .action-button {
  background: transparent;
  border-color: transparent;
  color: var(--muted);
}
.my-job-card .action-tertiary .action-button:hover {
  background: var(--surface-subtle);
  border-color: var(--line);
  color: var(--ink);
}
.reminder-note { color: var(--accent); font-weight: 700; margin-bottom: 8px; }
.next-step { margin-bottom: 0; max-width: 68ch; }
.my-jobs-empty, .my-jobs-filter-empty {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 22px;
}
.my-jobs-empty p { font-weight: 700; }
.visually-hidden {
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  height: 1px;
  overflow: hidden;
  position: absolute;
  white-space: nowrap;
  width: 1px;
}
.pill {
  display: inline-block;
  background: var(--accent-soft);
  color: var(--accent);
  padding: 4px 8px;
  border-radius: 999px;
  font-size: .86rem;
  margin-bottom: 0;
}
.caution {
  border-left: 3px solid #bf8700;
  color: #5f4b00;
  padding-left: 10px;
}
.technical-details {
  color: var(--muted);
  max-width: 300px;
}
.technical-details summary {
  cursor: pointer;
  font-weight: 700;
}
.status-note {
  color: var(--muted);
  font-weight: 700;
  margin: 0;
  padding: 6px 0;
}
.profile-review-form { display: grid; gap: 12px; margin: 0 auto; max-width: 920px; }
.review-section {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  margin: 0;
  padding: 20px;
}
.review-section-primary { border-color: #B8D4C9; }
.review-section-heading {
  align-items: start;
  display: flex;
  gap: 16px;
  justify-content: space-between;
  margin-bottom: 16px;
}
.review-section-heading h2 { font-size: 1.15rem; margin-bottom: 3px; }
.review-section-heading p { color: var(--muted); margin: 0; }
.inference-label {
  background: var(--surface-subtle);
  border-radius: 999px;
  color: var(--muted);
  flex: 0 0 auto;
  font-size: .76rem;
  font-weight: 700;
  padding: 5px 8px;
}
.review-grid { display: grid; gap: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.review-grid-three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.review-field { color: var(--ink); display: grid; font-weight: 700; gap: 5px; min-width: 0; }
.review-field input, .review-field select {
  background: white;
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--ink);
  font: inherit;
  min-height: 42px;
  padding: 8px 10px;
  width: 100%;
}
.language-review-list { display: grid; gap: 9px; }
.language-review-row { display: grid; gap: 10px; grid-template-columns: 1.1fr .9fr 1fr; }
.review-checks { display: flex; flex-wrap: wrap; gap: 10px 20px; margin-top: 14px; }
.review-checkbox { align-items: start; display: inline-flex; font-weight: 600; gap: 7px; }
.review-checkbox input { flex: 0 0 auto; height: 18px; margin-top: 3px; width: 18px; }
.review-more { border-top: 1px solid var(--line); margin-top: 16px; padding-top: 12px; }
.review-more summary { cursor: pointer; font-weight: 700; margin-bottom: 12px; }
.sensitive-review { background: #FCFDFC; }
.review-actions {
  align-items: center;
  background: var(--bg);
  bottom: 0;
  display: flex;
  gap: 10px;
  justify-content: flex-end;
  padding: 12px 0;
  position: sticky;
  z-index: 10;
}
.muted, .empty { color: var(--muted); }
.actions { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px 18px 18px 38px; }
.actions li { margin: 6px 0; }
.secondary-actions { background: #fbfaf7; }
.explore-details {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 16px 18px;
}
.explore-details summary {
  cursor: pointer;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  justify-content: space-between;
  list-style-position: inside;
}
.explore-details summary span {
  font-size: 1.35rem;
  font-weight: 700;
}
.explore-details summary small {
  color: var(--muted);
  font-size: .92rem;
}
.explore-copy { margin-top: 12px; }
.explore-group {
  border-top: 1px solid var(--line);
  margin-top: 14px;
  padding-top: 14px;
}
.explore-card {
  background: #fffefa;
}
.compact-actions {
  max-width: 240px;
}
.jump-link, .back-link {
  color: var(--accent);
  font-weight: 700;
  text-decoration: none;
}
.jump-link:hover, .back-link:hover { text-decoration: underline; }
.jump-link { margin-left: 6px; white-space: nowrap; }
.back-link { font-size: .88rem; }
.notice.success { border-color: #b9d8c5; color: var(--ok); background: #eef8f1; }
.notice.error { border-color: #e0b8a8; color: var(--warn); background: #fff2ec; }
.action-feedback { font-weight: 700; margin: 10px 0 0; }
.action-feedback.success { color: var(--ok); }
.action-feedback.error { color: var(--warn); }
button:focus-visible, a:focus-visible, textarea:focus-visible, select:focus-visible, summary:focus-visible {
  outline: 3px solid var(--focus);
  outline-offset: 2px;
}
table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
th, td { text-align: left; border-bottom: 1px solid var(--line); padding: 10px; vertical-align: top; }
th { color: var(--muted); font-size: .86rem; }
tr:last-child td { border-bottom: 0; }
@media (max-width: 820px) {
  .review-grid, .review-grid-three, .language-review-row { grid-template-columns: 1fr; }
  .review-section-heading { display: block; }
  .inference-label { display: inline-flex; margin-top: 8px; }
  .review-actions { justify-content: stretch; }
  .review-actions > * { flex: 1; }
  .app-header-inner { height: 56px; padding: 0 16px; }
  .app-main { padding: 26px 16px 48px; }
  section, .card { scroll-margin-top: 72px; }
  h1 { font-size: 30px; }
  .page-intro { margin-top: 8px; }
  .hero, .card, .preview-grid, .card.ranked-card { grid-template-columns: 1fr; }
  .card.ranked-card { gap: 12px; padding: 16px; }
  .match-rank { height: 32px; width: 32px; }
  .profile-context { align-items: flex-start; flex-direction: column; }
  .card-actions { justify-content: flex-start; max-width: none; }
  .ranked-card .card-actions {
    display: grid;
    gap: 8px;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    max-width: none;
    width: 100%;
  }
  .ranked-card .js-card-controls { display: contents; }
  button, .open, .profile-switcher select { min-height: 44px; }
  .my-jobs-header { margin-top: 4px; }
  .my-jobs-filter-row { align-items: stretch; flex-direction: column; gap: 6px; }
  .tracker-filters { margin: 0 -3px; }
  .tracker-filter, .show-hidden { min-height: 44px; }
  .show-hidden { align-self: flex-start; }
  .card.my-job-card { gap: 12px; grid-template-columns: 1fr; }
  .my-job-actions {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    max-width: none;
    width: 100%;
  }
  .my-job-actions .js-card-controls { display: contents; }
  .my-job-actions .action-form, .my-job-actions .action-button, .my-job-actions .open { min-width: 0; }
  .preview-form { padding: 16px; }
  .product-nav { gap: 18px; }
}
"""


if __name__ == "__main__":
    main()
