#!/usr/bin/env bash
# Set up osync-dash: create the venv for the Textual TUI and symlink the
# launcher onto your PATH. Safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "→ creating virtualenv (.venv) for the Textual TUI"
python3 -m venv "$HERE/.venv"
"$HERE/.venv/bin/pip" install --quiet --upgrade pip
"$HERE/.venv/bin/pip" install --quiet -r "$HERE/requirements.txt"

BIN="${1:-$HOME/.local/bin}"
mkdir -p "$BIN"
ln -sfn "$HERE/osync-dash" "$BIN/osync-dash"
echo "→ linked $BIN/osync-dash -> $HERE/osync-dash"
echo "done. Run:  osync-dash   (TUI)   ·   osync-dash --print   (one-shot)"
