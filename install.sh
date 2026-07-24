#!/usr/bin/env bash
# osd installer — curl-pipeable and idempotent.
#
#   curl -fsSL https://raw.githubusercontent.com/bk-bf/osync-dash/main/install.sh | bash
#
# Installs a version-pinned osync (from versions.env) plus the Textual TUI, so
# every node runs the exact same osync build — behaviour and log format matched.
#
# Usage:
#   install.sh [BINDIR]                 install here (BINDIR defaults to ~/.local/bin)
#   install.sh --remote user@host       copy the pinned osync onto a remote replica
#   install.sh --print-osync            print the path to the vendored osync.sh
#   install.sh --uninstall [--yes]      remove osd, its osync, units, and
#                                       every setup it created on this machine
#   install.sh --uninstall --purge-remote   also wipe .osync_workdir on replicas
#
# Env: OSD_HOME (default ~/.local/share/osd), OSD_REPO.
set -euo pipefail

REPO="${OSD_REPO:-https://github.com/bk-bf/osync-dash.git}"
PREFIX="${OSD_HOME:-$HOME/.local/share/osd}"

say() { printf '→ %s\n' "$*" >&2; }  # stderr: never pollute $(resolve_src) etc.
die() { printf 'osd install: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"; }

# ── uninstall — removes everything this installer + the TUI ever created ──────
# Handled before anything else so it never clones a source tree just to delete.
uninstall() {
  local purge_remote=0 yes=0 a
  for a in "$@"; do
    case "$a" in
      --purge-remote) purge_remote=1 ;;
      --yes|-y)       yes=1 ;;
      *) die "unknown uninstall flag: $a" ;;
    esac
  done
  local compose="$HOME/.config/osync/osd.toml"
  local cache="$HOME/.cache/osync"

  # read each connection's dirs from the compose *before* deleting it
  local conns=""
  if [ -f "$compose" ] && command -v python3 >/dev/null 2>&1; then
    conns="$(python3 - "$compose" <<'PY' || true
import sys, os
try:
    import tomllib
    d = tomllib.load(open(sys.argv[1], "rb"))
except Exception:
    raise SystemExit
defs = d.get("defaults") or {}
for c in d.get("connection") or []:
    row = [os.path.expanduser(str(c.get("local", ""))),
           str(c.get("user") or defs.get("user", "")),
           str(c.get("ts_host") or c.get("ssh_host") or ""),
           str(c.get("ts_port") or c.get("ssh_port") or 22),
           os.path.expanduser(str(c.get("key") or defs.get("key", "") or "")),
           str(c.get("remote", ""))]
    print("\t".join(row))
PY
)"
  fi

  echo "osd uninstall will remove:"
  echo "  • the launcher symlink(s) and $PREFIX (src, vendored osync, venv)"
  echo "  • all systemd --user units  osd-*  (stopped + disabled)"
  echo "  • $cache/generated and $cache/*.log"
  echo "  • the compose file  $compose"
  echo "  • the yeet spool  ~/.local/share/yeet  (any drops parked here, undelivered)"
  echo "  • .osync_workdir in each LOCAL synced dir"
  echo "    ⚠ that deletes osync's soft-delete + conflict-backup safety net"
  [ "$purge_remote" = 1 ] && echo "  • .osync_workdir on each REMOTE replica too (--purge-remote)"
  echo "  Your actual synced files are left untouched."
  echo
  if [ "$yes" != 1 ]; then
    [ -t 0 ] || die "non-interactive shell — re-run with --yes to confirm"
    read -rp "Proceed? [y/N] " a; case "$a" in y|Y|yes|YES) ;; *) echo "aborted."; exit 1 ;; esac
  fi

  # 1. systemd units
  if command -v systemctl >/dev/null 2>&1; then
    for a in $(systemctl --user list-unit-files --no-legend 'osd-*' 2>/dev/null | awk '{print $1}'); do
      systemctl --user disable --now "$a" >/dev/null 2>&1 || true
    done
    rm -f "$HOME/.config/systemd/user/"osd-*.service \
          "$HOME/.config/systemd/user/"osd-*.timer 2>/dev/null || true
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    say "removed systemd units"
  fi

  # 2. .osync_workdir in the synced dirs (local always; remote if asked)
  if [ -n "$conns" ]; then
    local L U H P K R opts
    while IFS=$'\t' read -r L U H P K R; do
      [ -n "$L" ] && [ -d "$L/.osync_workdir" ] && rm -rf "$L/.osync_workdir" && say "removed $L/.osync_workdir"
      if [ "$purge_remote" = 1 ] && [ -n "$H" ] && [ -n "$R" ]; then
        opts=(-o BatchMode=yes -o ConnectTimeout=6 -o ControlPath=none -p "$P")
        [ -n "$K" ] && [ -f "$K" ] && opts+=(-i "$K")
        if ssh "${opts[@]}" "$U@$H" "rm -rf \"$R/.osync_workdir\"" 2>/dev/null; then
          say "removed $U@$H:$R/.osync_workdir"
        else
          say "! could not reach $H — left $R/.osync_workdir in place"
        fi
      fi
    done <<< "$conns"
  fi

  # 3. compose + cache (keep any hand-written legacy *.conf; rmdir only if empty)
  rm -f "$compose"; rmdir "$HOME/.config/osync" 2>/dev/null || true
  rm -rf "$cache/generated"; rm -f "$cache/"*.log 2>/dev/null || true
  rmdir "$cache" 2>/dev/null || true
  say "removed compose + cache"

  # 4. launcher symlinks (only if they're symlinks — never a real file)
  for a in "$HOME/.local/bin/osd" "$HOME/bin/osd" "/usr/local/bin/osd" \
           "$HOME/.local/bin/yeet" "$HOME/bin/yeet" "/usr/local/bin/yeet"; do
    [ -L "$a" ] && rm -f "$a" && say "removed $a"
  done
  for n in osd yeet; do
    a="$(command -v "$n" 2>/dev/null || true)"
    [ -n "$a" ] && [ -L "$a" ] && rm -f "$a" && say "removed $a"
  done
  rm -rf "$HOME/.local/share/yeet"; say "removed the yeet spool"

  # 5. the install prefix (vendored osync + venv + src) — last, we may run from it
  rm -rf "$PREFIX"; say "removed $PREFIX"
  echo; echo "osd uninstalled."
}

