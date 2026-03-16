# Newsletter Digest System

Self-hosted newsletter aggregation + AI-powered daily digest email.

## Architecture

```
RSS-native sources ──────────────────────► FreshRSS ◄──── YouTube channel RSS
(Substack, Ghost, custom)                     │
                                              ▼
                                    FreshRSS GReader API
                                              │
                                              ▼
                                    Digest Container (cron)
                                    ├── Haiku: score items
                                    ├── Sonnet: generate digest
                                    └── SMTP: send email
                                              │
                                              ▼
                                    Morning email digest
```

## Services

| Service | Port | Purpose |
|---------|------|---------|
| FreshRSS | 8080 | RSS aggregator + reading UI |
| PostgreSQL | 5432 | FreshRSS database |
| Digest | — | Daily cron: score + generate + email via Claude API |

## Quick Start

```bash
cp .env.example .env
# Edit .env with real credentials
docker compose up -d
```

Then complete FreshRSS setup at http://localhost:8080.

See `DEPLOYMENT_SPEC.md` for the full phased deployment plan including
LXC provisioning, feed setup, and testing steps.

## Digest Script

```bash
# Inside the container, or locally with a venv:
python digest.py --dry-run         # Preview digest without sending
python digest.py --score-only      # Just score items, print results
python digest.py --since 48        # Weekend catchup (48h lookback)
python digest.py                   # Full run: score + generate + send
```

## Feeds

All feeds are listed in `config/feeds.txt`. Categories:
- **Deep Dives** — long-form essays (Zvi, Experimental History, Mindful Modeler)
- **Daily News** — Tangle, Rascal, AWS Weekly, Daily Upside, Sherwood News
- **YouTube** — 48 channels, shown in a separate digest section

## Cost

- Claude API for daily digest (~30-50 items/day): ~$1-3/mo
- Everything else is self-hosted
