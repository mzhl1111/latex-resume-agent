# Run on a Mac, expose through Cloudflare Tunnel

This runs the resume agent on your own Mac and lets remote agents (for example Meta's Muse, Claude,
or anything that speaks MCP or A2A) reach it at `https://<your-hostname>`. You don't open any ports.
`cloudflared` makes an outbound connection to Cloudflare, and Cloudflare forwards only the agent
endpoints to `127.0.0.1`.

```text
 remote agent ──HTTPS──▶ Cloudflare edge ──tunnel──▶ cloudflared (this Mac) ──▶ 127.0.0.1:8080  MCP + A2A
                         (only /mcp, /a2a, /healthz,                              resume_agent serve
                          /.well-known/agent-card.json;   everything else: 404)
 you ──────────────────────────────────────────────────────────────────────────▶ 127.0.0.1:8050  dashboard (local only)
```

## What's here

| File | Purpose |
|---|---|
| `resumectl` | One script for everything: `install`, `tunnel-setup`, `start`, `stop`, `status`, `doctor`, `logs`, `token`, `uninstall` |
| `env.example` | Blank settings. `install` copies it to `deploy/macos/.env` (gitignored, chmod 600) and generates the bearer token |
| `cloudflared/config.example.yml` | Blank tunnel config with the path allowlist. Rendered to `~/.cloudflared/resume-agent.yml` |
| `launchd/*.plist.template` | Login services for the server, the tunnel and the local dashboard (restart on crash, logs in `~/Library/Logs/resume-agent/`) |
| `run.sh` | What launchd runs: loads `.env`, then starts one service |
| `render.py` | Fills in the templates (escapes paths, validates the hostname and tunnel id) |
| `Brewfile` | `python@3.12`, `poppler`, `cloudflared`, `basictex` |

Nothing with a secret or a machine-specific path is committed. The token lives in
`deploy/macos/.env`, the tunnel credentials in `~/.cloudflared/`, and the rendered plists in
`~/Library/LaunchAgents/`.

## Setup (about 10 minutes)

You need: a Mac with Homebrew, a Cloudflare account, and a domain on Cloudflare DNS.

```bash
git clone https://github.com/mzhl1111/latex-resume-agent && cd latex-resume-agent
# put your private data in place (see the top-level README): profile/ and templates/

deploy/macos/resumectl install          # brew deps, TeX packages (asks for sudo once), .venv, .env + token, launchd plists
$EDITOR deploy/macos/.env               # set TUNNEL_HOSTNAME=resume.<your-domain>; optionally a model key
                                        # (ANTHROPIC_API_KEY or OPENROUTER_API_KEY) + RESUME_*_MODEL
deploy/macos/resumectl tunnel-setup     # browser login to Cloudflare, creates the tunnel + DNS record, renders config
deploy/macos/resumectl start            # server + tunnel + local dashboard, now and at every login
deploy/macos/resumectl doctor           # checks tools, data, secrets, public health, 401 without token, dashboard NOT public
deploy/macos/resumectl token            # bearer token + MCP client snippet for your agents
deploy/macos/resumectl llm-check        # tiny test call to the writer/verifier models (if keys are set)
```

You'll see `https://resume.<your-domain>/healthz` return `{"ok": true, ...}`, and
`http://localhost:8050` shows the dashboard on the Mac itself.

**Prefer a dashboard-managed tunnel?** Create it in Cloudflare Zero Trust → Networks → Tunnels.
Paste its token into `TUNNEL_TOKEN` in `.env` and skip `tunnel-setup`. Then add the public
hostname → `http://127.0.0.1:8080` in the Cloudflare UI, and restrict the paths there too.

## Security model
- **Bearer token on every request.** `/mcp` and `/a2a` require `Authorization: Bearer $RESUME_AGENT_TOKEN`.
  `run.sh` sets `RESUME_REQUIRE_TOKEN=1`, so the server refuses to start without a token of at least
  24 characters. `install` generates 64 hex characters.
- **Path allowlist at the edge.** Cloudflare only forwards `/mcp`, `/a2a`, `/healthz` and
  `/.well-known/agent-card.json`. The dashboard, `/files/...` and the dashboard's editing API are never
  reachable from outside, and `resumectl doctor` checks this from the internet side.
- **Loopback only.** The server binds `127.0.0.1`, so nothing listens on your LAN.
- **Optional extra layer: Cloudflare Access.** Put an Access application with a *service token* in
  front of the hostname. Callers then also send `CF-Access-Client-Id` / `CF-Access-Client-Secret`.
  The tunnel config doesn't change.
- **Rotating the token:** clear `RESUME_AGENT_TOKEN=` in `.env`, run `resumectl install` (it generates
  a new one), then `resumectl restart server`, and update your agents.

## Keeping it reachable
- The Mac must be awake. On power, `sudo pmset -c sleep 0` (or System Settings → Battery → Options →
  "Prevent automatic sleeping…"). Closing the lid still sleeps the Mac unless it's in clamshell
  mode with external power and a display.
- The launchd agents run while you're logged in, and they restart on crash and at login.
- Logs: `resumectl logs server|tunnel|dashboard`.
- Traces from tunnel traffic show up in the dashboard with mode **Server**.

## Uninstall
`resumectl uninstall` removes the launchd agents. Your data, `.venv`, `.env` and the Cloudflare
tunnel are kept. Delete the tunnel with `cloudflared tunnel delete resume-agent` if you want it gone.