if [ "${1:-}" = "--uninstall" ]; then shift; uninstall "$@"; exit 0; fi

# ── locate (or fetch) the source tree ────────────────────────────────────────
# When run from a checkout we use it in place; when piped through curl we clone.
resolve_src() {
  local self="${BASH_SOURCE[0]:-}"
  if [ -n "$self" ] && [ -f "$(dirname "$self")/osd_core.py" ]; then
    (cd "$(dirname "$self")" && pwd)
  else
    need git
    say "fetching osd → $PREFIX/src"
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
  ssh "$host" 'mkdir -p ~/.local/share/osd/osync ~/.local/bin' \
    || die "ssh to $host failed"
  scp -q "$OSYNC_DIR/osync.sh" "$host:.local/share/osd/osync/osync.sh"
  ssh "$host" 'chmod +x ~/.local/share/osd/osync/osync.sh &&
    ln -sfn ~/.local/share/osd/osync/osync.sh ~/.local/bin/osync.sh &&
    printf "→ remote now on: " && ~/.local/share/osd/osync/osync.sh --version 2>&1 | head -1'
  say "done — $host matches osync $OSYNC_VERSION"
}

# ── argument handling ────────────────────────────────────────────────────────
case "${1:-}" in
  --remote)        deploy_remote "${2:-}"; exit 0 ;;
  --print-osync)   vendor_osync >&2; printf '%s\n' "$OSYNC_DIR/osync.sh"; exit 0 ;;
esac

BIN="${1:-$HOME/.local/bin}"
need python3

# ── carry a pre-rename install over (osync-dash → osd) ───────────────────────
# Old systemd units keep firing under their own name and would run a launcher
# that no longer exists, so they are retired here; osd rewrites them on next use.
migrate_from_osync_dash() {
  local ud="$HOME/.config/systemd/user" old=0
  if command -v systemctl >/dev/null 2>&1 && compgen -G "$ud/osync-dash-*" >/dev/null; then
    for u in "$ud"/osync-dash-*.timer; do
      [ -e "$u" ] || continue
      systemctl --user disable --now "$(basename "$u")" >/dev/null 2>&1 || true
      old=1
    done
    rm -f "$ud"/osync-dash-*.service "$ud"/osync-dash-*.timer
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    [ "$old" = 1 ] && say "retired pre-rename systemd units (osd rewrites them)"
  fi
  [ -f "$HOME/.config/osync/osync-dash.toml" ] && [ ! -f "$HOME/.config/osync/osd.toml" ] &&
    mv "$HOME/.config/osync/osync-dash.toml" "$HOME/.config/osync/osd.toml" &&
    say "moved compose file to osd.toml"
  for a in "$BIN/osync-dash" "$HOME/.local/bin/osync-dash"; do
    [ -L "$a" ] && rm -f "$a" && say "removed old $a launcher"
  done
  [ -d "$HOME/.local/share/osync-dash" ] &&
    say "note: the old prefix ~/.local/share/osync-dash can be deleted"
  return 0
}
migrate_from_osync_dash

vendor_osync

say "creating virtualenv (.venv) for the Textual TUI"
python3 -m venv "$SRC/.venv"
"$SRC/.venv/bin/pip" install --quiet --upgrade pip
"$SRC/.venv/bin/pip" install --quiet "textual==$TEXTUAL_VERSION"

mkdir -p "$BIN"
ln -sfn "$SRC/osd" "$BIN/osd"
say "linked $BIN/osd -> $SRC/osd"
ln -sfn "$SRC/yeet" "$BIN/yeet"
say "linked $BIN/yeet -> $SRC/yeet"
say "vendored osync: $("$OSYNC_DIR/osync.sh" --version 2>&1 | head -1)"
echo
echo "done. Ensure $BIN is on your PATH, then run:"
echo "    osd                        # the TUI"
echo "    osd --print                # one-shot render"
echo "    yeet FILE / yeet                  # one-shot drops across the tailnet"
echo "    $SRC/install.sh --remote user@host  # match osync on a replica"
echo "    $SRC/install.sh --uninstall         # remove everything"
