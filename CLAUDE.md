# FreshRSS Deploy

## Architecture

- **FreshRSS** (pinned image `freshrss/freshrss:1.30.0`) runs in Docker inside **LXC 112** (`freshrss`) on **Yuffie** (Proxmox host)
- Docker Compose runs four services: `freshrss`, `postgres`, `youtube-helper`, and `assistant`
- **assistant/** is a Python (FastAPI + Anthropic SDK) service that owns all AI work: background scoring worker,
  full summaries, scheduled briefs, and the chat UI. It talks to the FreshRSS Postgres database directly
  (entry `attributes` JSON, native labels/tags) and keeps its own tables in the `ai` schema.
- The AI Assistant extension is a git submodule (`xExtension-AiAssistant`) bind-mounted into the FreshRSS container.
  It is a thin display layer: renders scores/summaries from entry attributes, preserves them on entry updates
  (`entry_before_update` hook), and proxies on-demand actions (summarize/detail/chat/feedback) to the assistant
  service using `ASSISTANT_URL` + `ASSISTANT_INTERNAL_TOKEN` (env vars on the freshrss container).
- Deploy directory inside the LXC: `/etc/komodo/stacks/freshrss/` (Komodo's clone, root-owned). Secrets:
  `/opt/freshrss/.env` (root, 0600)
- FreshRSS at `freshrss.yuffie.ts.net:8080`, assistant UI at `freshrss.yuffie.ts.net:8081`. The extension also
  registers a FreshRSS page (`?c=assistant`, "✨ Assistant" nav button) that embeds the UI in an iframe and signs
  in via `/sso` (HMAC over a timestamp with `ASSISTANT_INTERNAL_TOKEN`); this needs the same hostname for both.

## Deployment

- Komodo deploys it: stack `freshrss` in `shawnsquire/ansible-fleet` (`komodo/resources.toml`)
- Images are built on GitHub, never on the LXC (8 GB disk). `build-publish.yml` runs on every push to main except
  `*.md`-only ones, publishes `ghcr.io/kautiontape/freshrss-{assistant,youtube-helper}:sha-<commit>`, then commits
  the new tags to `docker-compose.yml` as github-actions[bot]. Pull before pushing again
- Komodo's 15-minute poll redeploys only when `docker-compose.yml` changes, so the bot's pin commit is what deploys
  app code, `config/` and submodule bumps
- A deploy runs `git pull`, `git submodule update --init --recursive`, `docker compose pull`, `up -d`
- `docker-compose.override.yml` holds the `build:` sections for local runs; Komodo passes `-f docker-compose.yml`
- The submodule must be pushed before the parent repo for deploys to succeed
- New env vars must be added to `/opt/freshrss/.env` before deploying (compose uses `${VAR:-}` defaults for optional ones)

## Repos

- Parent: `Kautiontape/freshrss-deploy` (public)
- Submodule: `Kautiontape/xExtension-AiAssistant`

## Assistant service

- Code in `assistant/app/`: `scoring.py` (worker), `chat.py` (tools + agent loop), `briefs.py` (scheduler + email),
  `freshrss.py` (DB access), `main.py` (API), `prompts.py`; UI in `assistant/static/`
- Models: Sonnet 5.5 for batch scoring, Opus 5.5 for summaries/chat/briefs by default (catalog + pricing in `llm.py`);
  configurable in the UI Settings. Check `client.models.list()` before adding a model id.
- Attribute keys the extension renders: `ai_score`, `ai_score_reason`, `ai_summary`, `ai_summary_full`, `ai_detail`,
  `yt_transcript`, `yt_is_short`, `yt_duration`, `full_content`. Write them with `freshrss.merge_attributes` (atomic JSONB merge).
- Local test loop: restore a `pg_dump` into a local Postgres, set env vars, run `python -m app.scoring N M K`
  or `uvicorn app.main:app`

## Extension Development

- Extension PHP code is in `xExtension-AiAssistant/extension.php`, config UI in `configure.phtml`, browser code in `static/`
- Lint with the container's PHP: `ssh shawn@freshrss 'docker exec -i freshrss php -l' < extension.php`
- Full local stack for UI testing: `.env` with test values (gitignored), `docker compose up -d postgres`, restore a
  `pg_dump` of the live DB, `docker compose up -d --build`, then `cli/do-install.php` + `cli/create-user.php` inside the
  freshrss container and set `db.prefix` to '' in `data/config.php` (the live tables are unprefixed `shawn_*`).
- YouTube feed 404s are transient rate limiting, not bad channel ids; per-feed `ttl` on the YouTube category is staggered.
- Changes to the extension require committing in the submodule first, then updating the submodule pointer in the parent
