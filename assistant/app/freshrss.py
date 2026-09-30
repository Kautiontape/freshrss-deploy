"""Read/write access to the FreshRSS tables (entries, feeds, categories).

All AI-generated data is stored in the entry `attributes` JSON column using the
same keys the FreshRSS extension renders (ai_score, ai_summary, ...). Writes are
atomic JSONB merges so a concurrent FreshRSS refresh can't clobber them.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from . import db
from .config import settings

T_ENTRY = f"{settings.freshrss_user}_entry"
T_FEED = f"{settings.freshrss_user}_feed"
T_CAT = f"{settings.freshrss_user}_category"

AI_KEYS = ("ai_score", "ai_score_reason", "ai_summary", "ai_detail", "ai_chat",
           "yt_transcript", "yt_is_short", "yt_duration", "full_content")


@dataclass
class Entry:
    id: int
    feed_id: int
    feed_name: str
    category_id: int
    category_name: str
    title: str
    author: str
    link: str
    date: int
    is_read: bool
    content: str
    attributes: dict[str, Any]

    @property
    def score(self) -> int | None:
        v = self.attributes.get("ai_score")
        return int(v) if v is not None else None

    @property
    def is_youtube(self) -> bool:
        return youtube_video_id(self.link) is not None


def youtube_video_id(url: str) -> str | None:
    import re
    m = re.search(r"(?:youtube\.com/watch\?.*v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def _parse_attrs(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


def _row_to_entry(r: dict) -> Entry:
    return Entry(
        id=int(r["id"]),
        feed_id=int(r["id_feed"] or 0),
        feed_name=r.get("feed_name") or "Unknown",
        category_id=int(r.get("category_id") or 0),
        category_name=r.get("category_name") or "",
        title=r["title"] or "",
        author=r.get("author") or "",
        link=r["link"] or "",
        date=int(r["date"] or 0),
        is_read=bool(r["is_read"]),
        content=r.get("content") or "",
        attributes=_parse_attrs(r.get("attributes")),
    )


_SELECT = f"""
SELECT e.id, e.id_feed, e.title, e.author, e.link, e.date, e.is_read, e.attributes,
       {{content}} AS content,
       f.name AS feed_name, f.category AS category_id, c.name AS category_name
