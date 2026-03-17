#!/usr/bin/env python3
"""
Daily Newsletter Digest Generator

Pulls scored articles from the AiAssistant extension,
generates a formatted digest using Claude Sonnet,
and emails the result.

Usage:
    python digest.py              # Run full pipeline, send email
    python digest.py --dry-run    # Print digest to stdout, don't send
    python digest.py --since 24   # Look back 24 hours (default)
    python digest.py --since 48   # Look back 48 hours (weekend catchup)
"""

import argparse
import json
import os
import re
import smtplib
import sys
from datetime import datetime
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

    def get_extension_data(self, action: str, params: dict | None = None) -> dict:
        """Call an AiAssistant extension AJAX endpoint."""
        url = f"{self.base_url}/"
        query = {
            "c": "extension",
            "a": "configure",
            "e": "AiAssistant",
        }
        post_data = {"ajax_action": action}
        if params:
            post_data.update(params)

        # The extension endpoints need a valid FreshRSS session, not just GReader auth.
        # Use cookie-based auth by logging in via the web UI.
        resp = self.session.post(url, params=query, data=post_data)
        resp.raise_for_status()
        return resp.json()

    def get_scored_entries(self, since_hours: int = 24) -> list[dict]:
        """Get scored entries from the extension."""
        data = self.get_extension_data("get_scored_entries", {"since": str(since_hours)})
        return data.get("entries", [])

    def get_profile(self) -> str:
        """Get the interest profile from the extension."""
        data = self.get_extension_data("get_profile")
        return data.get("profile", "")


# ── Digest Generation ───────────────────────────────────────────────────────

def generate_digest(
    client: anthropic.Anthropic, items: list[dict], profile: str, top_n: int
) -> tuple[str, str]:
    """
    Generate an HTML digest email from scored items.
    Returns (subject, html_body).
    """
    today = datetime.now().strftime("%A, %B %-d, %Y")

    # Separate articles from YouTube videos
    articles = [i for i in items[:top_n] if not any(
        ind in (i.get("url") or "") for ind in YOUTUBE_INDICATORS
    )]
    videos = [i for i in items if any(
        ind in (i.get("url") or "") for ind in YOUTUBE_INDICATORS
    ) and i.get("score", 0) >= 5][:10]

    articles_text = ""
    for item in articles:
        articles_text += f"""
---
Title: {item['title']}
Source: {item.get('source', 'Unknown')}
URL: {item.get('url', '')}
Relevance Score: {item.get('score', '?')}/10
Score Reason: {item.get('reason', '')}
Summary: {item.get('summary', '')}
"""

    videos_text = ""
    for item in videos:
        videos_text += f"""
---
Title: {item['title']}
Channel: {item.get('source', 'Unknown')}
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
4. For dense analytical essays, note the key thesis and whether I should read the full thing
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
        text = re.sub(r"<[^>]+>", " ", html_body)
        text = re.sub(r"\s+", " ", text)
        print(text[:3000])
        print(f"\n[... full HTML is {len(html_body)} chars]")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = TO_EMAIL

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
    args = parser.parse_args()

    print(f"Fetching scored entries from last {args.since} hours...")
    rss = FreshRSSClient(FRESHRSS_API_URL, FRESHRSS_API_USER, FRESHRSS_API_PASSWORD)

    items = rss.get_scored_entries(since_hours=args.since)
    print(f"Found {len(items)} scored entries")

    if not items:
        print("No scored entries. Skipping digest.")
        return

    items.sort(key=lambda x: x.get("score", 0), reverse=True)

    high_relevance = [i for i in items if i.get("score", 0) >= 7]
    print(f"  {len(high_relevance)} high-relevance items (score >= 7)")
    if items:
        print(f"  Top item: {items[0]['title']} (score: {items[0].get('score', '?')})")

    print("Fetching interest profile...")
    profile = rss.get_profile()
    if not profile:
        print("Warning: no interest profile set in extension settings", file=sys.stderr)

    print("Generating digest...")
    ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    subject, html = generate_digest(ai, items, profile, args.top)

    send_email(subject, html, dry_run=args.dry_run)

    total_items = len(items)
    included = min(args.top, len(items))
    skipped = total_items - included
    print(f"\nDigest: {included} articles included, {skipped} skipped, {len(high_relevance)} high-relevance")


if __name__ == "__main__":
    main()
