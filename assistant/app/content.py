"""Content extraction: HTML -> text, full-article fetch, YouTube helper client."""

from __future__ import annotations

import html
import logging
import re
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)

_BLOCK_END = re.compile(r"</(?:p|div|tr|h[1-6]|li|blockquote|figcaption|section|article)>", re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_CELL_END = re.compile(r"</(?:td|th)>", re.I)
_SCRIPT = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_AI_CONTAINER = re.compile(r'<div class="ai-assistant-container".*?</div>', re.S)
_DETAILS = re.compile(r"<details class=\"ai-(?:transcript|fullcontent)-section\">.*?</details>", re.S)
_HEADING_TAG = re.compile(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", re.I | re.S)
_BOLD_PARA = re.compile(r"<p\b[^>]*>\s*<(strong|b)\b[^>]*>((?:(?!</?(?:strong|b)\b).)*)</\1>\s*</p>", re.I | re.S)


def _heading_line(inner: str) -> str:
    text = re.sub(r"\s+", " ", html.unescape(_TAG.sub("", inner))).strip()
    return f"\n## {text}\n" if 0 < len(text) <= 150 else f"\n{inner}\n"


def html_to_text(raw: str, headings: bool = False) -> str:
    """Plain text from entry HTML. With headings=True, h1-h6 and bold-only paragraphs become '## ' lines."""
    if not raw:
        return ""
    s = _AI_CONTAINER.sub("", raw)
    s = _DETAILS.sub("", s)
    s = _SCRIPT.sub("", s)
    if headings:
        s = _HEADING_TAG.sub(lambda m: _heading_line(m.group(1)), s)
        s = _BOLD_PARA.sub(lambda m: _heading_line(m.group(2)), s)
    s = _BLOCK_END.sub("\n", s)
    s = _BR.sub("\n", s)
    s = _CELL_END.sub("  ", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"[^\S\n]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + " […]"


# ── Full article fetch ──────────────────────────────────────────────────────

def fetch_full_article(url: str, timeout: float = 15.0) -> str | None:
    """Fetch the article at `url` and extract the main text with trafilatura."""
    if not url:
        return None
    try:
        import trafilatura
    except ImportError:  # pragma: no cover
        return None
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; FreshRSS-Assistant/1.0)"}) as c:
            r = c.get(url)
            if r.status_code != 200 or not r.text:
                return None
            text = trafilatura.extract(r.text, url=url, include_comments=False, include_tables=True,
                                       favor_recall=True)
            return text.strip() if text else None
    except Exception as e:  # network errors, parse errors
        log.info("full article fetch failed for %s: %s", url, e)
        return None


# ── YouTube helper ──────────────────────────────────────────────────────────

def youtube_info(video_id: str, timeout: float = 40.0) -> dict[str, Any] | None:
    """Ask the youtube-helper container for duration / short / transcript."""
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{settings.youtube_helper_url}/video-info", params={"v": video_id})
            if r.status_code != 200:
                return None
            data = r.json()
            return data if isinstance(data, dict) else None
    except Exception as e:
        log.info("youtube-helper failed for %s: %s", video_id, e)
        return None