FROM {T_ENTRY} e
LEFT JOIN {T_FEED} f ON f.id = e.id_feed
LEFT JOIN {T_CAT} c ON c.id = f.category
"""


def _select(with_content: bool) -> str:
    return _SELECT.format(content="e.content" if with_content else "''")


# ── Feeds / categories ───────────────────────────────────────────────────────

def list_categories() -> list[dict]:
    return db.fetch_all(f"SELECT id, name FROM {T_CAT} ORDER BY name")


def list_feeds() -> list[dict]:
    return db.fetch_all(
        f"""
        SELECT f.id, f.name, f.url, f.website, f.category AS category_id, c.name AS category_name,
               f."cache_nbEntries" AS n_entries, f."cache_nbUnreads" AS n_unread, f.error
        FROM {T_FEED} f LEFT JOIN {T_CAT} c ON c.id = f.category
        ORDER BY c.name, f.name
        """
    )


def feed_stats(since_ts: int | None = None) -> list[dict]:
    join_cond = "e.id_feed = f.id" + (" AND e.date >= %s" if since_ts else "")
    params = (since_ts,) if since_ts else ()
    return db.fetch_all(
        f"""
        SELECT f.id AS feed_id, f.name AS feed_name, c.id AS category_id, c.name AS category_name,
               count(e.id) AS n_entries,
               count(e.id) FILTER (WHERE e.is_read = 0) AS n_unread,
               count(e.id) FILTER (WHERE e.attributes LIKE '%%"ai_score"%%') AS n_scored,
               count(e.id) FILTER (WHERE e.is_read = 0 AND (e.attributes::jsonb->>'ai_score')::int >= 7) AS n_unread_high,
               max(e.date) AS latest
        FROM {T_FEED} f
        LEFT JOIN {T_CAT} c ON c.id = f.category
        LEFT JOIN {T_ENTRY} e ON {join_cond}
        GROUP BY f.id, f.name, c.id, c.name
        ORDER BY c.name, f.name
        """,
        params,
    )


# ── Entries ─────────────────────────────────────────────────────────────────

def get_entry(entry_id: int | str, with_content: bool = True) -> Entry | None:
    row = db.fetch_one(_select(with_content) + " WHERE e.id = %s", (int(entry_id),))
    return _row_to_entry(row) if row else None


def get_entries(entry_ids: Iterable[int | str], with_content: bool = True) -> list[Entry]:
    ids = [int(i) for i in entry_ids]
    if not ids:
        return []
    rows = db.fetch_all(_select(with_content) + " WHERE e.id = ANY(%s) ORDER BY e.date DESC", (ids,))
    return [_row_to_entry(r) for r in rows]


def search_entries(
    *,
    query: str | None = None,
    feed_ids: list[int] | None = None,
    category_ids: list[int] | None = None,
    unread_only: bool = False,
    min_score: int | None = None,
    max_score: int | None = None,
    since_ts: int | None = None,
    until_ts: int | None = None,
    unscored_only: bool = False,
    sort: str = "date_desc",
    limit: int = 50,
    offset: int = 0,
    with_content: bool = False,
) -> list[Entry]:
    clauses: list[str] = []
    params: list[Any] = []
    if query:
        clauses.append("(e.title ILIKE %s OR e.content ILIKE %s)")
        like = f"%{query}%"
        params += [like, like]
    if feed_ids:
        clauses.append("e.id_feed = ANY(%s)")
        params.append([int(x) for x in feed_ids])
    if category_ids:
        clauses.append("f.category = ANY(%s)")
        params.append([int(x) for x in category_ids])
    if unread_only:
        clauses.append("e.is_read = 0")
    if min_score is not None:
        clauses.append("(e.attributes::jsonb->>'ai_score')::int >= %s")
        params.append(int(min_score))
    if max_score is not None:
        clauses.append("(e.attributes::jsonb->>'ai_score')::int <= %s")
        params.append(int(max_score))
    if since_ts is not None:
        clauses.append("e.date >= %s")
        params.append(int(since_ts))
    if until_ts is not None:
        clauses.append("e.date <= %s")
        params.append(int(until_ts))
    if unscored_only:
        clauses.append("(e.attributes IS NULL OR e.attributes NOT LIKE '%%\"ai_score\"%%')")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    order = {
        "date_desc": "e.date DESC",
        "date_asc": "e.date ASC",
        "score_desc": "(e.attributes::jsonb->>'ai_score')::int DESC NULLS LAST, e.date DESC",
    }.get(sort, "e.date DESC")
    params += [max(1, min(int(limit), 500)), max(0, int(offset))]
    rows = db.fetch_all(_select(with_content) + f" {where} ORDER BY {order} LIMIT %s OFFSET %s", params)
    return [_row_to_entry(r) for r in rows]


def count_entries(*, unread_only: bool = False, since_ts: int | None = None, unscored_only: bool = False,
                  feed_ids: list[int] | None = None) -> int:
    clauses, params = [], []
    if unread_only:
        clauses.append("e.is_read = 0")
    if since_ts is not None:
        clauses.append("e.date >= %s"); params.append(int(since_ts))
    if unscored_only:
        clauses.append("(e.attributes IS NULL OR e.attributes NOT LIKE '%%\"ai_score\"%%')")
    if feed_ids:
        clauses.append("e.id_feed = ANY(%s)"); params.append(feed_ids)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    row = db.fetch_one(f"SELECT count(*) AS n FROM {T_ENTRY} e {where}", params)
    return int(row["n"]) if row else 0


# ── Attribute writes (atomic merge) ─────────────────────────────────────────

# FreshRSS stores an empty attribute set as '[]' (PHP json_encode of []); only
# treat a JSON object as existing attributes, everything else starts from {}.
_ATTR_OBJ = ("(CASE WHEN jsonb_typeof(COALESCE(NULLIF(attributes, ''), '{}')::jsonb) = 'object' "
             "THEN COALESCE(NULLIF(attributes, ''), '{}')::jsonb ELSE '{}'::jsonb END)")

def merge_attributes(entry_id: int | str, values: dict[str, Any]) -> None:
    """Merge keys into the entry's attributes JSON without touching other keys."""
    db.execute(
        f"""
        UPDATE {T_ENTRY}
        SET attributes = ({_ATTR_OBJ} || %s::jsonb)::text
        WHERE id = %s
        """,
        (Jsonb(values), int(entry_id)),
    )


