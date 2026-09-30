# FreshRSS + AI Assistant

Self-hosted FreshRSS with an AI layer on top: relevance scoring, summaries, scheduled
briefs, and a chat interface over everything in the reader.

## Architecture

```
Feeds ──► FreshRSS (1.30, Postgres) ◄──────────── xExtension-AiAssistant (thin display + proxy)
                 │  shared database                            │ HTTP (internal token)
                 ▼                                             ▼
          assistant service (Python / FastAPI / Anthropic SDK)
          ├── scoring worker: scores + gists every entry in enabled feeds, full
          │   summaries for high-value ones, Shorts filtering, transcripts
          ├── writes FreshRSS labels (AI: High/Medium/Low) and #ai/topic tags
          ├── briefs: scheduled markdown briefings per feed set (optional email)
          └── chat UI (:8081): tools over the reader (search, read, mark read,
              edit interests, web search); "catch me up" flows
          youtube-helper (yt-dlp + transcripts)
```

| Service | Port | Purpose |
|---------|------|---------|
| freshrss | 8080 | Reader UI, feed refresh (cron every 15 min) |
| postgres | – | FreshRSS database; the assistant adds an `ai` schema |
| assistant | 8081 | Scoring worker, briefs scheduler, chat UI + API |
| youtube-helper | – | Shorts detection + transcripts for YouTube entries |

## Quick start

```bash
cp .env.example .env    # fill in POSTGRES_PASSWORD, ANTHROPIC_API_KEY, ASSISTANT_PASSWORD, ASSISTANT_INTERNAL_TOKEN
docker compose up -d --build
```

Then open FreshRSS on :8080 and enable the *AI Assistant* extension. A **✨ Assistant** button appears
in the top bar: it opens the assistant UI inside FreshRSS (signed in automatically). The UI is also
reachable directly on :8081 with `ASSISTANT_PASSWORD`.
The assistant seeds its interest profile from `config/interest-profile.md` and default
feed rules on first run; change both in the assistant's Settings page afterwards.

## How scoring works

- Every 2 minutes the worker looks for entries in scoring-enabled feeds/categories that
  have no score yet (unread entries, or anything newer than `score_lookback_days`).
- Entries are scored in batches of 10 with structured output (score 1-10, reason,
  one-line gist, topic tags) by Sonnet 5.5; summaries, briefs and chat use Opus 5.5 by default.
  Models and effort are per-purpose settings in the UI; spend is logged per call. Results are stored in the entry `attributes` JSON, which
  the extension renders as a badge + summary in the reader.
- Entries scoring at or above the summary threshold (or in feeds flagged *Summarize*)
  get a full summary from the whole text (fetched article / transcript).
- YouTube Shorts are scored 0 and marked read; transcripts are fetched for recent videos.
- Labels `AI: High / Medium / Low` appear in the FreshRSS sidebar, and `#ai/<topic>`
  tags are searchable, so you can filter without the assistant UI.

## Briefs

A brief is a scheduled briefing over a set of feeds or categories (for example a daily
Zvi brief at 6:30). Each run covers everything since the previous run, is stored in the
assistant, can be emailed, and can be opened as a chat ("Chat about this brief").

## YouTube feeds

YouTube's feed endpoint answers sporadic 404s when many channel feeds are fetched in a burst, and
FreshRSS flags a feed as failing after a single miss. The YouTube feeds are therefore given
staggered per-feed refresh intervals (2 to 3.75 hours) so they are not all fetched at once. If a
YouTube feed shows the error marker, check the FreshRSS log for the status code before assuming
the channel id is wrong.

## Deployment

- Push to `main` triggers `.github/workflows/deploy.yml` on the self-hosted `ktn` runner,
  which SSHes into the freshrss LXC and runs `git pull`, `docker compose pull`, and
  `docker compose up -d --build --remove-orphans`.
- The extension is a git submodule (`xExtension-AiAssistant`); push it before the parent.

## Local development

```bash
cd assistant
uv venv --python 3.12 .venv && uv pip install -p .venv/bin/python -r requirements.txt
# point at a Postgres with a FreshRSS dump, set ANTHROPIC_API_KEY etc.
.venv/bin/uvicorn app.main:app --reload --port 8081
.venv/bin/python -m app.scoring 20 2 0     # one worker cycle: score 20, summarize 2, enrich 0
```
