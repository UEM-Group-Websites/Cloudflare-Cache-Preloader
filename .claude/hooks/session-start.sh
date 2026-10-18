#!/bin/bash
# Installs project dependencies so tests and linters work in Claude Code cloud sessions.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Run in the background so the session starts without waiting for the install.
echo '{"async": true, "asyncTimeout": 300000}'

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

# Written up front so the session's PATH is set even while the install is still running.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$PWD/.venv/bin:\$HOME/.local/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi

if ! command -v uv >/dev/null 2>&1; then
  # Pinned release from PyPI rather than piping a remote install script into sh.
  python3 -m pip install --user --break-system-packages "uv==0.11.32"
  export PATH="$HOME/.local/bin:$PATH"
fi

# Matches CI: Python version comes from .python-version, deps from uv.lock.
# --inexact keeps extras installed later (e.g. playwright) when the hook reruns on resume/compact.
uv sync --extra dev --inexact