def remove_attributes(entry_id: int | str, keys: Iterable[str]) -> None:
    db.execute(
        f"""
        UPDATE {T_ENTRY}
        SET attributes = ({_ATTR_OBJ} - %s::text[])::text
        WHERE id = %s
        """,
        (list(keys), int(entry_id)),
    )


def clear_ai_attributes(entry_ids: Iterable[int | str] | None = None, keys: Iterable[str] = ("ai_score", "ai_score_reason", "ai_summary", "ai_detail")) -> int:
    ks = list(keys)
    if entry_ids is None:
        return db.execute(
            f"UPDATE {T_ENTRY} SET attributes = ({_ATTR_OBJ} - %s::text[])::text WHERE attributes LIKE '%%\"ai_score\"%%'",
            (ks,),
        )
    ids = [int(i) for i in entry_ids]
    return db.execute(
        f"UPDATE {T_ENTRY} SET attributes = ({_ATTR_OBJ} - %s::text[])::text WHERE id = ANY(%s)",
        (ks, ids),
    )


# ── Read state ──────────────────────────────────────────────────────────────

def mark_read(entry_ids: Iterable[int | str], read: bool = True) -> int:
    ids = [int(i) for i in entry_ids]
    if not ids:
        return 0
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            f"""
            UPDATE {T_ENTRY} SET is_read = %s, "lastUserModified" = %s
            WHERE id = ANY(%s) AND is_read <> %s
            """,
            (1 if read else 0, now, ids, 1 if read else 0),
        )
        n = cur.rowcount
        # Keep FreshRSS's per-feed unread cache in sync (same as FeedDAO::updateCachedValues)
        c.execute(
            f"""
            UPDATE {T_FEED} f SET
              "cache_nbEntries" = COALESCE(s.n, 0),
              "cache_nbUnreads" = COALESCE(s.u, 0)
            FROM (
              SELECT id_feed, count(*) AS n, count(*) FILTER (WHERE is_read = 0) AS u
              FROM {T_ENTRY} WHERE id_feed IN (SELECT DISTINCT id_feed FROM {T_ENTRY} WHERE id = ANY(%s))
              GROUP BY id_feed
            ) s WHERE s.id_feed = f.id
            """,
            (ids,),
        )
    return n


# ── Rules ───────────────────────────────────────────────────────────────────

def get_rules() -> dict[str, dict[int, dict]]:
    out: dict[str, dict[int, dict]] = {"feed": {}, "category": {}}
    for r in db.fetch_all("SELECT scope, ref_id, score, summarize, fetch_full FROM ai.feed_rules"):
        out[r["scope"]][int(r["ref_id"])] = {"score": r["score"], "summarize": r["summarize"], "fetch_full": r["fetch_full"]}
    return out


def set_rule(scope: str, ref_id: int, *, score: bool, summarize: bool, fetch_full: bool) -> None:
    db.execute(
        """
        INSERT INTO ai.feed_rules (scope, ref_id, score, summarize, fetch_full) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (scope, ref_id) DO UPDATE SET score = EXCLUDED.score, summarize = EXCLUDED.summarize, fetch_full = EXCLUDED.fetch_full
        """,
        (scope, int(ref_id), bool(score), bool(summarize), bool(fetch_full)),
    )


def rule_for(entry_or_feed: Entry | dict, rules: dict | None = None) -> dict:
    """Effective rule for an entry: feed rule OR category rule (any true wins)."""
    rules = rules or get_rules()
    if isinstance(entry_or_feed, Entry):
        fid, cid = entry_or_feed.feed_id, entry_or_feed.category_id
    else:
        fid, cid = int(entry_or_feed.get("id") or 0), int(entry_or_feed.get("category_id") or 0)
    f = rules["feed"].get(fid, {})
    c = rules["category"].get(cid, {})
    return {k: bool(f.get(k) or c.get(k)) for k in ("score", "summarize", "fetch_full")}


