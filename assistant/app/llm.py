"""Anthropic client, model catalog, usage/cost logging."""

from __future__ import annotations

import logging
import threading
from typing import Any

import anthropic

from . import db
from .config import settings

log = logging.getLogger(__name__)

# Model catalog: id -> (label, input $/MTok, output $/MTok, cache read $/MTok, cache write $/MTok)
MODELS: dict[str, dict[str, Any]] = {
    "claude-opus-5": {"label": "Claude Opus 5", "in": 5.0, "out": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "claude-sonnet-5": {"label": "Claude Sonnet 5", "in": 2.0, "out": 10.0, "cache_read": 0.2, "cache_write": 2.5},
    "claude-haiku-4-5": {"label": "Claude Haiku 4.5", "in": 1.0, "out": 5.0, "cache_read": 0.1, "cache_write": 1.25},
    "claude-fable-5-1": {"label": "Claude Fable 5.1 (most capable, $10/$50)", "in": 10.0, "out": 50.0, "cache_read": 0.25, "cache_write": 12.5},
}
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_MODEL = "claude-opus-5"

_client: anthropic.Anthropic | None = None
_lock = threading.Lock()


def client() -> anthropic.Anthropic:
    global _client
    with _lock:
        if _client is None:
            if not settings.anthropic_api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            _client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3, timeout=600.0)
        return _client


def valid_model(model: str | None, fallback: str = DEFAULT_MODEL) -> str:
    return model if model in MODELS else fallback


def valid_effort(effort: str | None, fallback: str = "high") -> str:
    return effort if effort in EFFORTS else fallback


def thinking_params(model: str, effort: str, display: str = "omitted") -> dict[str, Any]:
    """Thinking/effort parameters appropriate for the model family."""
    if model == "claude-haiku-4-5":
        # Haiku 4.5: no adaptive thinking, no effort. Keep it simple.
        return {}
    params: dict[str, Any] = {"thinking": {"type": "adaptive", "display": display},
                              "output_config": {"effort": effort}}
    return params


def fallback_params(model: str) -> dict[str, Any]:
    if model == "claude-fable-5-1":
        return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    return {}


def cost_usd(model: str, usage: Any) -> float:
    m = MODELS.get(model)
    if not m or usage is None:
        return 0.0
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (inp * m["in"] + out * m["out"] + cr * m["cache_read"] + cw * m["cache_write"]) / 1_000_000


def log_usage(purpose: str, model: str, usage: Any, ref: str | None = None) -> float:
    if usage is None:
        return 0.0
    cost = cost_usd(model, usage)
    try:
        db.execute(
            """
            INSERT INTO ai.usage_log (purpose, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, ref)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (purpose, model,
             getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0,
             getattr(usage, "cache_read_input_tokens", 0) or 0, getattr(usage, "cache_creation_input_tokens", 0) or 0,
             round(cost, 5), ref),
        )
    except Exception as e:  # never let bookkeeping break the request
        log.warning("usage log failed: %s", e)
    return cost


def text_of(message: Any) -> str:
    return "".join(b.text for b in message.content if getattr(b, "type", "") == "text")


def usage_summary(days: int = 30) -> dict[str, Any]:
    rows = db.fetch_all(
        """
        SELECT purpose, model, count(*) AS calls, sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
               sum(cache_read_tokens) AS cache_read_tokens, sum(cost_usd) AS cost_usd
        FROM ai.usage_log WHERE ts >= now() - (%s || ' days')::interval
        GROUP BY purpose, model ORDER BY cost_usd DESC
        """,
        (str(days),),
    )
    total = sum(float(r["cost_usd"] or 0) for r in rows)
    today = db.fetch_one("SELECT COALESCE(sum(cost_usd), 0) AS c FROM ai.usage_log WHERE ts >= date_trunc('day', now())")
    return {"days": days, "rows": [dict(r, cost_usd=float(r["cost_usd"] or 0)) for r in rows],
            "total_usd": round(total, 4), "today_usd": round(float(today["c"] if today else 0), 4)}
