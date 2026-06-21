import argparse
import html
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
    "save": "saved",
    "applied": "applied",
    "assessment_started": "assessment_started",
    "assessment_completed": "assessment_completed",
    "remind_later": "remind_later",
    "not_interested": "not_interested",
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
            try:
                message = handle_action(form, profile_id)
                self.redirect("/", profile=profile_id, message=message)
            except SystemExit as exc:
                self.redirect("/", profile=profile_id, error=str(exc))
            except Exception as exc:
                self.redirect("/", profile=profile_id, error=f"Action failed: {exc}")

        def redirect(self, path, **params):
            query = urlencode({key: value for key, value in params.items() if value})
            location = path + (f"?{query}" if query else "")
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

        if action == "save":
            return f"Saved {title}"

        if status == "remind_later":
            reminder_date = (datetime.now(timezone.utc).date() + timedelta(days=7)).isoformat()
            update_pipeline_item(
                conn,
                item,
                status=status,
                note=note,
                reminder_date=reminder_date,
            )
            return f"Reminder set for {title}"

        update_pipeline_item(conn, item, status=status, note=note)

        if status in {"applied", "assessment_started", "assessment_completed"}:
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

    return f"Updated {title}"


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


def render_dashboard(context, message=None, error=None):
    profile = context["profile"]
    pipeline_report = context["pipeline_report"]
    applicant_signals = context["applicant_signals"]
    matches = context["matches"]
    tracked = context["tracked"]
    actions = context["next_actions"]

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
        render_header(context),
        render_notice(message, error),
        render_actions(actions),
        render_matches(
            "Today's Best Matches",
            matches["live"],
            tracked,
            profile["profile_id"],
            include_actions=True,
            empty="No strong live matches found right now.",
        ),
        render_pipeline(pipeline_report["records"], profile["profile_id"]),
        render_matches(
            "New Matches This Week",
            matches["new"],
            tracked,
            profile["profile_id"],
            include_actions=True,
            empty="No especially relevant new matches this week.",
        ),
        render_matches(
            "Always-Open Applications",
            matches["evergreen"],
            tracked,
            profile["profile_id"],
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


def render_header(context):
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
      </div>
      <div class="profile-box">
        {rows}
        <p><strong>Live opportunities tracked:</strong> {market['estimated_market_opportunities']}</p>
      </div>
    </section>
    """


def render_notice(message, error):
    if error:
        return f"<div class='notice error'>{e(error)}</div>"
    if message:
        return f"<div class='notice success'>{e(message)}</div>"
    return ""


def render_actions(actions):
    items = "".join(
        f"<li>{e(demo.make_action_user_facing(action['action']))}</li>"
        for action in actions[:5]
    )
    if not items:
        items = "<li>No urgent actions today.</li>"
    return f"""
    <section>
      <h2>Do These First</h2>
      <ol class="actions">{items}</ol>
    </section>
    """


def render_matches(title, matches, tracked, profile_id, include_actions, empty):
    cards = []
    for match in matches[:8]:
        record = demo.tracked_record_for_match(match, tracked)
        reasons = "; ".join(demo.plain_reasons(match, record)[:3])
        status = demo.pipeline_label(record)
        cards.append(
            f"""
            <article class="card">
              <div class="card-main">
                <p class="source">{e(match['source'])}</p>
                <h3>{e(match['display_title'])}</h3>
                <p>{e(match['location'])} · {e(match['expertise'])}</p>
                <p class="muted">{e(reasons)}</p>
                <p class="pill">{e(status)}</p>
              </div>
              <div class="card-actions">
                <a class="open" href="{e(match['url'])}" target="_blank" rel="noreferrer">Open</a>
                {render_match_forms(match, record, profile_id) if include_actions else ""}
              </div>
            </article>
            """
        )
    if not cards:
        cards.append(f"<p class='empty'>{e(empty)}</p>")
    return f"""
    <section>
      <h2>{e(title)}</h2>
      <div class="stack">{''.join(cards)}</div>
    </section>
    """


def render_pipeline(records, profile_id):
    if not records:
        body = "<p class='empty'>No applications tracked yet.</p>"
    else:
        body = "".join(
            f"""
            <article class="card tracker">
              <div class="card-main">
                <p class="source">{e(record['source'])}</p>
                <h3>{e(record['title'])}</h3>
                <p class="pill">{e(demo.readable_status(record['status']))}</p>
                <p class="muted">{e(record['next_action'])}</p>
              </div>
              <div class="card-actions">
                {f'<a class="open" href="{e(record["url"])}" target="_blank" rel="noreferrer">Open</a>' if record["url"] else ""}
                {render_pipeline_forms(record, profile_id)}
              </div>
            </article>
            """
            for record in sorted(records, key=demo.pipeline_sort_key)
        )
    return f"""
    <section>
      <h2>Your Application Tracker</h2>
      <div class="stack">{body}</div>
    </section>
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
    <section>
      <h2>Applicant Signals</h2>
      <p class="muted">Directional sample signals from similar tracked activity. They are not guarantees.</p>
      <p><strong>{summary['total_updates']}</strong> relevant reports · <strong>{summary['assessment_updates']}</strong> assessment-related reports</p>
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


def render_match_forms(match, record, profile_id):
    pipeline_id = record.get("id") if record else ""
    return " ".join(
        action_form(
            label,
            action,
            profile_id,
            source=match["source"],
            title=match["display_title"],
            url=match["url"],
            pipeline_id=pipeline_id,
        )
        for action, label in (
            ("save", "Save"),
            ("applied", "Mark applied"),
            ("assessment_started", "Assessment started"),
            ("not_interested", "Not interested"),
        )
    )


def render_pipeline_forms(record, profile_id):
    return " ".join(
        action_form(
            label,
            action,
            profile_id,
            source=record["source"],
            title=record["title"],
            url=record["url"],
            pipeline_id=record.get("id", ""),
        )
        for action, label in (
            ("applied", "Applied"),
            ("assessment_started", "Started"),
            ("assessment_completed", "Completed"),
            ("remind_later", "Remind"),
            ("not_interested", "Hide"),
        )
    )


def action_form(label, action, profile_id, source, title, url, pipeline_id=""):
    return f"""
    <form method="post" action="/action">
      <input type="hidden" name="profile" value="{e(profile_id)}">
      <input type="hidden" name="action" value="{e(action)}">
      <input type="hidden" name="source" value="{e(source)}">
      <input type="hidden" name="title" value="{e(title)}">
      <input type="hidden" name="url" value="{e(url or '')}">
      <input type="hidden" name="pipeline_item_id" value="{e(str(pipeline_id or ''))}">
      <button type="submit">{e(label)}</button>
    </form>
    """


def action_note(action):
    labels = {
        "save": "Saved from local UI",
        "applied": "Marked applied from local UI",
        "assessment_started": "Marked assessment started from local UI",
        "assessment_completed": "Marked assessment completed from local UI",
        "remind_later": "Reminder set from local UI",
        "not_interested": "Marked not interested from local UI",
    }
    return labels[action]


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
.stack { display: grid; gap: 10px; }
.card {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 14px;
  align-items: start;
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
.muted, .empty { color: var(--muted); }
.actions { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px 18px 18px 38px; }
.actions li { margin: 6px 0; }
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
