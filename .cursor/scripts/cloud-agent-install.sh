#!/usr/bin/env bash
set -euo pipefail

if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
sudo ln -sf "${HOME}/.local/bin/uv" /usr/local/bin/uv

cd /workspace
uv sync --extra dev