def scoring_enabled_feed_ids(rules: dict | None = None) -> list[int]:
    rules = rules or get_rules()
    out = []
    for f in list_feeds():
        if rule_for(f, rules)["score"]:
            out.append(int(f["id"]))
    return out


def entry_url(entry_id: int | str) -> str:
    return f"{settings.freshrss_public_url.rstrip('/')}/i/?a=normal&state=3&search=%23{entry_id}"


def entry_permalink(entry: Entry) -> str:
    return entry.link


# ── Native FreshRSS labels and tags ─────────────────────────────────────────
# Score buckets are written as FreshRSS *labels* (sidebar filters with unread
# counts, visible to mobile clients). Topics are written into the entry `tags`
# column with an `ai/` prefix so `#ai/topic` search works.

T_TAG = f"{settings.freshrss_user}_tag"
T_ENTRYTAG = f"{settings.freshrss_user}_entrytag"

SCORE_LABELS = {"high": "AI: High", "medium": "AI: Medium", "low": "AI: Low"}
TOPIC_PREFIX = "ai/"


def score_bucket(score: int, high_min: int = 7, medium_min: int = 4) -> str:
    if score >= high_min:
        return "high"
    if score >= medium_min:
        return "medium"
    return "low"


def _label_ids() -> dict[str, int]:
    with db.conn() as c:
        for name in SCORE_LABELS.values():
            c.execute(f"INSERT INTO {T_TAG} (name, attributes) VALUES (%s, '') ON CONFLICT (name) DO NOTHING", (name,))
        rows = c.execute(f"SELECT id, name FROM {T_TAG} WHERE name = ANY(%s)", (list(SCORE_LABELS.values()),)).fetchall()
    return {r["name"]: int(r["id"]) for r in rows}


def apply_score_label(entry_id: int | str, score: int, high_min: int = 7, medium_min: int = 4) -> None:
    ids = _label_ids()
    want = ids.get(SCORE_LABELS[score_bucket(int(score), high_min, medium_min)])
    with db.conn() as c:
        c.execute(f"DELETE FROM {T_ENTRYTAG} WHERE id_entry = %s AND id_tag = ANY(%s)", (int(entry_id), list(ids.values())))
        if want:
            c.execute(f"INSERT INTO {T_ENTRYTAG} (id_tag, id_entry) VALUES (%s, %s) ON CONFLICT DO NOTHING", (want, int(entry_id)))


def clear_score_labels() -> int:
    ids = _label_ids()
    return db.execute(f"DELETE FROM {T_ENTRYTAG} WHERE id_tag = ANY(%s)", (list(ids.values()),))


def parse_tags(raw: str | None) -> list[str]:
    """FreshRSS stores tags as '#a #b c #d' (space-separated, each starting with '#')."""
    if not raw or not raw.strip():
        return []
    parts = [p.strip() for p in raw.strip().lstrip("#").split(" #")]
    return [p for p in parts if p]


def format_tags(tags: list[str]) -> str:
    return "" if not tags else "#" + " #".join(tags)


def merge_topic_tags(existing: str | None, topics: list[str]) -> str:
    kept = [t for t in parse_tags(existing) if not t.startswith(TOPIC_PREFIX)]
    slugs = []
    for t in topics:
        s = TOPIC_PREFIX + str(t).strip().lower().replace(" ", "-")
        if s not in slugs and s != TOPIC_PREFIX:
            slugs.append(s)
    return format_tags(kept + slugs)


def set_topic_tags(entry_id: int | str, topics: list[str]) -> None:
    with db.conn() as c:
        row = c.execute(f"SELECT tags FROM {T_ENTRY} WHERE id = %s", (int(entry_id),)).fetchone()
        if row is None:
            return
        c.execute(f"UPDATE {T_ENTRY} SET tags = %s WHERE id = %s", (merge_topic_tags(row["tags"], topics), int(entry_id)))
