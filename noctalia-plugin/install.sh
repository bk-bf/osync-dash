#!/usr/bin/env bash
# Link this plugin into Noctalia. It stays an addon of this repo: the shell
# loads it through a symlink, so `git pull` here updates the plugin in place and
# Noctalia's hot reload (it follows symlinks) picks up edits live.
#
#   ./install.sh              link it
#   ./install.sh --uninstall  remove the link
set -euo pipefail

ID="osync-dash"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/noctalia/plugins/$ID"

if [[ "${1:-}" == "--uninstall" ]]; then
  if [[ -L "$DEST" ]]; then
    rm "$DEST"
    echo "unlinked $DEST"
  else
    echo "not linked (or not a symlink): $DEST" >&2
  fi
  exit 0
fi

if [[ -e "$DEST" && ! -L "$DEST" ]]; then
  echo "refusing to replace real directory: $DEST" >&2
  exit 1
fi

mkdir -p "$(dirname "$DEST")"
ln -sfn "$SRC" "$DEST"
echo "linked $DEST -> $SRC"

# The plugin shells out to the osync-dash launcher; warn early if it is missing.
if ! command -v osync-dash >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/osync-dash" ]]; then
  echo
  echo "warning: osync-dash not found on PATH or at ~/.local/bin/osync-dash." >&2
  echo "         Run this repo's ../install.sh first — the plugin depends on it." >&2
fi

echo
echo "Next: Noctalia → Settings → Plugins → Installed → enable \"osync\"."
echo "It is discovered but disabled by default; enabling adds the pill to your bar."
