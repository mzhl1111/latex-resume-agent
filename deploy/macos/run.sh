#!/usr/bin/env bash
# Process wrapper used by the launchd agents: loads deploy/macos/.env, then execs one service.
#   run.sh server     MCP + A2A on 127.0.0.1:$RESUME_PORT (token required)
#   run.sh tunnel     cloudflared (locally-managed config, or TUNNEL_TOKEN)
#   run.sh dashboard  local dashboard on 127.0.0.1:$DASHBOARD_PORT
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ENV_FILE="$HERE/.env"
TUNNEL_CONFIG="${TUNNEL_CONFIG:-$HOME/.cloudflared/resume-agent.yml}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# CLIs installed by their native installers live here (claude, codex)
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

PY="$REPO/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
cd "$REPO"

case "${1:-}" in
  server)
    export RESUME_REQUIRE_TOKEN=1          # never serve the tunnel without a bearer token
    export RESUME_TRACE_MODE=server
    if [[ -z "${PUBLIC_BASE_URL:-}" && -n "${TUNNEL_HOSTNAME:-}" ]]; then
      export PUBLIC_BASE_URL="https://${TUNNEL_HOSTNAME}"
    fi
    exec "$PY" -m resume_agent serve --host 127.0.0.1 --port "${RESUME_PORT:-8080}"
    ;;
  tunnel)
    if [[ -n "${TUNNEL_TOKEN:-}" ]]; then
      exec cloudflared tunnel --no-autoupdate run --token "$TUNNEL_TOKEN"
    fi
    [[ -f "$TUNNEL_CONFIG" ]] || { echo "No $TUNNEL_CONFIG; run: deploy/macos/resumectl tunnel-setup" >&2; exit 78; }
    exec cloudflared tunnel --no-autoupdate --config "$TUNNEL_CONFIG" run
    ;;
  dashboard)
    exec "$PY" -m resume_agent dashboard --host 127.0.0.1 --port "${DASHBOARD_PORT:-8050}"
    ;;
  *)
    echo "usage: run.sh server|tunnel|dashboard" >&2
    exit 64
    ;;
esac
