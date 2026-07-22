#!/usr/bin/env bash
# osync-dash installer — curl-pipeable and idempotent.
#
#   curl -fsSL https://raw.githubusercontent.com/bk-bf/osync-dash/main/install.sh | bash
#
# Installs a version-pinned osync (from versions.env) plus the Textual TUI, so
# every node runs the exact same osync build — behaviour and log format matched.
#
# Usage:
#   install.sh [BINDIR]              install here (BINDIR defaults to ~/.local/bin)
#   install.sh --remote user@host    copy the pinned osync onto a remote replica
#   install.sh --print-osync         print the path to the vendored osync.sh
#
# Env: OSYNC_DASH_HOME (default ~/.local/share/osync-dash), OSYNC_DASH_REPO.
set -euo pipefail

REPO="${OSYNC_DASH_REPO:-https://github.com/bk-bf/osync-dash.git}"
PREFIX="${OSYNC_DASH_HOME:-$HOME/.local/share/osync-dash}"

say() { printf '→ %s\n' "$*"; }
die() { printf 'osync-dash install: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"; }

# ── locate (or fetch) the source tree ────────────────────────────────────────
# When run from a checkout we use it in place; when piped through curl we clone.
resolve_src() {
  local self="${BASH_SOURCE[0]:-}"
  if [ -n "$self" ] && [ -f "$(dirname "$self")/osync_core.py" ]; then
    (cd "$(dirname "$self")" && pwd)
  else
    need git
    say "fetching osync-dash → $PREFIX/src"
    rm -rf "$PREFIX/src"; mkdir -p "$PREFIX"
    git clone --depth 1 "$REPO" "$PREFIX/src" >/dev/null 2>&1 || die "git clone failed ($REPO)"
    printf '%s\n' "$PREFIX/src"
  fi
}

SRC="$(resolve_src)"
# shellcheck disable=SC1091
. "$SRC/versions.env"
OSYNC_DIR="$PREFIX/osync"

# ── vendored, version-pinned osync ───────────────────────────────────────────
vendor_osync() {
  if [ -x "$OSYNC_DIR/osync.sh" ] && [ "$(cat "$OSYNC_DIR/.version" 2>/dev/null)" = "$OSYNC_VERSION" ]; then
    say "osync $OSYNC_VERSION already vendored"; return
  fi
  need git
  say "vendoring osync $OSYNC_VERSION → $OSYNC_DIR"
  rm -rf "$OSYNC_DIR"; mkdir -p "$(dirname "$OSYNC_DIR")"
  git clone --depth 1 --branch "$OSYNC_VERSION" "$OSYNC_REPO" "$OSYNC_DIR" >/dev/null 2>&1 \
    || die "could not clone osync $OSYNC_VERSION from $OSYNC_REPO"
  chmod +x "$OSYNC_DIR/osync.sh"
  printf '%s\n' "$OSYNC_VERSION" > "$OSYNC_DIR/.version"
}

# ── deploy the SAME osync.sh onto a remote replica (byte-identical) ───────────
# osync.sh is a single self-contained script, so one scp guarantees a match.
deploy_remote() {
  local host="$1"; [ -n "$host" ] || die "--remote needs user@host"
  vendor_osync
  say "deploying osync $OSYNC_VERSION → $host"
  ssh "$host" 'mkdir -p ~/.local/share/osync-dash/osync ~/.local/bin' \
    || die "ssh to $host failed"
  scp -q "$OSYNC_DIR/osync.sh" "$host:.local/share/osync-dash/osync/osync.sh"
  ssh "$host" 'chmod +x ~/.local/share/osync-dash/osync/osync.sh &&
    ln -sfn ~/.local/share/osync-dash/osync/osync.sh ~/.local/bin/osync.sh &&
    printf "→ remote now on: " && ~/.local/share/osync-dash/osync/osync.sh --version 2>&1 | head -1'
  say "done — $host matches osync $OSYNC_VERSION"
}

# ── argument handling ────────────────────────────────────────────────────────
case "${1:-}" in
  --remote)        deploy_remote "${2:-}"; exit 0 ;;
  --print-osync)   vendor_osync >&2; printf '%s\n' "$OSYNC_DIR/osync.sh"; exit 0 ;;
esac

BIN="${1:-$HOME/.local/bin}"
need python3

vendor_osync

say "creating virtualenv (.venv) for the Textual TUI"
python3 -m venv "$SRC/.venv"
"$SRC/.venv/bin/pip" install --quiet --upgrade pip
"$SRC/.venv/bin/pip" install --quiet "textual==$TEXTUAL_VERSION"

mkdir -p "$BIN"
ln -sfn "$SRC/osync-dash" "$BIN/osync-dash"
say "linked $BIN/osync-dash -> $SRC/osync-dash"
say "vendored osync: $("$OSYNC_DIR/osync.sh" --version 2>&1 | head -1)"
echo
echo "done. Ensure $BIN is on your PATH, then run:"
echo "    osync-dash                        # the TUI"
echo "    osync-dash --print                # one-shot render"
echo "    $SRC/install.sh --remote user@host  # match osync on a replica"
