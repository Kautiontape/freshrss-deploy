"""Scheduled briefs: periodic summaries over a set of feeds, stored and optionally emailed."""

from __future__ import annotations

import logging
import smtplib
import threading
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Callable

import anthropic
from croniter import croniter
from psycopg.types.json import Jsonb

from . import db, freshrss, llm, prompts, scoring
from .config import settings
from .content import truncate

log = logging.getLogger(__name__)

MAX_ENTRIES = 150
MAX_CHARS_PER_ENTRY = 60_000
MAX_TOTAL_CHARS = 900_000
MAX_WINDOW_DAYS = 14


# ── CRUD ────────────────────────────────────────────────────────────────────

BRIEF_FIELDS = ("name", "enabled", "schedule", "feed_ids", "category_ids", "lookback_hours", "unread_only",
                "min_score", "instructions", "model", "effort", "send_email")


def list_briefs() -> list[dict]:
    return [dict(r) for r in db.fetch_all("SELECT * FROM ai.briefs ORDER BY id")]


def get_brief(brief_id: int) -> dict | None:
    r = db.fetch_one("SELECT * FROM ai.briefs WHERE id = %s", (int(brief_id),))
    return dict(r) if r else None


def _clean(data: dict) -> dict:
    out: dict[str, Any] = {}
    if "name" in data:
        out["name"] = str(data["name"]).strip()[:120] or "Brief"
    if "enabled" in data:
        out["enabled"] = bool(data["enabled"])
    if "schedule" in data:
        s = str(data["schedule"]).strip()
        if not croniter.is_valid(s):
            raise ValueError(f"invalid cron schedule: {s}")
        out["schedule"] = s
    for k in ("feed_ids", "category_ids"):
        if k in data:
            out[k] = [int(x) for x in (data[k] or [])]
    if "lookback_hours" in data:
        out["lookback_hours"] = max(1, min(int(data["lookback_hours"] or 24), 24 * MAX_WINDOW_DAYS))
    if "unread_only" in data:
        out["unread_only"] = bool(data["unread_only"])
    if "min_score" in data:
        out["min_score"] = max(0, min(int(data["min_score"] or 0), 10))
    if "instructions" in data:
        out["instructions"] = str(data["instructions"] or "")
    if "model" in data:
        out["model"] = llm.valid_model(data["model"], "") if data["model"] else ""
    if "effort" in data:
        out["effort"] = llm.valid_effort(data["effort"], "high")
    if "send_email" in data:
        out["send_email"] = bool(data["send_email"])
    return out


def create_brief(data: dict) -> dict:
    vals = _clean(data)
    vals.setdefault("name", "Brief")
    cols = list(vals.keys())
    row = db.fetch_one(
        f"INSERT INTO ai.briefs ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
        tuple(vals[c] for c in cols),
    )
    return dict(row)


def update_brief(brief_id: int, data: dict) -> dict | None:
    vals = _clean(data)
    if vals:
        sets = ", ".join(f"{k} = %s" for k in vals)
        db.execute(f"UPDATE ai.briefs SET {sets} WHERE id = %s", (*vals.values(), int(brief_id)))
    return get_brief(brief_id)


def delete_brief(brief_id: int) -> None:
    db.execute("DELETE FROM ai.briefs WHERE id = %s", (int(brief_id),))


def list_runs(brief_id: int | None = None, limit: int = 30) -> list[dict]:
    if brief_id:
        rows = db.fetch_all(
            """SELECT r.id, r.brief_id, b.name AS brief_name, r.started_at, r.finished_at, r.status, r.period_start, r.period_end,
                      cardinality(r.entry_ids) AS n_entries, r.error, r.usage
               FROM ai.brief_runs r JOIN ai.briefs b ON b.id = r.brief_id WHERE r.brief_id = %s ORDER BY r.id DESC LIMIT %s""",
            (int(brief_id), limit))
    else:
        rows = db.fetch_all(
            """SELECT r.id, r.brief_id, b.name AS brief_name, r.started_at, r.finished_at, r.status, r.period_start, r.period_end,
                      cardinality(r.entry_ids) AS n_entries, r.error, r.usage
               FROM ai.brief_runs r JOIN ai.briefs b ON b.id = r.brief_id ORDER BY r.id DESC LIMIT %s""", (limit,))
    return [dict(r) for r in rows]


def get_run(run_id: int) -> dict | None:
    r = db.fetch_one(
        """SELECT r.*, b.name AS brief_name FROM ai.brief_runs r JOIN ai.briefs b ON b.id = r.brief_id WHERE r.id = %s""",
        (int(run_id),))
    return dict(r) if r else None


