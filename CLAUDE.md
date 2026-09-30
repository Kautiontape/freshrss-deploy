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
- Deploy directory inside the LXC: `/home/shawn/freshrss/`
- FreshRSS at `freshrss.yuffie.ts.net:8080`, assistant UI at `freshrss.yuffie.ts.net:8081`

## Deployment

- GitHub Actions workflow on push to `main`
- Runner: self-hosted `[self-hosted, ktn]` on the kautiontape-new DigitalOcean droplet
- Deploy: ktn SSHs into the freshrss LXC (`shawn@freshrss` via Tailscale), runs `git pull`, `docker compose pull`,
  `docker compose up -d --build --remove-orphans`
- The submodule must be pushed before the parent repo for deploys to succeed
- New env vars must be added to the server `.env` before pushing (compose uses `${VAR:-}` defaults for optional ones)

## Repos

- Parent: `Kautiontape/freshrss-deploy` (public)
- Submodule: `Kautiontape/xExtension-AiAssistant`

## Assistant service

- Code in `assistant/app/`: `scoring.py` (worker), `chat.py` (tools + agent loop), `briefs.py` (scheduler + email),
  `freshrss.py` (DB access), `main.py` (API), `prompts.py`; UI in `assistant/static/`
- Models: Sonnet 5 for batch scoring, Opus 5 for summaries/chat/briefs by default; configurable in the UI Settings
- Attribute keys the extension renders: `ai_score`, `ai_score_reason`, `ai_summary`, `ai_summary_full`, `ai_detail`,
  `yt_transcript`, `yt_is_short`, `yt_duration`, `full_content`. Write them with `freshrss.merge_attributes` (atomic JSONB merge).
- Local test loop: restore a `pg_dump` into a local Postgres, set env vars, run `python -m app.scoring N M K`
  or `uvicorn app.main:app`

## Extension Development

- Extension PHP code is in `xExtension-AiAssistant/extension.php`, config UI in `configure.phtml`, browser code in `static/`
- Lint with the container's PHP: `ssh shawn@freshrss 'docker exec -i freshrss php -l' < extension.php`
- Changes to the extension require committing in the submodule first, then updating the submodule pointer in the parent
