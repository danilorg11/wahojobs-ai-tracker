import argparse
import hashlib
import html
import re
import sys
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import product_demo_report as demo
import product_state
from wahojobs.db.connection import get_connection


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PROFILE_ID = "portuguese_english_reviewer"

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
    "applied": "Mark applied",
    "assessment_started": "Assessment started",
    "assessment_completed": "Assessment completed",
    "remind_later": "Remind in 7 days",
    "not_interested": "Not interested",
    "accepted": "Accepted",
    "rejected": "Rejected",
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


def main():
    args = parse_args()
    product_state.initialize_product_state_schema()
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(args.default_profile),
    )
    url = f"http://{args.host}:{args.port}/"
    print("")
    print("Wahojobs Local Product UI")
    print("=========================")
    print(f"Open: {url}")
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
    parser.add_argument("--default-profile", default=DEFAULT_PROFILE_ID)
    return parser.parse_args()


def make_handler(default_profile):
    class ProductAppHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self.write_text("ok\n")
                return
            if parsed.path != "/":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            params = parse_qs(parsed.query)
            profile_id = first_value(params, "profile") or default_profile
            message = first_value(params, "message")
            error = first_value(params, "error")
            try:
                context = demo.build_demo_context(
                    profile_id=profile_id,
                    use_product_state=True,
                )
                body = render_dashboard(context, message=message, error=error)
                self.write_html(body)
            except SystemExit as exc:
                self.write_html(render_error(str(exc)), status=HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self.write_html(render_error(str(exc)), status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path != "/action":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            profile_id = first_value(form, "profile") or default_profile
            return_to = first_value(form, "return_to") or "do-these-first"
            try:
                message = handle_action(form, profile_id)
                self.redirect("/", fragment=return_to, profile=profile_id, message=message)
            except SystemExit as exc:
                self.redirect("/", fragment=return_to, profile=profile_id, error=str(exc))
            except Exception as exc:
                self.redirect("/", fragment=return_to, profile=profile_id, error=f"Action failed: {exc}")

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

        def log_message(self, format, *args):
            return

    return ProductAppHandler


def handle_action(form, profile_id):
    action = first_value(form, "action")
    if action not in ACTION_STATUSES:
        raise SystemExit(f"Unknown action: {action}")

    source = required_form_value(form, "source")
    title = required_form_value(form, "title")
    url = first_value(form, "url")
    pipeline_id = first_value(form, "pipeline_item_id")
    note = action_note(action)
    status = ACTION_STATUSES[action]

    with get_connection() as conn:
        if pipeline_id:
            item = product_state.require_pipeline_item(conn, pipeline_id)
        else:
            item = ensure_pipeline_item(conn, profile_id, source, title, url)

        allowed_actions = actions_for_status(item["status"])
        if action not in allowed_actions and action != "save":
            raise SystemExit(
                f"That action is not available while this item is {demo.readable_status(item['status'])}."
            )

        if action == "save":
            return action_success_message(action, title)

        if action == "show_again":
            update_pipeline_item(conn, item, status=status, note=note)
            return action_success_message(action, title)

        if status == "remind_later":
            reminder_date = (datetime.now(timezone.utc).date() + timedelta(days=7)).isoformat()
            update_pipeline_item(
                conn,
                item,
                status=status,
                note=note,
                reminder_date=reminder_date,
            )
            return f"Reminder set for {reminder_date}: {title}"

        update_pipeline_item(conn, item, status=status, note=note)

        if status in {
            "applied",
            "assessment_started",
            "assessment_completed",
            "accepted",
            "rejected",
        }:
            add_applicant_update(
                conn,
                profile_id=profile_id,
                source=source,
                title=title,
                url=url,
                status=status,
                previous_status=item["status"],
                note=note,
            )

    return action_success_message(action, title)


def ensure_pipeline_item(conn, profile_id, source, title, url):
    profile = product_state.require_profile(conn, profile_id)
    url = url or ""
    existing = product_state.find_pipeline_item_by_identity(
        conn,
        profile_id,
        source,
        title,
        url,
    )
    if existing is not None:
        return existing

    record = {
        "profile_id": profile_id,
        "source": source,
        "title": title,
        "url": url,
    }
    pipeline_item_id = product_state.stable_pipeline_item_id(record)
    conn.execute(
        """
        INSERT INTO user_pipeline_items (
          pipeline_item_id,
          user_id,
          profile_id,
          source,
          opportunity_title,
          opportunity_url,
          opportunity_external_id,
          canonical_id,
          status,
          status_date,
          user_priority,
          reminder_date,
          notes,
          last_user_action,
          is_sample
        )
        VALUES (?, ?, ?, ?, ?, ?, '', NULL, 'saved', ?, 'medium', '', '', 'Saved from local UI', 0)
        """,
        (
            pipeline_item_id,
            profile["user_id"],
            profile_id,
            source,
            title,
            url,
            product_state.today(),
        ),
    )
    return conn.execute(
        "SELECT * FROM user_pipeline_items WHERE pipeline_item_id = ?",
        (pipeline_item_id,),
    ).fetchone()


def update_pipeline_item(conn, item, status, note, reminder_date=None):
    notes = product_state.merge_note(item["notes"], note)
    status_date = product_state.today()
    if reminder_date is None:
        conn.execute(
            """
            UPDATE user_pipeline_items
            SET status = ?,
                status_date = ?,
                notes = ?,
                last_user_action = ?,
                is_sample = 0,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, status_date, notes, note, item["id"]),
        )
    else:
        conn.execute(
            """
            UPDATE user_pipeline_items
            SET status = ?,
                status_date = ?,
                reminder_date = ?,
                notes = ?,
                last_user_action = ?,
                is_sample = 0,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, status_date, reminder_date, notes, note, item["id"]),
        )


def add_applicant_update(conn, profile_id, source, title, url, status, previous_status, note):
    profile = product_state.require_profile(conn, profile_id)
    status_date = product_state.today()
    update_id = product_state.stable_applicant_update_id(
        profile_id,
        source,
        title,
        status,
        status_date,
        "self_reported",
        "medium",
        note,
    )
    conn.execute(
        """
        INSERT INTO applicant_status_updates (
          update_id,
          user_id,
          anonymous_user_key,
          profile_id,
          source,
          opportunity_title,
          opportunity_url,
          opportunity_external_id,
          canonical_id,
          status,
          previous_status,
          status_date,
          reported_at,
          evidence_type,
          confidence_level,
          notes,
          is_sample
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, '', NULL, ?, ?, ?, ?, 'self_reported', 'medium', ?, 0)
        ON CONFLICT(update_id) DO UPDATE SET
          previous_status = excluded.previous_status,
          reported_at = excluded.reported_at,
          notes = excluded.notes,
          updated_at = CURRENT_TIMESTAMP
        """,
        (
            update_id,
            profile["user_id"],
            profile["user_id"],
            profile_id,
            source,
            title,
            url or "",
            status,
            previous_status or "",
            status_date,
            product_state.now_utc(),
            note,
        ),
    )


def build_card_index(matches, pipeline_records, tracked):
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


def render_dashboard(context, message=None, error=None):
    profile = context["profile"]
    pipeline_report = context["pipeline_report"]
    applicant_signals = context["applicant_signals"]
    matches = context["matches"]
    tracked = context["tracked"]
    profiles = load_profile_options()
    visible_match_buckets = {
        key: visible_matches(bucket, tracked)
        for key, bucket in matches.items()
    }
    actions = visible_actions(context["next_actions"], tracked)
    secondary_actions = visible_actions(context.get("also_worth_reviewing", []), tracked)
    card_index = build_card_index(visible_match_buckets, pipeline_report["records"], tracked)

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
        "<main>",
        render_header(context, profiles),
        render_notice(message, error),
        render_actions(actions, card_index),
        render_secondary_actions(secondary_actions, card_index),
        render_matches(
            "Today's Best Matches",
            "best-matches",
            visible_match_buckets["live"],
            tracked,
            profile["profile_id"],
            card_index,
            include_actions=True,
            empty="No strong live matches found right now.",
        ),
        render_pipeline(pipeline_report["records"], profile["profile_id"]),
        render_matches(
            "New Matches This Week",
            "new-matches",
            visible_match_buckets["new"],
            tracked,
            profile["profile_id"],
            card_index,
            include_actions=True,
            empty="No especially relevant new matches this week.",
        ),
        render_matches(
            "Always-Open Applications",
            "always-open",
            visible_match_buckets["evergreen"],
            tracked,
            profile["profile_id"],
            card_index,
            include_actions=True,
            empty="No profile-relevant always-open applications surfaced today.",
        ),
        render_applicant_signals(applicant_signals),
        render_disclaimer(),
        "</main>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts)


def render_header(context, profiles):
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
    return f"""
    <section class="hero">
      <div>
        <p class="eyebrow">Local prototype</p>
        <h1>Your AI-work opportunity dashboard</h1>
        <p class="lead">A focused view of the best leads, active applications, and directional applicant signals for one profile.</p>
        {render_profile_selector(profiles, profile["profile_id"])}
      </div>
      <div class="profile-box">
        {rows}
        <p><strong>Live opportunities tracked:</strong> {market['estimated_market_opportunities']}</p>
      </div>
    </section>
    """


def render_profile_selector(profiles, selected_profile_id):
    if not profiles:
        return """
        <p class="muted">No product-state profiles found. Import profiles before switching users.</p>
        """
    options = "".join(
        f"<option value=\"{e(row['profile_id'])}\" {'selected' if row['profile_id'] == selected_profile_id else ''}>"
        f"{e(row['display_name'])}</option>"
        for row in profiles
    )
    return f"""
    <form class="profile-switcher" method="get" action="/">
      <label for="profile">View profile</label>
      <select id="profile" name="profile">
        {options}
      </select>
      <button type="submit">Switch</button>
    </form>
    """


def render_notice(message, error):
    if error:
        return f"<div class='notice error'>{e(error)}</div>"
    if message:
        return f"<div class='notice success'>{e(message)}</div>"
    return ""


def render_actions(actions, card_index):
    items = "".join(render_action_item(action, card_index) for action in actions[:4])
    if not items:
        items = "<li>No urgent new applications today. We'll keep watching for strong matches.</li>"
    return f"""
    <section id="do-these-first">
      <h2>Do These First</h2>
      <p class="muted">A short daily plan. You do not need to act on everything today.</p>
      <ol class="actions">{items}</ol>
    </section>
    """


def render_secondary_actions(actions, card_index):
    items = "".join(render_action_item(action, card_index) for action in actions[:8])
    if not items:
        items = "<li>No additional backlog items surfaced for this profile today.</li>"
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


def render_matches(title, section_id, matches, tracked, profile_id, card_index, include_actions, empty):
    cards = []
    for match in matches[:8]:
        record = demo.tracked_record_for_match(match, tracked)
        reasons = "; ".join(demo.plain_reasons(match, record)[:3])
        status = demo.pipeline_label(record)
        card_id = card_id_for_match(match, record)
        cards.append(
            f"""
            <article class="card" id="{e(card_id)}">
              <div class="card-main">
                <p class="source">{e(match['source'])}</p>
                <h3>{e(match['display_title'])}</h3>
                <p>{e(match['location'])} &middot; {e(match['expertise'])}</p>
                <p class="muted">{e(reasons)}</p>
                <p class="pill">{e(status)}</p>
                <p><a class="back-link" href="#do-these-first">Back to Do These First</a></p>
              </div>
              <div class="card-actions">
                <a class="open" href="{e(match['url'])}" target="_blank" rel="noreferrer">Open</a>
                {render_match_forms(match, record, profile_id, card_id) if include_actions else ""}
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


def render_pipeline(records, profile_id):
    groups = pipeline_groups(records)
    active_body = render_pipeline_group(
        groups["active"],
        profile_id,
        "No active application items yet.",
    )
    accepted_body = render_pipeline_group(
        groups["accepted"],
        profile_id,
        "No accepted or active work items yet.",
    )
    hidden_body = render_pipeline_group(
        groups["hidden"],
        profile_id,
        "No hidden opportunities.",
    )
    closed_body = render_pipeline_group(
        groups["closed"],
        profile_id,
        "No closed or expired items.",
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


def render_pipeline_group(records, profile_id, empty):
    if not records:
        return f"<p class='empty'>{e(empty)}</p>"
    return "".join(render_pipeline_card(record, profile_id) for record in records)


def render_reminder_note(record):
    if record["status"] == "remind_later" and record.get("reminder_date"):
        return f"<p class='muted'>Remind on {e(record['reminder_date'])}</p>"
    return ""


def render_pipeline_card(record, profile_id):
    return f"""
    <article class="card tracker" id="{e(card_id_for_record(record))}">
      <div class="card-main">
        <p class="source">{e(record['source'])}</p>
        <h3>{e(record['title'])}</h3>
        <p class="pill">{e(demo.readable_status(record['status']))}</p>
        {render_reminder_note(record)}
        <p class="muted">{e(record['next_action'])}</p>
        <p><a class="back-link" href="#do-these-first">Back to Do These First</a></p>
      </div>
      <div class="card-actions">
        {f'<a class="open" href="{e(record["url"])}" target="_blank" rel="noreferrer">Open</a>' if record["url"] else ""}
        {render_pipeline_forms(record, profile_id, card_id_for_record(record))}
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


def render_match_forms(match, record, profile_id, return_to):
    pipeline_id = record.get("id") if record else ""
    status = record.get("status") if record else None
    actions = actions_for_status(status)
    if not actions:
        return terminal_status_label(status)
    return " ".join(
        action_form(
            action,
            ACTION_LABELS[action],
            profile_id,
            source=match["source"],
            title=match["display_title"],
            url=match["url"],
            pipeline_id=pipeline_id,
            return_to=return_to,
        )
        for action in actions
    )


def render_pipeline_forms(record, profile_id, return_to):
    actions = actions_for_status(record["status"])
    if not actions:
        return terminal_status_label(record["status"])
    return " ".join(
        action_form(
            action,
            ACTION_LABELS[action],
            profile_id,
            source=record["source"],
            title=record["title"],
            url=record["url"],
            pipeline_id=record.get("id", ""),
            return_to=return_to,
        )
        for action in actions
    )


def action_form(action, label, profile_id, source, title, url, pipeline_id="", return_to="do-these-first"):
    return f"""
    <form method="post" action="/action">
      <input type="hidden" name="profile" value="{e(profile_id)}">
      <input type="hidden" name="action" value="{e(action)}">
      <input type="hidden" name="source" value="{e(source)}">
      <input type="hidden" name="title" value="{e(title)}">
      <input type="hidden" name="url" value="{e(url or '')}">
      <input type="hidden" name="pipeline_item_id" value="{e(str(pipeline_id or ''))}">
      <input type="hidden" name="return_to" value="{e(return_to)}">
      <button type="submit">{e(label)}</button>
    </form>
    """


def actions_for_status(status):
    return STATUS_ACTIONS.get(status, ("remind_later", "not_interested"))


def terminal_status_label(status):
    labels = {
        "not_interested": "Hidden / Not interested",
        "expired": "Expired",
        "accepted": "Accepted",
        "active_worker": "Active",
        "paid_task_received": "Paid task received",
        "rejected": "Rejected",
    }
    return f"<p class='status-note'>{e(labels.get(status, demo.readable_status(status)))}</p>"


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


def action_success_message(action, title):
    labels = {
        "show_again": "Shown again",
        "save": "Saved",
        "applied": "Marked as applied",
        "assessment_started": "Assessment started",
        "assessment_completed": "Assessment completed",
        "remind_later": "Remind later set for",
        "not_interested": "Marked not interested",
        "accepted": "Marked accepted",
        "rejected": "Marked rejected",
    }
    return f"{labels[action]}: {title}"


def render_error(message):
    return f"""
    <!doctype html>
    <html lang="en">
    <head><meta charset="utf-8"><title>Wahojobs Local UI</title><style>{CSS}</style></head>
    <body><main><section><h1>Wahojobs Local UI</h1><div class="notice error">{e(message)}</div></section></main></body>
    </html>
    """


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
  --bg: #f7f7f4;
  --ink: #1f2a24;
  --muted: #657168;
  --line: #d8ddd5;
  --panel: #ffffff;
  --accent: #27614f;
  --accent-soft: #e4f2ed;
  --warn: #7a3b24;
  --ok: #235d38;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 15px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
main { width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 48px; }
section { margin: 18px 0; }
section, .card { scroll-margin-top: 18px; }
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
h1 { font-size: clamp(2rem, 4vw, 3.4rem); line-height: 1; margin-bottom: 12px; }
h2 { font-size: 1.35rem; margin-bottom: 10px; }
h3 { font-size: 1.02rem; margin-bottom: 8px; }
.lead { color: var(--muted); font-size: 1.08rem; max-width: 56ch; }
.eyebrow, .source { color: var(--accent); font-weight: 700; font-size: .78rem; letter-spacing: .04em; text-transform: uppercase; margin-bottom: 8px; }
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
  min-height: 34px;
  min-width: min(360px, 100%);
  padding: 6px 9px;
}
.stack { display: grid; gap: 10px; }
.card {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 14px;
  align-items: start;
}
.card:target {
  border-color: var(--accent);
  background: #f4fbf7;
  box-shadow: 0 0 0 3px rgba(39, 97, 79, .12);
}
.card-actions { display: flex; flex-wrap: wrap; gap: 7px; justify-content: flex-end; max-width: 360px; }
.card-actions form { margin: 0; }
button, .open {
  border: 1px solid var(--accent);
  background: var(--accent);
  color: white;
  border-radius: 6px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  min-height: 32px;
  padding: 6px 10px;
  text-decoration: none;
  font: inherit;
  white-space: nowrap;
}
button:hover, .open:hover { filter: brightness(.96); }
.open { background: var(--accent-soft); color: var(--accent); }
.pill {
  display: inline-block;
  background: var(--accent-soft);
  color: var(--accent);
  padding: 4px 8px;
  border-radius: 999px;
  font-size: .86rem;
  margin-bottom: 0;
}
.status-note {
  color: var(--muted);
  font-weight: 700;
  margin: 0;
  padding: 6px 0;
}
.muted, .empty { color: var(--muted); }
.actions { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px 18px 18px 38px; }
.actions li { margin: 6px 0; }
.secondary-actions { background: #fbfaf7; }
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
table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
th, td { text-align: left; border-bottom: 1px solid var(--line); padding: 10px; vertical-align: top; }
th { color: var(--muted); font-size: .86rem; }
tr:last-child td { border-bottom: 0; }
@media (max-width: 820px) {
  .hero, .card { grid-template-columns: 1fr; }
  .card-actions { justify-content: flex-start; max-width: none; }
}
"""


if __name__ == "__main__":
    main()
