"""Scoring, summary, and enrichment worker.

Runs as a background thread: every `scoring_interval_s` it scores unscored
entries in scoring-enabled feeds (newest first), upgrades summaries for
high-value entries, and enriches recent YouTube entries (Shorts detection,
transcripts). Everything is idempotent; state lives in ai.entry_state and in
the entry attributes the FreshRSS extension renders.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Iterable

import anthropic
from pydantic import BaseModel, Field

from . import db, freshrss, llm, prompts
from .config import settings
from .content import fetch_full_article, html_to_text, truncate, youtube_info
from .freshrss import Entry

log = logging.getLogger(__name__)

BATCH_SIZE = 10
SCORE_EXCERPT_CHARS = 1800
VIDEO_EXCERPT_CHARS = 900
SUMMARY_CONTENT_CHARS = 40_000
MAX_ATTEMPTS = 3
RETRY_AFTER_S = 6 * 3600


class ScoreItem(BaseModel):
    id: str
    score: int = Field(ge=1, le=10)
    reason: str
    gist: str
    topics: list[str] = Field(default_factory=list)


class ScoreBatch(BaseModel):
    results: list[ScoreItem]


# ── Helpers ─────────────────────────────────────────────────────────────────

def _profile() -> str:
    return (db.get_setting("interest_profile") or "").strip() or "(no interest profile configured)"


def _fmt_date(ts: int) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
    except Exception:
        return ""


def entry_kind(entry: Entry) -> str:
    return "YouTube video" if entry.is_youtube else "article"


def entry_text(entry: Entry, max_chars: int, *, allow_fetch: bool = True, allow_transcript: bool = True,
               rules: dict | None = None) -> str:
    """Best available text for an entry, filling caches (transcript / full content) when allowed."""
    attrs = entry.attributes
    if entry.is_youtube:
        transcript = attrs.get("yt_transcript")
        if transcript is None and allow_transcript and not attrs.get("yt_is_short"):
            enrich_youtube(entry)
            transcript = entry.attributes.get("yt_transcript")
        if transcript:
            return truncate(transcript, max_chars)
        return truncate(html_to_text(entry.content), max_chars)

    text = html_to_text(entry.content)
    full = attrs.get("full_content")
    if full:
        return truncate(full if len(full) > len(text) else text, max_chars)
    if allow_fetch and full is None:
        rule = freshrss.rule_for(entry, rules)
        if rule["fetch_full"] and len(text) < 1500:
            fetched = fetch_full_article(entry.link)
            freshrss.merge_attributes(entry.id, {"full_content": fetched or ""})
            entry.attributes["full_content"] = fetched or ""
            if fetched and len(fetched) > len(text):
                return truncate(fetched, max_chars)
    return truncate(text, max_chars)


def enrich_youtube(entry: Entry) -> dict[str, Any] | None:
    """Fetch duration / Shorts flag / transcript from youtube-helper and cache on the entry."""
    vid = freshrss.youtube_video_id(entry.link)
    if not vid:
        return None
    info = youtube_info(vid)
    if not info:
        return None
    is_short = bool(info.get("is_short"))
    values: dict[str, Any] = {
        "yt_is_short": is_short,
        "yt_duration": info.get("duration"),
        "yt_transcript": (info.get("transcript") or "") if not is_short else "",
    }
    freshrss.merge_attributes(entry.id, values)
    entry.attributes.update(values)
    db.execute(
        """INSERT INTO ai.entry_state (entry_id, enriched_at) VALUES (%s, now())
           ON CONFLICT (entry_id) DO UPDATE SET enriched_at = now(), updated_at = now()""",
        (entry.id,),
    )
    return values


def _bump_attempt(entry_ids: Iterable[int], error: str) -> None:
    for eid in entry_ids:
        db.execute(
            """INSERT INTO ai.entry_state (entry_id, attempts, last_error) VALUES (%s, 1, %s)
               ON CONFLICT (entry_id) DO UPDATE SET attempts = ai.entry_state.attempts + 1, last_error = EXCLUDED.last_error, updated_at = now()""",
            (eid, error[:2000]),
        )


def _mark_scored(entry_id: int) -> None:
    db.execute(
        """INSERT INTO ai.entry_state (entry_id, scored_at, attempts, last_error) VALUES (%s, now(), 0, NULL)
           ON CONFLICT (entry_id) DO UPDATE SET scored_at = now(), attempts = 0, last_error = NULL, updated_at = now()""",
        (entry_id,),
    )


def _mark_summarized(entry_id: int) -> None:
    db.execute(
        """INSERT INTO ai.entry_state (entry_id, summarized_at) VALUES (%s, now())
           ON CONFLICT (entry_id) DO UPDATE SET summarized_at = now(), updated_at = now()""",
        (entry_id,),
    )


# ── Scoring ─────────────────────────────────────────────────────────────────

def _score_items_payload(entries: list[Entry]) -> list[dict[str, Any]]:
    items = []
    for e in entries:
        if e.is_youtube:
            transcript = e.attributes.get("yt_transcript")
            excerpt = truncate(transcript, VIDEO_EXCERPT_CHARS) if transcript else truncate(html_to_text(e.content), VIDEO_EXCERPT_CHARS)
            kind = "video"
        else:
            excerpt = truncate(entry_text(e, SCORE_EXCERPT_CHARS, allow_fetch=False, allow_transcript=False), SCORE_EXCERPT_CHARS)
            kind = "article"
        items.append({
            "id": str(e.id),
            "type": kind,
            "title": e.title,
            "source": f"{e.feed_name} ({e.category_name})" if e.category_name else e.feed_name,
            "published": _fmt_date(e.date),
            "excerpt": excerpt,
        })
    return items


def score_batch(entries: list[Entry], *, model: str | None = None, effort: str | None = None,
                purpose: str = "score") -> dict[int, dict[str, Any]]:
    """Score a batch of entries with one API call. Returns {entry_id: {score, reason, gist}}."""
    if not entries:
        return {}
    cfg = db.get_all_settings()
    model = llm.valid_model(model or cfg["scoring_model"], "claude-sonnet-5")
    effort = llm.valid_effort(effort or cfg["scoring_effort"], "low")
    items = _score_items_payload(entries)
    topics = [str(t).strip() for t in (cfg.get("topic_tags") or []) if str(t).strip()]
    system = [{"type": "text", "text": prompts.SCORING_SYSTEM.format(profile=_profile(), topics=", ".join(topics) or "(none)"),
               "cache_control": {"type": "ephemeral"}}]
    user = prompts.SCORING_USER.format(n=len(items), items=json.dumps(items, ensure_ascii=False, indent=1))

    resp = llm.client().messages.parse(
        model=model,
        max_tokens=8000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=ScoreBatch,
        **llm.thinking_params(model, effort),
    )
    llm.log_usage(purpose, model, resp.usage, ref=f"n={len(entries)}")
    if resp.stop_reason == "refusal":
        raise RuntimeError("scoring request was refused")
    parsed = resp.parsed_output
    if parsed is None:
        raise RuntimeError("scoring response could not be parsed")

    by_id = {str(e.id): e for e in entries}
    out: dict[int, dict[str, Any]] = {}
    for item in parsed.results:
        e = by_id.get(item.id)
        if not e:
            continue
        values = {"ai_score": int(item.score), "ai_score_reason": item.reason.strip()}
        if not e.attributes.get("ai_summary"):
            values["ai_summary"] = item.gist.strip()
        freshrss.merge_attributes(e.id, values)
        e.attributes.update(values)
        _mark_scored(e.id)
        _apply_labels_and_tags(e.id, int(item.score), [t for t in item.topics if t in topics], cfg)
        out[e.id] = {"score": int(item.score), "reason": item.reason.strip(), "gist": item.gist.strip(),
                     "summary": e.attributes.get("ai_summary", ""), "topics": item.topics}
    missing = [e.id for e in entries if e.id not in out]
    if missing:
        _bump_attempt(missing, "model omitted item from batch")
    return out


def _apply_labels_and_tags(entry_id: int, score: int, topics: list[str], cfg: dict[str, Any]) -> None:
    try:
        if cfg.get("write_labels", True):
            freshrss.apply_score_label(entry_id, score, int(cfg.get("label_high_min", 7)), int(cfg.get("label_medium_min", 4)))
        if cfg.get("write_topic_tags", True) and topics:
            freshrss.set_topic_tags(entry_id, topics)
    except Exception as e:  # labels are a nicety; never fail scoring over them
        log.warning("label/tag write failed for %s: %s", entry_id, e)


def _filter_shorts(entries: list[Entry], mark_read: bool) -> tuple[list[Entry], dict[int, dict[str, Any]]]:
    """Entries already known to be Shorts get score 0 without an API call."""
    keep, results = [], {}
    for e in entries:
        if e.is_youtube and (e.attributes.get("yt_is_short") or "youtube.com/shorts/" in e.link):
            values = {"ai_score": 0, "ai_score_reason": "YouTube Short (filtered)"}
            freshrss.merge_attributes(e.id, values)
            e.attributes.update(values)
            _mark_scored(e.id)
            _apply_labels_and_tags(e.id, 0, [], db.get_all_settings())
            if mark_read and not e.is_read:
                freshrss.mark_read([e.id], True)
            results[e.id] = {"score": 0, "reason": values["ai_score_reason"], "gist": "", "summary": ""}
        else:
            keep.append(e)
    return keep, results


def score_entries(entries: list[Entry], *, model: str | None = None, effort: str | None = None,
                  purpose: str = "score") -> dict[int, dict[str, Any]]:
    """Score arbitrary entries (any size), in batches, with a small thread pool."""
    cfg = db.get_all_settings()
    entries, results = _filter_shorts(entries, bool(cfg.get("mark_shorts_read", True)))
    batches = [entries[i:i + BATCH_SIZE] for i in range(0, len(entries), BATCH_SIZE)]

    def run(batch: list[Entry]) -> dict[int, dict[str, Any]]:
        try:
            return score_batch(batch, model=model, effort=effort, purpose=purpose)
        except (anthropic.APIError, RuntimeError, ValueError) as e:
            log.warning("scoring batch failed (%d entries): %s", len(batch), e)
            _bump_attempt([b.id for b in batch], str(e))
            return {}

    with ThreadPoolExecutor(max_workers=max(1, settings.scoring_concurrency)) as pool:
        for r in pool.map(run, batches):
            results.update(r)
    return results


def pending_entries(limit: int = 500, *, rules: dict | None = None) -> list[Entry]:
    """Unscored entries in scoring-enabled feeds: unread, or newer than the lookback window."""
    rules = rules or freshrss.get_rules()
    feed_ids = freshrss.scoring_enabled_feed_ids(rules)
    if not feed_ids:
        return []
    cfg = db.get_all_settings()
    since = int(time.time()) - int(cfg.get("score_lookback_days", 180)) * 86400
    rows = db.fetch_all(
        f"""
        SELECT e.id FROM {freshrss.T_ENTRY} e
        LEFT JOIN ai.entry_state s ON s.entry_id = e.id
        WHERE e.id_feed = ANY(%s)
          AND (e.attributes IS NULL OR e.attributes NOT LIKE '%%"ai_score"%%')
          AND (e.is_read = 0 OR e.date >= %s)
          AND (s.entry_id IS NULL OR s.attempts < %s OR s.updated_at < now() - (%s || ' seconds')::interval)
        ORDER BY e.date DESC LIMIT %s
        """,
        (feed_ids, since, MAX_ATTEMPTS, str(RETRY_AFTER_S), int(limit)),
    )
    return freshrss.get_entries([r["id"] for r in rows], with_content=True) if rows else []


def pending_count() -> int:
    rules = freshrss.get_rules()
    feed_ids = freshrss.scoring_enabled_feed_ids(rules)
    if not feed_ids:
        return 0
    cfg = db.get_all_settings()
    since = int(time.time()) - int(cfg.get("score_lookback_days", 180)) * 86400
    row = db.fetch_one(
        f"""SELECT count(*) AS n FROM {freshrss.T_ENTRY} e
            WHERE e.id_feed = ANY(%s) AND (e.attributes IS NULL OR e.attributes NOT LIKE '%%"ai_score"%%')
              AND (e.is_read = 0 OR e.date >= %s)""",
        (feed_ids, since),
    )
    return int(row["n"]) if row else 0


# ── Summaries ───────────────────────────────────────────────────────────────

def _summary_call(system_tpl: str, user_tpl: str, entry: Entry, *, model: str, effort: str, max_tokens: int,
                  content_chars: int, purpose: str, rules: dict | None = None, stream_cb=None) -> str:
    content = entry_text(entry, content_chars, rules=rules)
    system = [{"type": "text", "text": system_tpl.format(profile=_profile()), "cache_control": {"type": "ephemeral"}}]
    user = user_tpl.format(kind=entry_kind(entry), title=entry.title,
                           source=f"{entry.feed_name} ({entry.category_name})" if entry.category_name else entry.feed_name,
                           date=_fmt_date(entry.date), content=content)
    with llm.client().messages.stream(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
        **llm.thinking_params(model, effort),
    ) as stream:
        if stream_cb:
            for text in stream.text_stream:
                stream_cb(text)
        msg = stream.get_final_message()
    llm.log_usage(purpose, model, msg.usage, ref=str(entry.id))
    if msg.stop_reason == "refusal":
        raise RuntimeError("request was refused")
    return llm.text_of(msg).strip()


def summarize_entry(entry: Entry, *, stream_cb=None, rules: dict | None = None) -> str:
    cfg = db.get_all_settings()
    model = llm.valid_model(cfg["summary_model"], "claude-opus-5")
    effort = llm.valid_effort(cfg["summary_effort"], "low")
    summary = _summary_call(prompts.SUMMARY_SYSTEM, prompts.SUMMARY_USER, entry, model=model, effort=effort,
                            max_tokens=2000, content_chars=SUMMARY_CONTENT_CHARS, purpose="summary", rules=rules,
                            stream_cb=stream_cb)
    if summary:
        freshrss.merge_attributes(entry.id, {"ai_summary": summary, "ai_summary_full": True})
        entry.attributes["ai_summary"] = summary
        entry.attributes["ai_summary_full"] = True
        _mark_summarized(entry.id)
    return summary


def detail_entry(entry: Entry, *, stream_cb=None, rules: dict | None = None) -> str:
    cfg = db.get_all_settings()
    model = llm.valid_model(cfg["summary_model"], "claude-opus-5")
    effort = llm.valid_effort(cfg["summary_effort"], "low")
    detail = _summary_call(prompts.DETAIL_SYSTEM, prompts.DETAIL_USER, entry, model=model, effort=effort,
                           max_tokens=4000, content_chars=SUMMARY_CONTENT_CHARS, purpose="detail", rules=rules,
                           stream_cb=stream_cb)
    if detail:
        freshrss.merge_attributes(entry.id, {"ai_detail": detail})
        entry.attributes["ai_detail"] = detail
    return detail


def summary_candidates(limit: int = 100, *, rules: dict | None = None) -> list[Entry]:
    """Scored entries that deserve a full summary but only have a gist (or nothing)."""
    rules = rules or freshrss.get_rules()
    cfg = db.get_all_settings()
    threshold = int(cfg.get("summary_threshold", 7))
    since = int(time.time()) - int(cfg.get("summary_lookback_days", 45)) * 86400
    feeds = freshrss.list_feeds()
    score_feeds = [int(f["id"]) for f in feeds if freshrss.rule_for(f, rules)["score"]]
    sum_feeds = [int(f["id"]) for f in feeds if freshrss.rule_for(f, rules)["summarize"]]
    if not score_feeds:
        return []
    rows = db.fetch_all(
        f"""
        SELECT e.id FROM {freshrss.T_ENTRY} e
        LEFT JOIN ai.entry_state s ON s.entry_id = e.id
        WHERE e.id_feed = ANY(%s)
          AND e.attributes LIKE '%%"ai_score"%%'
          AND (s.summarized_at IS NULL)
          AND (e.is_read = 0 OR e.date >= %s)
          AND ((e.attributes::jsonb->>'ai_score')::int >= %s OR e.id_feed = ANY(%s))
          AND (e.attributes::jsonb->>'ai_score')::int > 0
          AND (s.entry_id IS NULL OR s.attempts < %s OR s.updated_at < now() - (%s || ' seconds')::interval)
        ORDER BY e.date DESC LIMIT %s
        """,
        (score_feeds, since, threshold, sum_feeds or [0], MAX_ATTEMPTS, str(RETRY_AFTER_S), int(limit)),
    )
    return freshrss.get_entries([r["id"] for r in rows], with_content=True) if rows else []


# ── Enrichment (YouTube) ────────────────────────────────────────────────────

def enrichment_candidates(limit: int = 25) -> list[Entry]:
    cfg = db.get_all_settings()
    since = int(time.time()) - int(cfg.get("enrich_lookback_days", 30)) * 86400
    feed_ids = freshrss.scoring_enabled_feed_ids()
    if not feed_ids:
        return []
    rows = db.fetch_all(
        f"""
        SELECT e.id FROM {freshrss.T_ENTRY} e
        LEFT JOIN ai.entry_state s ON s.entry_id = e.id
        WHERE e.id_feed = ANY(%s) AND e.is_read = 0 AND e.date >= %s
          AND (e.link LIKE '%%youtube.com/%%' OR e.link LIKE '%%youtu.be/%%')
          AND (e.attributes IS NULL OR e.attributes NOT LIKE '%%"yt_is_short"%%')
          AND s.enriched_at IS NULL
        ORDER BY e.date DESC LIMIT %s
        """,
        (feed_ids, since, int(limit)),
    )
    return freshrss.get_entries([r["id"] for r in rows], with_content=False) if rows else []


def enrich_pass(limit: int = 25) -> int:
    cfg = db.get_all_settings()
    n = 0
    for e in enrichment_candidates(limit):
        info = enrich_youtube(e)
        if info is None:
            db.execute(
                """INSERT INTO ai.entry_state (entry_id, enriched_at) VALUES (%s, now())
                   ON CONFLICT (entry_id) DO UPDATE SET enriched_at = now()""", (e.id,))
            continue
        n += 1
        if info.get("yt_is_short"):
            freshrss.merge_attributes(e.id, {"ai_score": 0, "ai_score_reason": "YouTube Short (filtered)"})
            _mark_scored(e.id)
            _apply_labels_and_tags(e.id, 0, [], cfg)
            if cfg.get("mark_shorts_read", True):
                freshrss.mark_read([e.id], True)
    return n


# ── Feedback ────────────────────────────────────────────────────────────────

def apply_feedback(entry: Entry, direction: str, reason: str = "") -> str:
    cfg = db.get_all_settings()
    model = llm.valid_model(cfg["summary_model"], "claude-opus-5")
    profile = (db.get_setting("interest_profile") or "").strip()
    user = prompts.FEEDBACK_USER.format(
        profile=profile, direction="MORE" if direction == "more" else "FEWER", title=entry.title,
        source=entry.feed_name, reason=f' Their reason: "{reason.strip()}".' if reason and reason.strip() else "",
        gist=entry.attributes.get("ai_summary") or truncate(html_to_text(entry.content), 400),
    )
    resp = llm.client().messages.create(
        model=model, max_tokens=6000, system=prompts.FEEDBACK_SYSTEM,
        messages=[{"role": "user", "content": user}],
        **llm.thinking_params(model, "medium"),
    )
    llm.log_usage("feedback", model, resp.usage, ref=str(entry.id))
    new_profile = llm.text_of(resp).strip()
    if new_profile.startswith("```"):
        new_profile = new_profile.strip("`").strip()
    if len(new_profile) < 0.5 * len(profile):
        raise RuntimeError("profile rewrite came back suspiciously short; not applied")
    db.set_setting("interest_profile", new_profile)
    return new_profile


# ── Worker ──────────────────────────────────────────────────────────────────

class Worker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.state: dict[str, Any] = {"running": False, "last_cycle": None, "last_error": None,
                                      "scored_total": 0, "summarized_total": 0, "enriched_total": 0,
                                      "current": None}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="scoring-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def kick(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        # first cycle shortly after boot
        self._wake.wait(5)
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.run_cycle()
            except Exception as e:  # keep the loop alive no matter what
                log.exception("worker cycle failed")
                self.state["last_error"] = str(e)
            self._wake.wait(max(30, settings.scoring_interval_s))

    def run_cycle(self, *, max_score: int | None = None, max_summaries: int | None = None,
                  max_enrich: int | None = None) -> dict[str, int]:
        cfg = db.get_all_settings()
        stats = {"scored": 0, "summarized": 0, "enriched": 0}
        if cfg.get("scoring_paused"):
            return stats
        if not settings.anthropic_api_key:
            self.state["last_error"] = "ANTHROPIC_API_KEY not set"
            return stats
        self.state["running"] = True
        try:
            rules = freshrss.get_rules()
            # 1. Score pending entries, newest first, in waves
            remaining = max_score
            while not self._stop.is_set():
                if db.get_setting("scoring_paused"):
                    break
                limit = 100 if remaining is None else min(100, remaining)
                if limit <= 0:
                    break
                pending = pending_entries(limit, rules=rules)
                if not pending:
                    break
                self.state["current"] = f"scoring {len(pending)} entries"
                res = score_entries(pending)
                stats["scored"] += len(res)
                if remaining is not None:
                    remaining -= len(pending)
                if len(res) == 0:
                    break  # avoid a hot loop on persistent failures
            # 2. Upgrade summaries for high-value entries
            n_sum = 0
            limit_sum = 40 if max_summaries is None else max_summaries
            if limit_sum > 0:
                cands = summary_candidates(limit_sum, rules=rules)
                self.state["current"] = f"summarizing {len(cands)} entries"

                def do_sum(e: Entry) -> int:
                    try:
                        summarize_entry(e, rules=rules)
                        return 1
                    except (anthropic.APIError, RuntimeError, ValueError) as ex:
                        log.warning("summary failed for %s: %s", e.id, ex)
                        _bump_attempt([e.id], str(ex))
                        return 0

                with ThreadPoolExecutor(max_workers=max(1, settings.scoring_concurrency)) as pool:
                    n_sum = sum(pool.map(do_sum, cands))
            stats["summarized"] = n_sum
            # 3. Enrich recent YouTube entries (Shorts + transcripts), a few per cycle
            self.state["current"] = "enriching youtube entries"
            stats["enriched"] = enrich_pass(25 if max_enrich is None else max_enrich)
        finally:
            self.state["running"] = False
            self.state["current"] = None
            self.state["last_cycle"] = datetime.now(timezone.utc).isoformat()
            self.state["scored_total"] += stats["scored"]
            self.state["summarized_total"] += stats["summarized"]
            self.state["enriched_total"] += stats["enriched"]
        if any(stats.values()):
            log.info("worker cycle: %s", stats)
        return stats


worker = Worker()


if __name__ == "__main__":  # manual run: python -m app.scoring [max_score] [max_summaries] [max_enrich]
    import sys
    from . import seed
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.migrate()
    seed.run()
    ms = int(sys.argv[1]) if len(sys.argv) > 1 else None
    msu = int(sys.argv[2]) if len(sys.argv) > 2 else None
    me = int(sys.argv[3]) if len(sys.argv) > 3 else None
    print(worker.run_cycle(max_score=ms, max_summaries=msu, max_enrich=me))