def list_briefs_with_runs() -> list[dict]:
    out = []
    for b in list_briefs():
        runs = list_runs(b["id"], limit=3)
        out.append({"id": b["id"], "name": b["name"], "enabled": b["enabled"], "schedule": b["schedule"],
                    "feed_ids": b["feed_ids"], "category_ids": b["category_ids"], "last_run_at": b["last_run_at"],
                    "recent_runs": [{"run_id": r["id"], "status": r["status"], "period_start": r["period_start"],
                                     "period_end": r["period_end"], "n_entries": r["n_entries"]} for r in runs]})
    return out


# ── Generation ──────────────────────────────────────────────────────────────

def _collect_entries(brief: dict, start_ts: int, end_ts: int) -> list[freshrss.Entry]:
    entries = freshrss.search_entries(
        feed_ids=brief["feed_ids"] or None, category_ids=brief["category_ids"] or None,
        unread_only=bool(brief["unread_only"]), min_score=(brief["min_score"] or None),
        since_ts=start_ts, until_ts=end_ts, sort="date_desc", limit=MAX_ENTRIES, with_content=True,
    )
    # Drop filtered Shorts
    return [e for e in entries if e.attributes.get("ai_score") != 0]


def _items_text(entries: list[freshrss.Entry], rules: dict) -> str:
    parts = []
    total = 0
    for i, e in enumerate(entries, 1):
        budget = min(MAX_CHARS_PER_ENTRY, max(2000, MAX_TOTAL_CHARS - total))
        text = scoring.entry_text(e, budget, rules=rules) if total < MAX_TOTAL_CHARS else "(omitted: brief size limit)"
        total += len(text)
        a = e.attributes
        meta = [f"id: {e.id}", f"source: {e.feed_name}", f"published: {scoring._fmt_date(e.date)}", f"url: {e.link}",
                f"type: {scoring.entry_kind(e)}"]
        if a.get("ai_score") is not None:
            meta.append(f"relevance_score: {a['ai_score']}/10")
        if a.get("ai_summary"):
            meta.append(f"existing_summary: {a['ai_summary']}")
        parts.append(f"<item n=\"{i}\" title=\"{e.title.replace(chr(34), '&quot;')}\">\n" + "\n".join(meta) +
                     f"\n\n{text}\n</item>")
    return "\n\n".join(parts)


def run_brief(brief_id: int, *, force_window_hours: int | None = None, stream_cb: Callable[[str], None] | None = None) -> dict:
    brief = get_brief(brief_id)
    if not brief:
        raise ValueError("brief not found")
    cfg = db.get_all_settings()
    model = llm.valid_model(brief["model"] or cfg["brief_model"], "claude-opus-5")
    effort = llm.valid_effort(brief["effort"] or cfg["brief_effort"], "high")

    now = datetime.now(timezone.utc)
    end_ts = int(now.timestamp())
    if force_window_hours:
        start = now - timedelta(hours=force_window_hours)
    else:
        last = db.fetch_one(
            "SELECT period_end FROM ai.brief_runs WHERE brief_id = %s AND status = 'ok' ORDER BY id DESC LIMIT 1", (brief_id,))
        if last and last["period_end"]:
            start = max(last["period_end"], now - timedelta(days=MAX_WINDOW_DAYS))
        else:
            start = now - timedelta(hours=int(brief["lookback_hours"] or 24))
    start_ts = int(start.timestamp())

    run = db.fetch_one(
        "INSERT INTO ai.brief_runs (brief_id, period_start, period_end) VALUES (%s, %s, %s) RETURNING id",
        (brief_id, start, now))
    run_id = int(run["id"])
    try:
        rules = freshrss.get_rules()
        entries = _collect_entries(brief, start_ts, end_ts)
        if not entries:
            db.execute("UPDATE ai.brief_runs SET status = 'empty', finished_at = now(), content_md = %s WHERE id = %s",
                       ("_No new items in this period._", run_id))
            db.execute("UPDATE ai.briefs SET last_run_at = now() WHERE id = %s", (brief_id,))
            return get_run(run_id) or {}

        period = f"{start.astimezone().strftime('%b %-d %H:%M')} to {now.astimezone().strftime('%b %-d %H:%M %Z')}"
        system = [{"type": "text", "text": prompts.BRIEF_SYSTEM.format(
            profile=(db.get_setting("interest_profile") or "").strip(), instructions=brief["instructions"] or "(none)"),
            "cache_control": {"type": "ephemeral"}}]
        user = prompts.BRIEF_USER.format(name=brief["name"], period=period, n=len(entries), items=_items_text(entries, rules))
        with llm.api(model).stream(
            model=model, max_tokens=16000, system=system, messages=[{"role": "user", "content": user}],
            **llm.thinking_params(model, effort), **llm.fallback_params(model),
        ) as stream:
            for text in stream.text_stream:
                if stream_cb:
                    stream_cb(text)
            msg = stream.get_final_message()
        cost = llm.log_usage("brief", model, msg.usage, ref=str(run_id))
        if msg.stop_reason == "refusal":
            raise RuntimeError("brief generation was refused")
        content = llm.text_of(msg).strip()
        usage = {"model": model, "input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens,
                 "cost_usd": round(cost, 4)}
        db.execute(
            "UPDATE ai.brief_runs SET status = 'ok', finished_at = now(), entry_ids = %s, content_md = %s, usage = %s WHERE id = %s",
            ([e.id for e in entries], content, Jsonb(usage), run_id))
        db.execute("UPDATE ai.briefs SET last_run_at = now() WHERE id = %s", (brief_id,))
        if brief["send_email"]:
            try:
                send_brief_email(brief, run_id, content, period)
            except Exception as e:
                log.warning("brief email failed: %s", e)
                db.execute("UPDATE ai.brief_runs SET error = %s WHERE id = %s", (f"email failed: {e}", run_id))
    except (anthropic.APIError, RuntimeError, ValueError) as e:
        log.warning("brief %s failed: %s", brief_id, e)
        db.execute("UPDATE ai.brief_runs SET status = 'error', finished_at = now(), error = %s WHERE id = %s", (str(e), run_id))
        db.execute("UPDATE ai.briefs SET last_run_at = now() WHERE id = %s", (brief_id,))
    return get_run(run_id) or {}


