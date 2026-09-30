"""First-run seeding: interest profile from config/interest-profile.md, default feed rules."""

from __future__ import annotations

import logging
from pathlib import Path

from . import db, freshrss

log = logging.getLogger(__name__)

PROFILE_PATHS = [Path("/app/config/interest-profile.md"), Path(__file__).resolve().parents[2] / "config" / "interest-profile.md"]

# Applied by category *name* the first time the service runs with no rules at all.
# Mirrors the rules that were configured in the old extension.
DEFAULT_CATEGORY_RULES = {
    "Deep Dives": {"score": True, "summarize": True, "fetch_full": False},
    "Daily News": {"score": True, "summarize": False, "fetch_full": False},
    "Product News": {"score": True, "summarize": True, "fetch_full": False},
}


def seed_profile() -> bool:
    if (db.get_setting("interest_profile") or "").strip():
        return False
    for p in PROFILE_PATHS:
        if p.exists():
            text = p.read_text(encoding="utf-8").strip()
            if text:
                db.set_setting("interest_profile", text)
                log.info("seeded interest profile from %s", p)
                return True
    return False


def seed_rules() -> bool:
    if db.fetch_one("SELECT 1 FROM ai.feed_rules LIMIT 1"):
        return False
    cats = {c["name"]: int(c["id"]) for c in freshrss.list_categories()}
    n = 0
    for name, rule in DEFAULT_CATEGORY_RULES.items():
        if name in cats:
            freshrss.set_rule("category", cats[name], **rule)
            n += 1
    if n:
        log.info("seeded %d default category rules", n)
    return n > 0


def run() -> None:
    seed_profile()
    seed_rules()
