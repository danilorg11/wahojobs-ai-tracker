import argparse
import hashlib
import html
import json
import re
import secrets
import sqlite3
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import product_demo_report as demo
import profile_to_matches_preview as profile_preview
import product_state
import user_pipeline_digest as pipeline_digest
from wahojobs import (
    pipeline_actions,
    pipeline_reconciliation,
    pipeline_records,
    pipeline_state,
)
from wahojobs.db.connection import get_connection
from wahojobs.profiles.canonical import (
    SCHEMA_VERSION,
    canonical_profile_fingerprint,
    validate_canonical_profile,
)
from wahojobs.profiles.normalizer import normalize_profile_input
from wahojobs.profiles.review import (
    CREDENTIAL_STATUSES,
    EDUCATION_LEVELS,
    LANGUAGE_PROFICIENCIES,
    apply_reviewed_profile,
)


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
TRACKER_PATHS = {"/", "/tracker"}
HEAVY_DASHBOARD_PATHS = {"/dashboard", "/market-dashboard"}

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
    canonical_profile: dict | None = None
    profile_confirmed: bool = False
    review_token: str = ""


class MatchRunRegistry:
    """Bounded process-local run storage for the local prototype."""

    def __init__(self, max_size=MATCH_RUN_REGISTRY_LIMIT):
        if max_size < 1:
            raise ValueError("MatchRun registry max_size must be positive.")
        self.max_size = max_size
        self._runs = OrderedDict()
        self._lock = threading.RLock()

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
        with self._lock:
            self._runs[run.match_run_id] = run
            self._runs.move_to_end(run.match_run_id)
            while len(self._runs) > self.max_size:
                self._runs.popitem(last=False)
        return run

    def confirm_profile(self, match_run_id, canonical_profile, recommendation_context):
        validate_canonical_profile(canonical_profile)
        with self._lock:
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

    def get(self, match_run_id):
        with self._lock:
            run = self._runs.get(match_run_id)
            if run is None:
                return None
            run = replace(run, last_accessed_at=datetime.now(timezone.utc))
            self._runs[match_run_id] = run
            self._runs.move_to_end(match_run_id)
            return run

    def __len__(self):
        with self._lock:
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


def make_handler(registry=None, demo_mode=False):
    registry = registry if registry is not None else MatchRunRegistry()

    class ProductAppHandler(BaseHTTPRequestHandler):
        match_run_registry = registry
        is_demo_mode = demo_mode

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self.write_text("ok\n")
                return
            if parsed.path in FIND_MATCHES_PATHS:
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

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path in FIND_MATCHES_PATHS:
                form = self.read_form(keep_blank_values=True)
                try:
                    run = create_match_run(form, registry, demo_mode)
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


def create_match_run(form, registry, demo_mode=False):
    if "form_action" in form:
        run, updates = validate_profile_review_submission(form, registry)
        if run.canonical_profile is None:
            raise ActionError("This profile review has expired. Start again.")
        try:
            canonical = apply_reviewed_profile(
                run.canonical_profile,
                updates,
            )
        except ValueError as exc:
            raise ActionError(str(exc)) from exc
        context = build_current_structured_preview_context(
            canonical,
            run.raw_input,
            run.input_style,
            PREVIEW_MATCH_LIMIT,
            preview_data_signature(),
        )
        confirmed = registry.confirm_profile(run.match_run_id, canonical, context)
        if confirmed is None:
            raise ActionError(
                "That match run is unknown or has expired. Start a new search.",
                HTTPStatus.GONE,
            )
        return confirmed

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
    require_owner_profile(owner_profile_id)
    normalization = normalize_profile_input(
        raw_input,
        input_style,
        metadata={
            "profile_id": "preview_profile",
            "display_name": "Preview Profile",
        },
    )
    return registry.create(
        owner_profile_id=owner_profile_id,
        raw_input=raw_input,
        input_style=input_style,
        demo_persona=demo_persona or None,
        recommendation_context=None,
        canonical_profile=normalization.canonical_profile,
        profile_confirmed=False,
    )


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
        "profile_draft_fingerprint": canonical_profile_fingerprint(canonical),
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
    run = require_match_run(registry, run_id)
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
    if not secrets.compare_digest(
        fingerprint, canonical_profile_fingerprint(run.canonical_profile)
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
    raw_input,
    input_style,
    limit,
    data_signature,
    *,
    evaluated_at=None,
):
    cached = build_cached_structured_preview_context(
        canonical_profile_fingerprint(canonical),
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


def render_structured_profile_review(canonical, match_run_id, review_token):
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
    back_url = "/find-matches?" + urlencode({"run": match_run_id, "edit_text": "1"})
    return f"""
    <form method="post" action="/find-matches" class="profile-review-form" id="profile-review-form">
      <input type="hidden" name="form_action" value="confirm_profile">
      <input type="hidden" name="edit_run_id" value="{e(match_run_id)}">
      <input type="hidden" name="review_token" value="{e(review_token)}">
      <input type="hidden" name="schema_version" value="{e(SCHEMA_VERSION)}">
      <input type="hidden" name="profile_draft_fingerprint" value="{e(canonical_profile_fingerprint(canonical))}">

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
        <button type="submit" id="confirm-profile-button">Find my matches</button>
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
