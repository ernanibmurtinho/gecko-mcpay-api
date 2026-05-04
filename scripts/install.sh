#!/usr/bin/env bash
# =============================================================================
# Gecko one-line installer
#
#   curl -fsSL https://app.geckovision.tech/install.sh | bash
#
# What this does:
#   1. Verifies prereqs: Python 3.11+, uv, Claude Code CLI.
#   2. Installs `gecko-mcp` from PyPI via `uv tool install`.
#   3. Registers the MCP server with Claude Code (best-effort).
#   4. Prints next steps — wallet setup, then a research call.
#
# Flags:
#   --no-mcp-register   Skip the `claude mcp add` step.
# =============================================================================
set -euo pipefail

SKIP_MCP_REGISTER=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-mcp-register) SKIP_MCP_REGISTER=true; shift ;;
    *) echo "Unknown argument: $1"; exit 2 ;;
  esac
done

c_red()    { printf "\033[31m%s\033[0m" "$*"; }
c_green()  { printf "\033[32m%s\033[0m" "$*"; }
c_yellow() { printf "\033[33m%s\033[0m" "$*"; }
c_bold()   { printf "\033[1m%s\033[0m" "$*"; }

ok()    { echo "  $(c_green ✅) $*"; }
warn()  { echo "  $(c_yellow ⚠️ ) $*"; }
fail()  { echo "  $(c_red ❌) $*"; }
hdr()   { echo; echo "$(c_bold "▶ $*")"; }

# -----------------------------------------------------------------------------

hdr "1/4 Prereqs"

if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 not found — install Python 3.11+ first"
  exit 1
fi
PY_VERSION="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
PY_MAJOR="$(echo "$PY_VERSION" | cut -d. -f1)"
PY_MINOR="$(echo "$PY_VERSION" | cut -d. -f2)"
if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 11 ]]; }; then
  fail "Python 3.11+ required (found $PY_VERSION)"
  exit 1
fi
ok "Python $PY_VERSION"

if ! command -v uv >/dev/null 2>&1; then
  warn "uv not found — installing"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"

if command -v claude >/dev/null 2>&1; then
  ok "Claude Code CLI present"
  HAVE_CLAUDE=true
else
  warn "Claude Code CLI not found — MCP registration will be skipped"
  HAVE_CLAUDE=false
fi

# -----------------------------------------------------------------------------

hdr "2/4 Install gecko-mcp"

uv tool install --force gecko-mcp
ok "gecko-mcp installed ($(gecko-mcp --version 2>/dev/null || echo 'ok'))"

# -----------------------------------------------------------------------------

hdr "3/4 Register with Claude Code"

if [[ "$SKIP_MCP_REGISTER" == "true" ]] || [[ "$HAVE_CLAUDE" == "false" ]]; then
  warn "skipped (run manually: claude mcp add gecko -- gecko-mcp serve)"
else
  if claude mcp list 2>/dev/null | grep -q '^gecko'; then
    ok "gecko already registered"
  else
    claude mcp add gecko -- gecko-mcp serve >/dev/null
    ok "gecko registered with Claude Code"
  fi
fi

# -----------------------------------------------------------------------------

hdr "4/4 Next steps"

cat <<'EOF'

  Set up your wallet (paste into Claude Code):

      gecko-mcp wallet new

  Verify everything is working:

      gecko-mcp doctor

  Run your first session (in Claude Code):

      Use gecko_research to validate: a hotel guide for Brazil

  Builder Bootstrap Platform · geckovision.tech · No API keys, just a wallet.
EOF