# ── Email ───────────────────────────────────────────────────────────────────

def markdown_to_html(md_text: str) -> str:
    import markdown
    body = markdown.markdown(md_text, extensions=["extra", "sane_lists"])
    return (
        "<html><body style=\"font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; line-height: 1.5; "
        "max-width: 720px; margin: 0 auto; padding: 16px; color: inherit;\">"
        f"{body}</body></html>"
    )


def send_brief_email(brief: dict, run_id: int, content_md: str, period: str) -> None:
    if not (settings.smtp_user and settings.smtp_password and settings.email_to):
        raise RuntimeError("SMTP settings (DIGEST_SMTP_USER / DIGEST_SMTP_PASSWORD / DIGEST_TO_EMAIL) are not configured")
    subject = f"{brief['name']} — {datetime.now().astimezone().strftime('%a %b %-d')}"
    footer = f"\n\n---\n[Open in the assistant]({settings.freshrss_public_url.rsplit(':', 1)[0]}:8081/#/briefs/{run_id})"
    html = markdown_to_html(content_md + footer)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_user
    msg["To"] = settings.email_to
    msg.attach(MIMEText(content_md, "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        server.starttls()
        server.login(settings.smtp_user, settings.smtp_password)
        server.sendmail(settings.smtp_user, [settings.email_to], msg.as_string())
    log.info("brief email sent: %s", subject)


# ── Scheduler ───────────────────────────────────────────────────────────────

class Scheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.running: set[int] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="brief-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_running(self, brief_id: int) -> bool:
        with self._lock:
            return brief_id in self.running

    def run_async(self, brief_id: int, **kwargs: Any) -> bool:
        with self._lock:
            if brief_id in self.running:
                return False
            self.running.add(brief_id)

        def go() -> None:
            try:
                run_brief(brief_id, **kwargs)
            except Exception:
                log.exception("brief run failed")
            finally:
                with self._lock:
                    self.running.discard(brief_id)

        threading.Thread(target=go, name=f"brief-{brief_id}", daemon=True).start()
        return True

    def _loop(self) -> None:
        self._stop.wait(20)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            self._stop.wait(60)

    def tick(self) -> None:
        if not settings.anthropic_api_key:
            return
        now = datetime.now().astimezone()
        for b in list_briefs():
            if not b["enabled"] or not croniter.is_valid(b["schedule"]):
                continue
            base = b["last_run_at"] or b["created_at"]
            base = base.astimezone() if base.tzinfo else base.replace(tzinfo=timezone.utc).astimezone()
            nxt = croniter(b["schedule"], base).get_next(datetime)
            if nxt <= now:
                log.info("brief due: %s (next was %s)", b["name"], nxt)
                self.run_async(b["id"])


scheduler = Scheduler()
