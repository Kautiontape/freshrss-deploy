"""Postgres access: connection pool, schema migration for the `ai` schema, settings store."""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .config import settings

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def pool() -> ConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(
                settings.dsn,
                min_size=1,
                max_size=12,
                kwargs={"row_factory": dict_row, "autocommit": True},
                open=True,
            )
        return _pool


@contextmanager
def conn() -> Iterator[psycopg.Connection]:
    with pool().connection() as c:
        yield c


def fetch_all(sql: str, params: Any = None) -> list[dict]:
    with conn() as c:
        return c.execute(sql, params).fetchall()


def fetch_one(sql: str, params: Any = None) -> dict | None:
    with conn() as c:
        return c.execute(sql, params).fetchone()


def execute(sql: str, params: Any = None) -> int:
    with conn() as c:
        cur = c.execute(sql, params)
        return cur.rowcount


SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS ai;

CREATE TABLE IF NOT EXISTS ai.settings (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai.feed_rules (
    scope TEXT NOT NULL CHECK (scope IN ('feed', 'category')),
    ref_id INTEGER NOT NULL,
    score BOOLEAN NOT NULL DEFAULT false,
    summarize BOOLEAN NOT NULL DEFAULT false,
    fetch_full BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (scope, ref_id)
);

CREATE TABLE IF NOT EXISTS ai.entry_state (
    entry_id BIGINT PRIMARY KEY,
    scored_at TIMESTAMPTZ,
    summarized_at TIMESTAMPTZ,
    enriched_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai.usage_log (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd NUMERIC(10, 5) NOT NULL DEFAULT 0,
    ref TEXT
);
CREATE INDEX IF NOT EXISTS usage_log_ts_idx ON ai.usage_log (ts);

CREATE TABLE IF NOT EXISTS ai.briefs (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true,
    schedule TEXT NOT NULL DEFAULT '30 6 * * *',
    feed_ids INTEGER[] NOT NULL DEFAULT '{}',
    category_ids INTEGER[] NOT NULL DEFAULT '{}',
    lookback_hours INTEGER NOT NULL DEFAULT 24,
    unread_only BOOLEAN NOT NULL DEFAULT false,
    min_score INTEGER NOT NULL DEFAULT 0,
    instructions TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    effort TEXT NOT NULL DEFAULT 'high',
    send_email BOOLEAN NOT NULL DEFAULT false,
    last_run_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai.brief_runs (
    id SERIAL PRIMARY KEY,
    brief_id INTEGER NOT NULL REFERENCES ai.briefs(id) ON DELETE CASCADE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running',
    period_start TIMESTAMPTZ,
    period_end TIMESTAMPTZ,
    entry_ids BIGINT[] NOT NULL DEFAULT '{}',
    content_md TEXT,
    error TEXT,
    usage JSONB
);
CREATE INDEX IF NOT EXISTS brief_runs_brief_idx ON ai.brief_runs (brief_id, started_at DESC);

CREATE TABLE IF NOT EXISTS ai.chats (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New chat',
    context_type TEXT NOT NULL DEFAULT 'general',
    context_id TEXT,
    model TEXT NOT NULL DEFAULT '',
    effort TEXT NOT NULL DEFAULT 'high',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chats_context_idx ON ai.chats (context_type, context_id);

CREATE TABLE IF NOT EXISTS ai.chat_messages (
    id BIGSERIAL PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES ai.chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content JSONB NOT NULL,
    model TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_messages_chat_idx ON ai.chat_messages (chat_id, id);

CREATE TABLE IF NOT EXISTS ai.article_outlines (
    entry_id BIGINT PRIMARY KEY,
    digest TEXT NOT NULL,
    outline TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def migrate() -> None:
    with conn() as c:
        c.execute(SCHEMA_SQL)
    log.info("ai schema ready")


# ── Settings store ──────────────────────────────────────────────────────────

DEFAULT_SETTINGS: dict[str, Any] = {
    "interest_profile": "",
    "scoring_model": "claude-sonnet-5-5",
    "scoring_effort": "low",
    "summary_model": "claude-opus-5-5",
    "summary_effort": "low",
    "chat_model": "claude-opus-5-5",
    "chat_effort": "high",
    "brief_model": "claude-opus-5-5",
    "brief_effort": "high",
    "summary_threshold": 7,
    "score_lookback_days": 180,
    "summary_lookback_days": 45,
    "enrich_lookback_days": 30,
    "mark_shorts_read": True,
    "scoring_paused": False,
    "write_labels": True,
    "label_high_min": 7,
    "label_medium_min": 4,
    "write_topic_tags": True,
    "topic_tags": ["ai-enterprise", "aws", "homelab", "ai-policy", "ai-research", "essay", "photography",
                   "tech-news", "gamedev", "mtg", "ttrpg", "linux", "parenting", "finance", "politics",
                   "science", "comedy", "entertainment", "product-update"],
}


def get_setting(key: str, default: Any = None) -> Any:
    row = fetch_one("SELECT value FROM ai.settings WHERE key = %s", (key,))
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return row["value"]


def get_all_settings() -> dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    for row in fetch_all("SELECT key, value FROM ai.settings"):
        out[row["key"]] = row["value"]
    return out


def set_setting(key: str, value: Any) -> None:
    execute(
        """
        INSERT INTO ai.settings (key, value, updated_at) VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (key, Jsonb(value)),
    )


def set_settings(values: dict[str, Any]) -> None:
    for k, v in values.items():
        set_setting(k, v)


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
