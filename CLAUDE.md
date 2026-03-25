# FreshRSS Deploy

## Architecture

- **FreshRSS** runs in a Docker container inside **LXC 112** (`freshrss`) on **Yuffie** (Proxmox host)
- Docker Compose runs three services: `freshrss`, `postgres`, and `digest`
- The AI Assistant extension is a git submodule (`xExtension-AiAssistant`) bind-mounted into the container
- Deploy directory inside the LXC: `/home/shawn/freshrss/`
- Accessible at `freshrss.yuffie.ts.net:8080`

## Deployment

- GitHub Actions workflow on push to `main`
- Runner: self-hosted `[self-hosted, ktn]` on the kautiontape-new DigitalOcean droplet
- Deploy: ktn SSHs into the freshrss LXC (`shawn@freshrss` via Tailscale), runs `git pull` + `docker compose up -d`
- The submodule must be pushed before the parent repo for deploys to succeed

## Repos

- Parent: `Kautiontape/freshrss-deploy` (public)
- Submodule: `Kautiontape/xExtension-AiAssistant`

## Extension Development

- Extension PHP code is in `xExtension-AiAssistant/extension.php`
- Config UI is in `xExtension-AiAssistant/configure.phtml`
- Changes to the extension require committing in the submodule first, then updating the submodule pointer in the parent
- The extension uses the Claude API (Anthropic) for scoring and summarization
