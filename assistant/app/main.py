"""FastAPI application: UI + API for the browser, internal API for the FreshRSS extension."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import briefs, chat, db, freshrss, llm, scoring, seed
from .config import settings
from .scoring import worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx2").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
COOKIE = "assistant_session"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.migrate()
    seed.run()
    if not settings.ui_password:
        log.warning("ASSISTANT_PASSWORD is not set: the UI is open to anyone who can reach this port")
    if not settings.internal_token:
        log.warning("ASSISTANT_INTERNAL_TOKEN is not set: the FreshRSS extension cannot call this service")
    if settings.worker_enabled:
        worker.start()
        briefs.scheduler.start()
    yield
    worker.stop()
    briefs.scheduler.stop()


app = FastAPI(title="FreshRSS Assistant", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Auth ────────────────────────────────────────────────────────────────────

def _session_token() -> str:
    secret = (settings.cookie_secret or "assistant").encode()
    return hmac.new(secret, b"assistant-session-v1", hashlib.sha256).hexdigest()


def require_ui(request: Request) -> None:
    if not settings.ui_password:
        return
    if hmac.compare_digest(request.cookies.get(COOKIE, ""), _session_token()):
        return
    raise HTTPException(status_code=401, detail="login required")


def require_internal(request: Request) -> None:
    token = request.headers.get("x-assistant-token", "")
    if settings.internal_token and hmac.compare_digest(token, settings.internal_token):
        return
    # A logged-in browser session may also use internal endpoints (handy for debugging)
    if settings.ui_password and hmac.compare_digest(request.cookies.get(COOKIE, ""), _session_token()):
        return
    if not settings.internal_token and not settings.ui_password:
        return
    raise HTTPException(status_code=401, detail="invalid internal token")


class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginBody, response: Response):
    if not settings.ui_password:
        return {"ok": True, "auth": False}
    if not hmac.compare_digest(body.password, settings.ui_password):
        time.sleep(0.8)
        raise HTTPException(status_code=403, detail="wrong password")
    response.set_cookie(COOKIE, _session_token(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 90)
    return {"ok": True, "auth": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/sso")
def sso(ts: str = "", sig: str = "", next: str = "#/chat"):
    """Sign-in from FreshRSS: HMAC-SHA256 over 'sso:<ts>' with the shared internal token, valid 5 minutes."""
    from fastapi.responses import RedirectResponse
    target = next if next.startswith("#/") else "#/chat"
    resp = RedirectResponse(url="/" + target, status_code=303)
    if not settings.ui_password:
        return resp
    try:
        age = abs(time.time() - int(ts))
    except ValueError:
        raise HTTPException(400, "bad timestamp")
    if not settings.internal_token or age > 300:
        raise HTTPException(403, "sign-in link expired")
    expected = hmac.new(settings.internal_token.encode(), f"sso:{ts}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(403, "bad signature")
    resp.set_cookie(COOKIE, _session_token(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 90)
    return resp


# ── Static UI ───────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True}


# ── Status / settings ───────────────────────────────────────────────────────

SETTING_KEYS = set(db.DEFAULT_SETTINGS.keys())


@app.get("/api/status", dependencies=[Depends(require_ui)])
def status():
    cfg = db.get_all_settings()
    usage = llm.usage_summary(30)
    return {
        "worker": dict(worker.state, enabled=settings.worker_enabled, interval_s=settings.scoring_interval_s),
        "pending": scoring.pending_count(),
        "unread": freshrss.count_entries(unread_only=True),
        "scoring_paused": bool(cfg.get("scoring_paused")),
        "usage_today_usd": usage["today_usd"], "usage_30d_usd": usage["total_usd"],
        "api_key_set": bool(settings.anthropic_api_key),
        "email_configured": bool(settings.smtp_user and settings.smtp_password and settings.email_to),
        "freshrss_url": settings.freshrss_public_url,
        "briefs_running": sorted(briefs.scheduler.running),
    }


@app.get("/api/models", dependencies=[Depends(require_ui)])
def models():
    return {"models": [{"id": k, **v} for k, v in llm.MODELS.items()], "efforts": llm.EFFORTS}


@app.get("/api/settings", dependencies=[Depends(require_ui)])
def get_settings():
    return db.get_all_settings()


@app.put("/api/settings", dependencies=[Depends(require_ui)])
def put_settings(body: dict[str, Any]):
    clean: dict[str, Any] = {}
    for k, v in body.items():
        if k not in SETTING_KEYS:
            continue
        if k.endswith("_model"):
            v = llm.valid_model(str(v))
        elif k.endswith("_effort"):
            v = llm.valid_effort(str(v))
        elif k in ("summary_threshold", "score_lookback_days", "summary_lookback_days", "enrich_lookback_days",
                   "label_high_min", "label_medium_min"):
            v = int(v)
        elif k in ("mark_shorts_read", "scoring_paused", "write_labels", "write_topic_tags"):
            v = bool(v)
        elif k == "topic_tags":
            if isinstance(v, str):
                v = [t.strip() for t in v.replace("\n", ",").split(",")]
            v = [str(t).strip().lower().replace(" ", "-") for t in v if str(t).strip()]
        elif k == "interest_profile":
            v = str(v)
        clean[k] = v
    db.set_settings(clean)
    if "scoring_paused" in clean and not clean["scoring_paused"]:
        worker.kick()
    return db.get_all_settings()


@app.get("/api/usage", dependencies=[Depends(require_ui)])
def usage(days: int = 30):
    return llm.usage_summary(max(1, min(days, 365)))


# ── Feeds / rules ───────────────────────────────────────────────────────────

@app.get("/api/feeds", dependencies=[Depends(require_ui)])
def feeds():
    rules = freshrss.get_rules()
    stats = {r["feed_id"]: r for r in freshrss.feed_stats()}
    cats: dict[int, dict] = {}
    for f in freshrss.list_feeds():
        cid = int(f["category_id"] or 0)
        c = cats.setdefault(cid, {"id": cid, "name": f["category_name"] or "Uncategorized",
                                  "rule": rules["category"].get(cid, {"score": False, "summarize": False, "fetch_full": False}),
                                  "feeds": []})
        st = stats.get(int(f["id"]), {})
        c["feeds"].append({
            "id": int(f["id"]), "name": f["name"], "url": f["url"], "website": f["website"], "error": f["error"],
            "rule": rules["feed"].get(int(f["id"]), {"score": False, "summarize": False, "fetch_full": False}),
            "effective": freshrss.rule_for(f, rules),
            "n_entries": st.get("n_entries", 0), "n_unread": st.get("n_unread", 0), "n_scored": st.get("n_scored", 0),
            "n_unread_high": st.get("n_unread_high", 0), "latest": st.get("latest"),
        })
    return {"categories": list(cats.values())}


class RuleBody(BaseModel):
    scope: str
    ref_id: int
    score: bool = False
    summarize: bool = False
    fetch_full: bool = False


@app.put("/api/rules", dependencies=[Depends(require_ui)])
def put_rules(body: list[RuleBody]):
    for r in body:
        if r.scope not in ("feed", "category"):
            raise HTTPException(400, "scope must be feed or category")
        freshrss.set_rule(r.scope, r.ref_id, score=r.score, summarize=r.summarize, fetch_full=r.fetch_full)
    worker.kick()
    return {"ok": True, "pending": scoring.pending_count()}


@app.post("/api/worker/run", dependencies=[Depends(require_ui)])
def worker_run():
    worker.kick()
    return {"ok": True}


class RescoreBody(BaseModel):
    feed_ids: list[int] = Field(default_factory=list)
    category_ids: list[int] = Field(default_factory=list)
    since_days: int | None = None
    unread_only: bool = True


@app.post("/api/rescore", dependencies=[Depends(require_ui)])
def rescore(body: RescoreBody):
    since = int(time.time()) - body.since_days * 86400 if body.since_days else None
    entries = freshrss.search_entries(feed_ids=body.feed_ids or None, category_ids=body.category_ids or None,
                                      unread_only=body.unread_only, since_ts=since, limit=500, with_content=False)
    ids = [e.id for e in entries]
    n = freshrss.clear_ai_attributes(ids) if ids else 0
    db.execute("DELETE FROM ai.entry_state WHERE entry_id = ANY(%s)", (ids,)) if ids else None
    worker.kick()
    return {"cleared": n}


# ── Entries (reader view) ───────────────────────────────────────────────────

@app.get("/api/entries", dependencies=[Depends(require_ui)])
def entries(query: str | None = None, feed_id: int | None = None, category_id: int | None = None,
            unread_only: bool = True, min_score: int | None = None, since_days: int | None = None,
            sort: str = "score_desc", limit: int = 60, offset: int = 0):
    since = int(time.time()) - since_days * 86400 if since_days else None
    rows = freshrss.search_entries(query=query, feed_ids=[feed_id] if feed_id else None,
                                   category_ids=[category_id] if category_id else None, unread_only=unread_only,
                                   min_score=min_score, since_ts=since, sort=sort, limit=limit, offset=offset)
    return {"entries": [chat._entry_row(e) | {"reason": e.attributes.get("ai_score_reason")} for e in rows]}


class MarkBody(BaseModel):
    entry_ids: list[str]
    read: bool = True


@app.post("/api/entries/mark", dependencies=[Depends(require_ui)])
def mark(body: MarkBody):
    return {"changed": freshrss.mark_read(body.entry_ids, body.read)}


# ── Chats ───────────────────────────────────────────────────────────────────

def sse(gen: Iterator[dict]) -> StreamingResponse:
    def stream():
        for ev in gen:
            yield "data: " + json.dumps(ev, ensure_ascii=False, default=str) + "\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ChatCreate(BaseModel):
    title: str = "New chat"
    context_type: str = "general"
    context_id: str | None = None
    model: str | None = None
    effort: str | None = None


class MessageBody(BaseModel):
    text: str
    model: str | None = None
    effort: str | None = None


@app.get("/api/chats", dependencies=[Depends(require_ui)])
def chats():
    return {"chats": chat.list_chats()}


@app.post("/api/chats", dependencies=[Depends(require_ui)])
def chats_create(body: ChatCreate):
    if body.context_type not in ("general", "brief", "entry"):
        raise HTTPException(400, "bad context_type")
    return chat.create_chat(title=body.title, context_type=body.context_type, context_id=body.context_id,
                            model=body.model, effort=body.effort)


@app.get("/api/chats/{chat_id}", dependencies=[Depends(require_ui)])
def chat_get(chat_id: int):
    c = chat.get_chat(chat_id)
    if not c:
        raise HTTPException(404, "not found")
    return {"chat": c, "messages": chat.display_messages(chat_id)}


@app.delete("/api/chats/{chat_id}", dependencies=[Depends(require_ui)])
def chat_delete(chat_id: int):
    chat.delete_chat(chat_id)
    return {"ok": True}


@app.post("/api/chats/{chat_id}/message", dependencies=[Depends(require_ui)])
def chat_message(chat_id: int, body: MessageBody):
    if not body.text.strip():
        raise HTTPException(400, "empty message")
    return sse(chat.run_turn(chat_id, body.text, model=body.model, effort=body.effort))


# ── Briefs ──────────────────────────────────────────────────────────────────

@app.get("/api/briefs", dependencies=[Depends(require_ui)])
def briefs_list():
    return {"briefs": briefs.list_briefs(), "runs": briefs.list_runs(limit=40),
            "running": sorted(briefs.scheduler.running)}


@app.post("/api/briefs", dependencies=[Depends(require_ui)])
def briefs_create(body: dict[str, Any]):
    try:
        return briefs.create_brief(body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/briefs/{brief_id}", dependencies=[Depends(require_ui)])
def briefs_update(brief_id: int, body: dict[str, Any]):
    try:
        b = briefs.update_brief(brief_id, body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not b:
        raise HTTPException(404, "not found")
    return b


@app.delete("/api/briefs/{brief_id}", dependencies=[Depends(require_ui)])
def briefs_delete(brief_id: int):
    briefs.delete_brief(brief_id)
    return {"ok": True}


class RunBody(BaseModel):
    window_hours: int | None = None


@app.post("/api/briefs/{brief_id}/run", dependencies=[Depends(require_ui)])
def briefs_run(brief_id: int, body: RunBody | None = None):
    if not briefs.get_brief(brief_id):
        raise HTTPException(404, "not found")
    started = briefs.scheduler.run_async(brief_id, force_window_hours=(body.window_hours if body else None))
    return {"started": started}


@app.get("/api/briefs/{brief_id}/runs", dependencies=[Depends(require_ui)])
def briefs_runs(brief_id: int):
    return {"runs": briefs.list_runs(brief_id)}


@app.get("/api/runs/{run_id}", dependencies=[Depends(require_ui)])
def run_get(run_id: int):
    r = briefs.get_run(run_id)
    if not r:
        raise HTTPException(404, "not found")
    return r


# ── Internal API (FreshRSS extension) ───────────────────────────────────────

class ScoreBody(BaseModel):
    entry_ids: list[str]


@app.post("/internal/score", dependencies=[Depends(require_internal)])
def internal_score(body: ScoreBody):
    if not settings.anthropic_api_key:
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured")
    rules = freshrss.get_rules()
    entries = freshrss.get_entries(body.entry_ids[:50], with_content=True)
    scores: dict[str, Any] = {}
    skipped: list[str] = []
    todo = []
    for e in entries:
        a = e.attributes
        if a.get("ai_score") is not None:
            scores[str(e.id)] = {"score": a["ai_score"], "reason": a.get("ai_score_reason", ""), "summary": a.get("ai_summary", "")}
        elif freshrss.rule_for(e, rules)["score"]:
            todo.append(e)
        else:
            skipped.append(str(e.id))
    if todo:
        res = scoring.score_entries(todo, purpose="score_now")
        for eid, r in res.items():
            scores[str(eid)] = {"score": r["score"], "reason": r["reason"], "summary": r.get("summary", "")}
    failed = [str(e.id) for e in todo if str(e.id) not in scores]
    return {"status": "ok", "scores": scores, "skipped": skipped, "failed": failed}


def _text_sse(fn, cached: str | None) -> StreamingResponse:
    """Stream text chunks as {"text": ...} frames, ending with {"done": true}."""
    def gen():
        if cached:
            yield {"text": cached}
            yield {"done": True}
            return
        chunks: list[str] = []
        import queue, threading
        q: queue.Queue = queue.Queue()

        def cb(t: str) -> None:
            q.put(("text", t))

        def run() -> None:
            try:
                fn(cb)
                q.put(("done", None))
            except Exception as e:  # surface as an SSE error frame
                log.warning("stream generation failed: %s", e)
                q.put(("error", str(e)))

        threading.Thread(target=run, daemon=True).start()
        while True:
            kind, val = q.get()
            if kind == "text":
                chunks.append(val)
                yield {"text": val}
            elif kind == "error":
                yield {"error": val}
                return
            else:
                yield {"done": True}
                return
    return sse(gen())


def _load_entry(entry_id: str) -> freshrss.Entry:
    e = freshrss.get_entry(entry_id, with_content=True)
    if not e:
        raise HTTPException(404, "entry not found")
    return e


class ForceBody(BaseModel):
    force: bool = False


@app.post("/internal/entries/{entry_id}/summarize", dependencies=[Depends(require_internal)])
def internal_summarize(entry_id: str, body: ForceBody | None = None):
    e = _load_entry(entry_id)
    force = bool(body and body.force)
    st = db.fetch_one("SELECT summarized_at FROM ai.entry_state WHERE entry_id = %s", (e.id,))
    cached = e.attributes.get("ai_summary") if (st and st["summarized_at"] and not force) else None
    return _text_sse(lambda cb: scoring.summarize_entry(e, stream_cb=cb), cached)


@app.post("/internal/entries/{entry_id}/detail", dependencies=[Depends(require_internal)])
def internal_detail(entry_id: str, body: ForceBody | None = None):
    e = _load_entry(entry_id)
    cached = None if (body and body.force) else e.attributes.get("ai_detail")
    return _text_sse(lambda cb: scoring.detail_entry(e, stream_cb=cb), cached)


class EntryChatBody(BaseModel):
    message: str
    model: str | None = None
    effort: str | None = None


@app.post("/internal/entries/{entry_id}/chat", dependencies=[Depends(require_internal)])
def internal_entry_chat(entry_id: str, body: EntryChatBody):
    e = _load_entry(entry_id)
    c = chat.find_entry_chat(str(e.id)) or chat.create_chat(title=e.title[:70] or "Entry chat", context_type="entry",
                                                             context_id=str(e.id), model=body.model, effort=body.effort or "medium")

    def gen():
        for ev in chat.run_turn(c["id"], body.message, model=body.model, effort=body.effort):
            if ev["type"] == "text":
                yield {"text": ev["text"]}
            elif ev["type"] == "status":
                yield {"status": ev["text"]}
            elif ev["type"] == "error":
                yield {"error": ev["message"]}
            elif ev["type"] == "done":
                yield {"done": True, "chat_id": c["id"]}
    return sse(gen())


@app.get("/internal/entries/{entry_id}/chat", dependencies=[Depends(require_internal)])
def internal_entry_chat_history(entry_id: str):
    c = chat.find_entry_chat(str(entry_id))
    if not c:
        return {"chat_id": None, "messages": []}
    msgs = []
    for m in chat.display_messages(c["id"]):
        text = "".join(b["text"] for b in m["blocks"] if b["type"] == "text")
        if text.strip():
            msgs.append({"role": m["role"], "text": text})
    return {"chat_id": c["id"], "messages": msgs}


class FeedbackBody(BaseModel):
    entry_id: str
    direction: str
    reason: str = ""


@app.post("/internal/feedback", dependencies=[Depends(require_internal)])
def internal_feedback(body: FeedbackBody):
    e = _load_entry(body.entry_id)
    if body.direction not in ("more", "less"):
        raise HTTPException(400, "direction must be more or less")
    try:
        scoring.apply_feedback(e, body.direction, body.reason)
    except Exception as ex:
        raise HTTPException(500, str(ex))
    return {"status": "ok", "profile_changed": True}


@app.post("/internal/entries/{entry_id}/transcript", dependencies=[Depends(require_internal)])
def internal_transcript(entry_id: str):
    e = _load_entry(entry_id)
    if e.attributes.get("yt_transcript") is None:
        scoring.enrich_youtube(e)
    t = e.attributes.get("yt_transcript") or ""
    return {"status": "ok" if t else "error", "transcript": t, "message": None if t else "Transcript unavailable"}


@app.post("/internal/entries/{entry_id}/full_content", dependencies=[Depends(require_internal)])
def internal_full_content(entry_id: str):
    e = _load_entry(entry_id)
    content = e.attributes.get("full_content")
    if not content:
        from .content import fetch_full_article
        content = fetch_full_article(e.link) or ""
        freshrss.merge_attributes(e.id, {"full_content": content})
    return {"status": "ok" if content else "error", "content": content, "message": None if content else "Could not fetch full content"}


@app.get("/internal/health", dependencies=[Depends(require_internal)])
def internal_health():
    return {"ok": True, "pending": scoring.pending_count(), "worker": worker.state}
