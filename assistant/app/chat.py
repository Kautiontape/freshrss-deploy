"""Chat with the reader's news: tool-using agent loop with streaming.

Each chat is persisted in ai.chats / ai.chat_messages. Message content is
stored as the API content blocks (including tool_use / tool_result / thinking)
so a conversation can be resumed exactly.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import anthropic
from psycopg.types.json import Jsonb

from . import article, db, freshrss, llm, prompts, scoring
from .config import settings
from .content import html_to_text, truncate
from .freshrss import Entry

log = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 16
MAX_ENTRY_CHARS = 30_000


# ── Tool definitions ────────────────────────────────────────────────────────

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_feeds",
        "description": "List all feeds grouped by category with entry counts, unread counts, and how many unread entries scored high (7+). Use it first to orient, and to find feed/category ids for filtering.",
        "input_schema": {"type": "object", "properties": {
            "since_days": {"type": "integer", "description": "Only count entries published in the last N days (default: all)."}
        }},
    },
    {
        "name": "get_stats",
        "description": "Overview numbers: unread entries by relevance bucket, how many are unscored, and the date range of unread items. Good for 'what am I missing' questions.",
        "input_schema": {"type": "object", "properties": {
            "since_days": {"type": "integer", "description": "Restrict to entries published in the last N days."}
        }},
    },
    {
        "name": "search_entries",
        "description": "Search or list entries. Returns compact rows (id, date, feed, title, score, summary, read flag, url). Filter by text, feeds, categories, unread state, score, and date; sort by date or score. Page with offset.",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Case-insensitive text to match in title or content."},
            "feed_ids": {"type": "array", "items": {"type": "integer"}},
            "category_ids": {"type": "array", "items": {"type": "integer"}},
            "unread_only": {"type": "boolean", "default": False},
            "min_score": {"type": "integer", "description": "Only entries scored at least this (1-10)."},
            "max_score": {"type": "integer"},
            "since": {"type": "string", "description": "ISO date or datetime lower bound (inclusive)."},
            "until": {"type": "string", "description": "ISO date or datetime upper bound (inclusive)."},
            "sort": {"type": "string", "enum": ["date_desc", "date_asc", "score_desc"], "default": "date_desc"},
            "limit": {"type": "integer", "default": 40, "maximum": 200},
            "offset": {"type": "integer", "default": 0},
        }},
    },
    {
        "name": "read_entries",
        "description": "Read the full text of up to 8 entries (article text, fetched full article when enabled for the feed, or the YouTube transcript). Use this before summarizing or answering questions about substance.",
        "input_schema": {"type": "object", "properties": {
            "entry_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
            "max_chars": {"type": "integer", "default": 12000, "description": "Per-entry character cap (max 30000)."},
        }, "required": ["entry_ids"]},
    },
    {
        "name": "mark_read",
        "description": "Mark entries as read (or unread with read=false). Only use this when the reader asked for it in this conversation. Returns the number of entries changed.",
        "input_schema": {"type": "object", "properties": {
            "entry_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "read": {"type": "boolean", "default": True},
        }, "required": ["entry_ids"]},
    },
    {
        "name": "get_interest_profile",
        "description": "Return the reader's current interest profile (markdown) used for relevance scoring.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_interest_profile",
        "description": "Replace the reader's interest profile with new markdown text. Use only when the reader asked to change their interests; keep the existing structure and make targeted edits.",
        "input_schema": {"type": "object", "properties": {"profile": {"type": "string"}}, "required": ["profile"]},
    },
    {
        "name": "list_briefs",
        "description": "List the reader's scheduled briefs and their most recent runs (ids, names, schedules, last run time).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_brief_run",
        "description": "Return the markdown content of a specific brief run and the entry ids it covered.",
        "input_schema": {"type": "object", "properties": {"run_id": {"type": "integer"}}, "required": ["run_id"]},
    },
]

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}


def _article_tools(entry_scoped: bool) -> list[dict[str, Any]]:
    """search_article / read_article. In an entry chat the entry is implied; elsewhere it is a parameter."""
    target = "this article" if entry_scoped else "one long entry"
    entry_prop = {} if entry_scoped else {"entry_id": {"type": "string", "description": "Entry id, as returned by search_entries."}}
    entry_req = [] if entry_scoped else ["entry_id"]
    return [
        {
            "name": "search_article",
            "description": f"Keyword search inside {target}. Returns the best-matching chunk numbers with their section and a snippet. Search on distinctive words (names, companies, numbers); put a phrase in double quotes to require it exactly.",
            "input_schema": {"type": "object", "properties": {
                **entry_prop,
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 8, "maximum": 20},
            }, "required": [*entry_req, "query"]},
        },
        {
            "name": "read_article",
            "description": f"Read a range of chunks from {target}, inclusive (about {article.CHUNK_CHARS} characters each, up to ~{article.READ_MAX_CHARS // 1000}k characters per call). Read a search hit with a chunk or two either side for context, or a whole outline section by its range.",
            "input_schema": {"type": "object", "properties": {
                **entry_prop,
                "start": {"type": "integer", "description": "First chunk number."},
                "end": {"type": "integer", "description": "Last chunk number (default start + 2)."},
            }, "required": [*entry_req, "start"]},
        },
    ]


# ── Tool execution ──────────────────────────────────────────────────────────

def _iso(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def _parse_when(s: str | None, end_of_day: bool = False) -> int | None:
    if not s:
        return None
    s = s.strip()
    try:
        if len(s) == 10:
            d = datetime.fromisoformat(s)
            if end_of_day:
                d = d + timedelta(days=1) - timedelta(seconds=1)
        else:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.astimezone()
        return int(d.timestamp())
    except ValueError:
        return None


def _entry_row(e: Entry) -> dict[str, Any]:
    a = e.attributes
    return {
        "id": str(e.id), "date": _iso(e.date), "feed": e.feed_name, "category": e.category_name,
        "title": e.title, "score": a.get("ai_score"), "summary": truncate(a.get("ai_summary") or "", 300),
        "read": e.is_read, "url": e.link, "video": e.is_youtube,
    }


def tool_list_feeds(args: dict) -> Any:
    since = None
    if args.get("since_days"):
        since = int(time.time()) - int(args["since_days"]) * 86400
    rows = freshrss.feed_stats(since)
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(r["category_name"] or "Uncategorized", []).append({
            "feed_id": r["feed_id"], "feed": r["feed_name"], "category_id": r["category_id"],
            "entries": r["n_entries"], "unread": r["n_unread"], "scored": r["n_scored"],
            "unread_high": r["n_unread_high"], "latest": _iso(r["latest"]),
        })
    return out


def tool_get_stats(args: dict) -> Any:
    since = int(time.time()) - int(args["since_days"]) * 86400 if args.get("since_days") else None
    where = "WHERE e.is_read = 0" + (" AND e.date >= %s" if since else "")
    params = (since,) if since else ()
    row = db.fetch_one(
        f"""
        SELECT count(*) AS unread,
               count(*) FILTER (WHERE (e.attributes::jsonb->>'ai_score')::int >= 7) AS high,
               count(*) FILTER (WHERE (e.attributes::jsonb->>'ai_score')::int BETWEEN 4 AND 6) AS medium,
               count(*) FILTER (WHERE (e.attributes::jsonb->>'ai_score')::int BETWEEN 1 AND 3) AS low,
               count(*) FILTER (WHERE (e.attributes::jsonb->>'ai_score')::int = 0) AS filtered,
               count(*) FILTER (WHERE e.attributes IS NULL OR e.attributes NOT LIKE '%%"ai_score"%%') AS unscored,
               min(e.date) AS oldest, max(e.date) AS newest
        FROM {freshrss.T_ENTRY} e {where}
        """, params)
    return {"unread": row["unread"], "unread_high": row["high"], "unread_medium": row["medium"], "unread_low": row["low"],
            "unread_filtered_shorts": row["filtered"], "unread_unscored": row["unscored"],
            "oldest_unread": _iso(row["oldest"]), "newest_unread": _iso(row["newest"]),
            "scoring_pending": scoring.pending_count()}


def tool_search_entries(args: dict) -> Any:
    entries = freshrss.search_entries(
        query=args.get("query") or None,
        feed_ids=args.get("feed_ids") or None,
        category_ids=args.get("category_ids") or None,
        unread_only=bool(args.get("unread_only")),
        min_score=args.get("min_score"),
        max_score=args.get("max_score"),
        since_ts=_parse_when(args.get("since")),
        until_ts=_parse_when(args.get("until"), end_of_day=True),
        sort=args.get("sort") or "date_desc",
        limit=min(int(args.get("limit") or 40), 200),
        offset=int(args.get("offset") or 0),
    )
    return {"count": len(entries), "entries": [_entry_row(e) for e in entries]}


def _article(entry_id: str) -> article.Article:
    entry = freshrss.get_entry(entry_id, with_content=True)
    if not entry:
        raise ValueError(f"entry {entry_id} not found")
    return article.load(entry)


def tool_search_article(args: dict) -> Any:
    art = _article(str(args["entry_id"]))
    limit = max(1, min(int(args.get("limit") or 8), 20))
    return {"total_chunks": len(art.chunks), "hits": art.search(str(args.get("query") or ""), limit)}


def tool_read_article(args: dict) -> Any:
    art = _article(str(args["entry_id"]))
    start = int(args.get("start") or 0)
    end = int(args["end"]) if args.get("end") is not None else start + 2
    if start >= len(art.chunks):
        return {"error": f"chunk {start} is out of range; the last chunk is #{len(art.chunks) - 1}"}
    return art.read(start, end)


def tool_read_entries(args: dict) -> Any:
    ids = [str(i) for i in (args.get("entry_ids") or [])][:8]
    max_chars = max(500, min(int(args.get("max_chars") or 12000), MAX_ENTRY_CHARS))
    rules = freshrss.get_rules()
    out = []
    for e in freshrss.get_entries(ids, with_content=True):
        text = scoring.entry_text(e, max_chars, rules=rules)
        row = _entry_row(e)
        row.update({"reason": e.attributes.get("ai_score_reason"), "detail": e.attributes.get("ai_detail"),
                    "author": e.author, "text": text})
        if text.endswith(" […]"):
            row["truncated"] = "Text was cut off; use search_article / read_article with this entry id for the rest."
        out.append(row)
    missing = sorted(set(ids) - {r["id"] for r in out})
    return {"entries": out, "missing": missing}


def tool_mark_read(args: dict) -> Any:
    ids = [str(i) for i in (args.get("entry_ids") or [])]
    n = freshrss.mark_read(ids, bool(args.get("read", True)))
    return {"changed": n, "requested": len(ids), "read": bool(args.get("read", True))}


def tool_get_profile(args: dict) -> Any:
    return {"profile": db.get_setting("interest_profile") or ""}


def tool_update_profile(args: dict) -> Any:
    text = (args.get("profile") or "").strip()
    if len(text) < 40:
        return {"error": "profile too short; not saved"}
    db.set_setting("interest_profile", text)
    return {"saved": True, "chars": len(text)}


def tool_list_briefs(args: dict) -> Any:
    from . import briefs
    return briefs.list_briefs_with_runs()


def tool_get_brief_run(args: dict) -> Any:
    from . import briefs
    run = briefs.get_run(int(args.get("run_id") or 0))
    if not run:
        return {"error": "run not found"}
    return {"run_id": run["id"], "brief": run["brief_name"], "period_start": run["period_start"],
            "period_end": run["period_end"], "content": run["content_md"], "entry_ids": [str(i) for i in run["entry_ids"]]}


TOOL_IMPL = {
    "list_feeds": tool_list_feeds,
    "get_stats": tool_get_stats,
    "search_entries": tool_search_entries,
    "read_entries": tool_read_entries,
    "mark_read": tool_mark_read,
    "get_interest_profile": tool_get_profile,
    "update_interest_profile": tool_update_profile,
    "list_briefs": tool_list_briefs,
    "get_brief_run": tool_get_brief_run,
    "search_article": tool_search_article,
    "read_article": tool_read_article,
}


def execute_tool(name: str, args: Any, chat: dict | None = None) -> tuple[str, bool]:
    """Returns (json_text, is_error). In an entry chat, article tools default to that entry."""
    fn = TOOL_IMPL.get(name)
    if fn is None:
        return json.dumps({"error": f"unknown tool {name}"}), True
    if not isinstance(args, dict):
        return json.dumps({"error": "tool input must be an object"}), True
    if name in ("search_article", "read_article") and chat and chat.get("context_type") == "entry":
        args = {**args, "entry_id": chat["context_id"]}
    try:
        result = fn(args)
        return json.dumps(result, ensure_ascii=False, default=str), False
    except Exception as e:
        log.exception("tool %s failed", name)
        return json.dumps({"error": str(e)}), True


# ── Persistence ─────────────────────────────────────────────────────────────

def create_chat(*, title: str = "New chat", context_type: str = "general", context_id: str | None = None,
                model: str | None = None, effort: str | None = None) -> dict:
    cfg = db.get_all_settings()
    row = db.fetch_one(
        """INSERT INTO ai.chats (title, context_type, context_id, model, effort) VALUES (%s, %s, %s, %s, %s)
           RETURNING id, title, context_type, context_id, model, effort, created_at, updated_at""",
        (title, context_type, context_id, llm.valid_model(model or cfg["chat_model"]), llm.valid_effort(effort or cfg["chat_effort"])),
    )
    return dict(row)


def get_chat(chat_id: int) -> dict | None:
    row = db.fetch_one("SELECT * FROM ai.chats WHERE id = %s", (int(chat_id),))
    return dict(row) if row else None


def find_entry_chat(entry_id: str) -> dict | None:
    row = db.fetch_one("SELECT * FROM ai.chats WHERE context_type = 'entry' AND context_id = %s ORDER BY id DESC LIMIT 1", (str(entry_id),))
    return dict(row) if row else None


def list_chats(limit: int = 50, context_type: str | None = None, include_entry: bool = False) -> list[dict]:
    """Chats newest first. Entry chats (opened from the FreshRSS Chat button) are left out
    of the sidebar unless asked for, but they are ordinary chats stored like any other."""
    if context_type:
        rows = db.fetch_all("SELECT * FROM ai.chats WHERE context_type = %s ORDER BY updated_at DESC LIMIT %s", (context_type, limit))
    elif include_entry:
        rows = db.fetch_all("SELECT * FROM ai.chats ORDER BY updated_at DESC LIMIT %s", (limit,))
    else:
        rows = db.fetch_all("SELECT * FROM ai.chats WHERE context_type <> 'entry' ORDER BY updated_at DESC LIMIT %s", (limit,))
    return [dict(r) for r in rows]


def delete_chat(chat_id: int) -> None:
    db.execute("DELETE FROM ai.chats WHERE id = %s", (int(chat_id),))


def update_chat(chat_id: int, **fields: Any) -> None:
    allowed = {"title", "model", "effort"}
    sets = [f"{k} = %s" for k in fields if k in allowed]
    vals = [fields[k] for k in fields if k in allowed]
    if not sets:
        return
    db.execute(f"UPDATE ai.chats SET {', '.join(sets)}, updated_at = now() WHERE id = %s", (*vals, int(chat_id)))


def load_messages(chat_id: int) -> list[dict]:
    rows = db.fetch_all("SELECT id, role, content, model, created_at FROM ai.chat_messages WHERE chat_id = %s ORDER BY id", (int(chat_id),))
    return [dict(r) for r in rows]


def _store(chat_id: int, role: str, content: Any, model: str | None = None) -> int:
    row = db.fetch_one(
        "INSERT INTO ai.chat_messages (chat_id, role, content, model) VALUES (%s, %s, %s, %s) RETURNING id",
        (int(chat_id), role, Jsonb(content), model),
    )
    db.execute("UPDATE ai.chats SET updated_at = now() WHERE id = %s", (int(chat_id),))
    return int(row["id"])


def api_messages(chat_id: int) -> list[dict]:
    """Messages in API form (role + content blocks)."""
    return [{"role": m["role"], "content": m["content"]} for m in load_messages(chat_id)]


def display_messages(chat_id: int) -> list[dict]:
    """Messages simplified for the UI: text, tool calls, tool results (truncated)."""
    out = []
    for m in load_messages(chat_id):
        content = m["content"]
        if isinstance(content, str):
            out.append({"id": m["id"], "role": m["role"], "blocks": [{"type": "text", "text": content}], "created_at": m["created_at"]})
            continue
        blocks = []
        for b in content:
            t = b.get("type")
            if t == "text":
                blocks.append({"type": "text", "text": b.get("text", "")})
            elif t == "thinking" and b.get("thinking"):
                blocks.append({"type": "thinking", "text": b.get("thinking", "")})
            elif t == "tool_use":
                blocks.append({"type": "tool_use", "name": b.get("name"), "input": b.get("input")})
            elif t == "server_tool_use":
                blocks.append({"type": "tool_use", "name": b.get("name"), "input": b.get("input")})
            elif t == "tool_result":
                c = b.get("content")
                text = c if isinstance(c, str) else json.dumps(c)[:400]
                blocks.append({"type": "tool_result", "text": truncate(text, 400), "is_error": bool(b.get("is_error"))})
            elif t == "web_search_tool_result":
                blocks.append({"type": "tool_result", "text": "web search results", "is_error": False})
        if blocks:
            out.append({"id": m["id"], "role": m["role"], "blocks": blocks, "created_at": m["created_at"]})
    return out


# ── System prompt ───────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.now().astimezone().strftime("%A, %B %-d, %Y %H:%M %Z")


def _entry_article(chat: dict) -> article.Article:
    entry = freshrss.get_entry(chat["context_id"], with_content=True)
    if not entry:
        raise ValueError("entry not found")
    return article.load(entry)


def build_system(chat: dict) -> tuple[list[dict], list[dict]]:
    """Returns (system_blocks, tools) for a chat."""
    profile = (db.get_setting("interest_profile") or "").strip()
    ctype = chat.get("context_type") or "general"
    if ctype == "entry":
        art = _entry_article(chat)
        entry = art.entry
        extras = ""
        if entry.attributes.get("ai_summary"):
            extras += f"\nPrevious summary: {entry.attributes['ai_summary']}\n"
        if entry.attributes.get("ai_detail"):
            extras += f"\nPrevious breakdown:\n{entry.attributes['ai_detail']}\n"
        kind = scoring.entry_kind(entry)
        common = dict(kind=kind, kind_cap=kind[0].upper() + kind[1:], title=entry.title, source=entry.feed_name,
                      date=scoring._fmt_date(entry.date), extras=extras, profile=profile)
        if art.is_long:
            outline = article.outline_nowait(art) or "(The outline is still being built. Use search_article to find things, or read_article to go through the chunks in order.)"
            opening, opening_last = art.opening()
            text = prompts.ENTRY_CHAT_LONG_SYSTEM.format(
                **common, chars=len(art.text), n_chunks=len(art.chunks), last=len(art.chunks) - 1,
                outline=outline, opening=opening, opening_last=opening_last)
            tools = _article_tools(entry_scoped=True) + [WEB_SEARCH_TOOL]
        else:
            text = prompts.ENTRY_CHAT_SYSTEM.format(**common, content=art.text)
            tools = [WEB_SEARCH_TOOL]
        system = [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}},
                  {"type": "text", "text": f"Current date/time: {_today()}"}]
        return system, tools

    text = prompts.CHAT_SYSTEM.format(profile=profile or "(none configured)")
    blocks = [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
    if ctype == "brief":
        from . import briefs
        run = briefs.get_run(int(chat["context_id"]))
        if run:
            blocks.append({"type": "text", "text": prompts.CHAT_CONTEXT_BRIEF.format(
                name=run["brief_name"], period=f"{run['period_start']} to {run['period_end']}", run_id=run["id"],
                content=run["content_md"] or "", entry_ids=", ".join(str(i) for i in run["entry_ids"]))})
    blocks.append({"type": "text", "text": f"Current date/time: {_today()}. Reader timezone: {settings.timezone}. FreshRSS is at {settings.freshrss_public_url}."})
    return blocks, TOOLS + _article_tools(entry_scoped=False) + [WEB_SEARCH_TOOL]


# ── Agent loop ──────────────────────────────────────────────────────────────

def _blocks_json(message: Any) -> list[dict]:
    return [b.model_dump(mode="json", exclude_none=True) for b in message.content]


def run_turn(chat_id: int, user_text: str, *, model: str | None = None, effort: str | None = None) -> Iterator[dict]:
    """Run one user turn. Yields UI events: text, thinking, status, tool_call, tool_result, done, error."""
    chat = get_chat(chat_id)
    if not chat:
        yield {"type": "error", "message": "chat not found"}
        return
    if model or effort:
        update_chat(chat_id, model=llm.valid_model(model or chat["model"]), effort=llm.valid_effort(effort or chat["effort"]))
        chat = get_chat(chat_id) or chat
    model = llm.valid_model(chat["model"])
    effort = llm.valid_effort(chat["effort"])

    history = api_messages(chat_id)
    if not history and chat["title"] == "New chat":
        update_chat(chat_id, title=truncate(user_text.strip().splitlines()[0] if user_text.strip() else "New chat", 70))
    _store(chat_id, "user", [{"type": "text", "text": user_text}])
    history.append({"role": "user", "content": [{"type": "text", "text": user_text}]})

    try:
        system, tools = build_system(chat)
    except ValueError as e:
        yield {"type": "error", "message": str(e)}
        return

    purpose = "chat_entry" if chat["context_type"] == "entry" else "chat"
    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            with llm.api(model).stream(
                model=model, max_tokens=16000, system=system, messages=history, tools=tools,
                cache_control={"type": "ephemeral"},
                **llm.thinking_params(model, effort, display="summarized"),
                **llm.fallback_params(model),
            ) as stream:
                for event in stream:
                    et = event.type
                    if et == "content_block_start":
                        cb = event.content_block
                        if cb.type == "tool_use":
                            yield {"type": "status", "text": f"Calling {cb.name}…"}
                        elif cb.type == "server_tool_use":
                            yield {"type": "status", "text": "Searching the web…"}
                    elif et == "content_block_delta":
                        d = event.delta
                        if d.type == "text_delta":
                            yield {"type": "text", "text": d.text}
                        elif d.type == "thinking_delta" and d.thinking:
                            yield {"type": "thinking", "text": d.thinking}
                msg = stream.get_final_message()
        except anthropic.APIError as e:
            log.warning("chat API error: %s", e)
            yield {"type": "error", "message": f"API error: {getattr(e, 'message', str(e))}"}
            return

        llm.log_usage(purpose, model, msg.usage, ref=str(chat_id))
        blocks = _blocks_json(msg)
        _store(chat_id, "assistant", blocks, model=model)
        history.append({"role": "assistant", "content": blocks})

        if msg.stop_reason == "pause_turn":
            continue
        if msg.stop_reason == "refusal":
            yield {"type": "error", "message": "The model declined this request."}
            break
        tool_uses = [b for b in msg.content if b.type == "tool_use"]
        if not tool_uses or msg.stop_reason == "max_tokens":
            if msg.stop_reason == "max_tokens":
                yield {"type": "status", "text": "Response hit the length limit."}
            break

        results = []
        for tu in tool_uses:
            yield {"type": "tool_call", "name": tu.name, "input": tu.input}
            out, is_err = execute_tool(tu.name, tu.input, chat)
            yield {"type": "tool_result", "name": tu.name, "text": truncate(out, 300), "is_error": is_err}
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out, "is_error": is_err})
        _store(chat_id, "user", results)
        history.append({"role": "user", "content": results})
    else:
        yield {"type": "status", "text": "Stopped after too many tool calls."}

    yield {"type": "done", "chat_id": chat_id}
