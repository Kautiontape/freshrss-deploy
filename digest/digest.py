#!/usr/bin/env python3
"""
Daily Newsletter Digest Generator

Pulls unread items from FreshRSS via Google Reader API,
scores them for relevance using a cheap model (Haiku),
generates a formatted digest using a smarter model (Sonnet),
and emails the result.

Usage:
    python digest.py              # Run full pipeline, send email
    python digest.py --dry-run    # Print digest to stdout, don't send
    python digest.py --since 24   # Look back 24 hours (default)
    python digest.py --since 48   # Look back 48 hours (weekend catchup)
    python digest.py --score-only # Score items and print results, skip digest
"""

import argparse
import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

# Load .env from parent directory (local dev) or current env (Docker)
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)


# ── Config ──────────────────────────────────────────────────────────────────

FRESHRSS_API_URL = os.environ["FRESHRSS_API_URL"]
FRESHRSS_API_USER = os.environ["FRESHRSS_API_USER"]
FRESHRSS_API_PASSWORD = os.environ["FRESHRSS_API_PASSWORD"]

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

SMTP_HOST = os.environ.get("DIGEST_SMTP_HOST", "smtp.fastmail.com")
SMTP_PORT = int(os.environ.get("DIGEST_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("DIGEST_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("DIGEST_SMTP_PASSWORD", "")
TO_EMAIL = os.environ.get("DIGEST_TO_EMAIL", "")

# Interest profile: check mounted config volume, then fallback to relative path
INTEREST_PROFILE_PATH = Path("/app/config/interest-profile.md")
if not INTEREST_PROFILE_PATH.exists():
    INTEREST_PROFILE_PATH = Path(__file__).parent.parent / "config" / "interest-profile.md"

# Models
SCORING_MODEL = "claude-haiku-4-5-20251001"
DIGEST_MODEL = "claude-sonnet-4-20250514"

DIGEST_TOP_N = 20

# YouTube category detection
YOUTUBE_INDICATORS = {"youtube.com", "youtu.be"}


# ── FreshRSS API Client ────────────────────────────────────────────────────

class FreshRSSClient:
    """Minimal Google Reader API client for FreshRSS."""

    def __init__(self, base_url: str, user: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.auth_token = self._login(user, password)
        self.session.headers["Authorization"] = f"GoogleLogin auth={self.auth_token}"

    def _login(self, user: str, password: str) -> str:
        resp = self.session.post(
            f"{self.base_url}/accounts/ClientLogin",
            data={"Email": user, "Passwd": password},
        )
        resp.raise_for_status()
        for line in resp.text.strip().split("\n"):
            if line.startswith("Auth="):
                return line.split("=", 1)[1]
        raise ValueError("No Auth token in FreshRSS login response")

    def get_unread_items(self, since_hours: int = 24, count: int = 500) -> list[dict]:
        """Fetch unread items from the last N hours."""
        since_ts = int((datetime.now(timezone.utc) - timedelta(hours=since_hours)).timestamp())
        items = []
        continuation = None

        while True:
            params = {
                "output": "json",
                "n": min(count - len(items), 100),
                "ot": since_ts,
                "s": "user/-/state/com.google/reading-list",
                "xt": "user/-/state/com.google/read",
            }
            if continuation:
                params["c"] = continuation

            resp = self.session.get(
                f"{self.base_url}/reader/api/0/stream/contents/reading-list",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("items", []):
                url = ""
                for alt in item.get("alternate", []):
                    if alt.get("type") == "text/html":
                        url = alt["href"]
                        break
                if not url and item.get("canonical"):
                    url = item["canonical"][0].get("href", "")

                categories = [
                    c.get("label", "")
                    for c in item.get("categories", [])
                    if c.get("label")
                ]

                is_youtube = any(
                    ind in url for ind in YOUTUBE_INDICATORS
                ) or "YouTube" in categories

                items.append({
                    "id": item.get("id", ""),
                    "title": item.get("title", "Untitled"),
                    "author": item.get("author", "Unknown"),
                    "source": item.get("origin", {}).get("title", "Unknown"),
                    "url": url,
                    "published": datetime.fromtimestamp(
                        item.get("published", 0), tz=timezone.utc
                    ).isoformat(),
                    "summary": _extract_text(
                        item.get("summary", {}).get("content", "")
                    )[:1000],
                    "categories": categories,
                    "is_youtube": is_youtube,
                })

            continuation = data.get("continuation")
            if not continuation or len(items) >= count:
                break

        return items


def _extract_text(html: str) -> str:
    """Crude HTML to text. Good enough for scoring prompts."""
    import re
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ── AI Scoring ──────────────────────────────────────────────────────────────

def score_items(client: anthropic.Anthropic, items: list[dict], profile: str) -> list[dict]:
    """
    Score items for relevance using Haiku. Returns items with 'score' and 'reason' added.
    Batches items to minimize API calls.
    """
    if not items:
        return []

    items_for_prompt = []
    for i, item in enumerate(items):
        items_for_prompt.append({
            "idx": i,
            "title": item["title"],
            "source": item["source"],
            "summary": item["summary"][:300],
            "is_youtube": item.get("is_youtube", False),
        })

    batch_size = 25
    scored_items = list(items)

    for batch_start in range(0, len(items_for_prompt), batch_size):
        batch = items_for_prompt[batch_start : batch_start + batch_size]

        prompt = f"""Score these articles for relevance based on the interest profile below.

<interest_profile>
{profile}
</interest_profile>

<articles>
{json.dumps(batch, indent=2)}
</articles>

For each article, return a JSON array of objects with:
- "idx": the article index
- "score": 1-10 relevance score (10 = must read, 1 = irrelevant)
- "reason": one sentence explaining the score

For YouTube videos, score based on whether the channel/topic aligns with the interest profile.
YouTube videos should generally score slightly lower than articles unless the topic is highly relevant.

Return ONLY the JSON array, no markdown fences, no other text."""

        resp = client.messages.create(
            model=SCORING_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )

        try:
            scores = json.loads(resp.content[0].text)
            for s in scores:
                idx = s["idx"]
                abs_idx = batch_start + idx
                if abs_idx < len(scored_items):
                    scored_items[abs_idx]["score"] = s.get("score", 5)
                    scored_items[abs_idx]["reason"] = s.get("reason", "")
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"Warning: failed to parse scoring response for batch starting at {batch_start}: {e}", file=sys.stderr)
            for i in range(batch_start, min(batch_start + batch_size, len(scored_items))):
                scored_items[i].setdefault("score", 5)
                scored_items[i].setdefault("reason", "scoring failed")

    scored_items.sort(key=lambda x: x.get("score", 0), reverse=True)
    return scored_items


# ── Digest Generation ───────────────────────────────────────────────────────

def generate_digest(
    client: anthropic.Anthropic, items: list[dict], profile: str, top_n: int
) -> tuple[str, str]:
    """
    Generate an HTML digest email from top-scored items.
    Returns (subject, html_body).
    """
    today = datetime.now().strftime("%A, %B %-d, %Y")

    # Separate articles from YouTube videos
    articles = [i for i in items[:top_n] if not i.get("is_youtube")]
    videos = [i for i in items if i.get("is_youtube") and i.get("score", 0) >= 5][:10]

    articles_text = ""
    for item in articles:
        articles_text += f"""
---
Title: {item['title']}
Source: {item['source']}
Author: {item.get('author', 'Unknown')}
URL: {item.get('url', '')}
Relevance Score: {item.get('score', '?')}/10
Score Reason: {item.get('reason', '')}
Summary: {item.get('summary', '')[:500]}
Categories: {', '.join(item.get('categories', []))}
"""

    videos_text = ""
    for item in videos:
        videos_text += f"""
---
Title: {item['title']}
Channel: {item['source']}
URL: {item.get('url', '')}
Score: {item.get('score', '?')}/10
"""

    prompt = f"""Generate a daily newsletter digest email for {today}.

<interest_profile>
{profile}
</interest_profile>

<scored_articles>
{articles_text}
</scored_articles>

<new_videos>
{videos_text}
</new_videos>

Write an HTML email that:
1. Opens with a brief 1-2 sentence overview of today's highlights
2. Lists the top 3-5 most relevant articles with:
   - Bold title as a clickable link
   - 1-2 sentence summary explaining WHY this matters to me specifically
3. Groups remaining articles by theme (not by source) with shorter descriptions
4. For dense analytical essays (Zvi, Mastroianni-style), note the key thesis
   and whether I should read the full thing
5. Has a separate "New Videos" section at the end listing relevant YouTube uploads
   with channel name and a one-line note on why it might be interesting
6. Ends with a count of lower-priority items skipped

Style: clean, minimal HTML. No heavy CSS frameworks. Dark-mode friendly
(use color: inherit where possible). Inline styles only. Readable on mobile.
Keep the email scannable in under 5 minutes.

Return ONLY the HTML, starting with <html>. No markdown fences."""

    resp = client.messages.create(
        model=DIGEST_MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )

    html = resp.content[0].text.strip()
    subject = f"Daily Digest \u2014 {today}"

    return subject, html


# ── Email Sending ───────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str, dry_run: bool = False):
    """Send the digest email via SMTP."""
    if dry_run:
        print(f"\n{'='*60}")
        print(f"SUBJECT: {subject}")
        print(f"TO: {TO_EMAIL}")
        print(f"{'='*60}\n")
        import re
        text = re.sub(r"<[^>]+>", " ", html_body)
        text = re.sub(r"\s+", " ", text)
        print(text[:3000])
        print(f"\n[... full HTML is {len(html_body)} chars]")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = TO_EMAIL

    import re
    plain = re.sub(r"<[^>]+>", " ", html_body)
    plain = re.sub(r"\s+", " ", plain).strip()

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, TO_EMAIL, msg.as_string())

    print(f"Digest sent to {TO_EMAIL}")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate daily newsletter digest")
    parser.add_argument("--dry-run", action="store_true", help="Print digest, don't send email")
    parser.add_argument("--since", type=int, default=24, help="Look back N hours (default: 24)")
    parser.add_argument("--top", type=int, default=DIGEST_TOP_N, help=f"Include top N items (default: {DIGEST_TOP_N})")
    parser.add_argument("--score-only", action="store_true", help="Score items and print results, skip digest generation")
    args = parser.parse_args()

    profile = INTEREST_PROFILE_PATH.read_text()

    print(f"Fetching unread items from last {args.since} hours...")
    rss = FreshRSSClient(FRESHRSS_API_URL, FRESHRSS_API_USER, FRESHRSS_API_PASSWORD)
    items = rss.get_unread_items(since_hours=args.since)
    print(f"Found {len(items)} unread items")

    articles = [i for i in items if not i.get("is_youtube")]
    videos = [i for i in items if i.get("is_youtube")]
    print(f"  {len(articles)} articles, {len(videos)} YouTube videos")

    if not items:
        print("No unread items. Skipping digest.")
        return

    print("Scoring items for relevance...")
    ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    scored = score_items(ai, items, profile)

    high_relevance = [i for i in scored if i.get("score", 0) >= 7]
    print(f"  {len(high_relevance)} high-relevance items (score >= 7)")
    if scored:
        print(f"  Top item: {scored[0]['title']} (score: {scored[0].get('score', '?')})")

    if args.score_only:
        print(f"\n{'='*60}")
        print("SCORED ITEMS (top 30)")
        print(f"{'='*60}")
        for item in scored[:30]:
            yt = " [YT]" if item.get("is_youtube") else ""
            print(f"  [{item.get('score', '?'):>2}] {item['title'][:70]}{yt}")
            print(f"       {item.get('reason', '')}")
        return

    print("Generating digest...")
    subject, html = generate_digest(ai, scored, profile, args.top)

    send_email(subject, html, dry_run=args.dry_run)

    total_items = len(items)
    included = min(args.top, len([i for i in scored if not i.get("is_youtube")]))
    skipped = total_items - included
    print(f"\nDigest: {included} articles + videos section, {skipped} items skipped, {len(high_relevance)} high-relevance")


if __name__ == "__main__":
    main()
