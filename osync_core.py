#!/usr/bin/env python3
"""osync-dash — terminal dashboard for osync sync jobs (core / one-shot renderer).

Reads the compose file (~/.config/osync/osync-dash.toml), which defines many
sync connections in one place, probes both replicas of each (local + remote
over ssh), and renders health, devices, paths and the soft-delete/backup safety
net. This core is pure Python stdlib; the interactive TUI (osync_tui.py) adds
Textual.

Usage:
    osync-dash [-c NAME] [options]

Run with no arguments for the interactive Textual TUI (auto-refreshing,
keyboard-driven), which shows every connection as its own card. In the TUI:
r refresh · c check pending · s sync · l log · q quit.

Piped or with --print it falls back to a one-shot render of every connection.

Options:
    -c, --config NAME   Limit to one connection (by name) or a standalone .conf
                        path. Default: all connections in the compose file.
    -p, --print         One-shot render to stdout instead of the TUI (also the
                        automatic behaviour when output is piped).
    -f, --fast          Skip the pending dry-run (no ssh rsync pass).
    -i, --interval N    TUI auto-refresh seconds (default 6).
        --check         Include the pending dry-run in --print output.
        --sync          Run the sync now (streams osync output), print the
                        result, exit. Extra args after -- are passed to osync.
        --log           Page the full osync log (less), then exit.
        --local-only    Skip the remote ssh probe (offline / fast).
        --no-color      Disable ANSI colour (--print mode).
    -h, --help          This help.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

def _resolve_osync() -> str:
    """Prefer the version-pinned osync vendored by install.sh (so local matches
    the build deployed to replicas), then fall back to PATH."""
    home = os.environ.get("OSYNC_DASH_HOME", os.path.expanduser("~/.local/share/osync-dash"))
    vendored = os.path.join(home, "osync", "osync.sh")
    if os.path.exists(vendored):
        return vendored
    return shutil.which("osync.sh") or "/usr/local/bin/osync.sh"


OSYNC_BIN = _resolve_osync()
OSYNC_DIR = ".osync_workdir"
CONFIG_HOME = Path(os.path.expanduser("~/.config/osync"))
# single source of truth: one compose file defining many sync connections
COMPOSE_FILE = CONFIG_HOME / "osync-dash.toml"
# real osync .conf files are materialised here on demand (one per connection)
GEN_DIR = Path(os.path.expanduser("~/.cache/osync/generated"))
COMPOSE_DEFAULTS = {"user": os.environ.get("USER", ""), "key": "~/.ssh/id_ed25519",
                    "soft_delete_days": 30, "conflict_backup_days": 30}
# global [settings] — portable across machines (notify command is configurable
# so it isn't hard-wired to notify-send). delete_guard = block a sync whose
# dry-run would propagate more than N deletions (0 disables the guard).
SETTINGS_DEFAULTS = {"notify": True, "notify_cmd": "notify-send", "delete_guard": 25,
                     "node": socket.gethostname()}
STALE_AFTER = 24 * 3600  # a sync older than this flips health to STALE

# ── ANSI ────────────────────────────────────────────────────────────────────
_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str) -> str:
    return code if _COLOR else ""


RESET = _c("\033[0m")
DIM = _c("\033[2m")
BOLD = _c("\033[1m")


def fg(n: int) -> str:
    return _c(f"\033[38;5;{n}m")


GREEN, YELLOW, RED, BLUE, CYAN, GREY, WHITE, MAGENTA = (
    fg(114), fg(179), fg(203), fg(75), fg(80), fg(244), fg(252), fg(176),
)

# name -> ANSI, shared with health(); the curses front-end maps the same names
ANSI_BY_NAME = dict(green=GREEN, yellow=YELLOW, red=RED, blue=BLUE,
                    cyan=CYAN, grey=GREY, white=WHITE, magenta=MAGENTA)


def strip_ansi(s: str) -> int:
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


# ── config parsing ──────────────────────────────────────────────────────────
def parse_config(path: Path) -> dict:
    """osync 1.3 configs are parsed literally (grep-style), not sourced."""
    cfg: dict[str, str] = {}
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        cfg[key] = val
    return cfg


def parse_target(uri: str) -> dict:
    """ssh://user@host:port//abs/path  ->  {user,host,port,path,remote}."""
    m = re.match(r"^ssh://(?:([^@]+)@)?([^:/]+)(?::(\d+))?(/.*)$", uri)
    if not m:
        return {"remote": False, "path": uri}
    path = m.group(4)
    if path.startswith("//"):
        path = path[1:]  # osync uses // to mark an absolute remote path
    return {
        "remote": True,
        "user": m.group(1) or os.environ.get("USER", ""),
        "host": m.group(2),
        "port": m.group(3) or "22",
        "path": path,
    }


def ssh_prefix(cfg: dict, tgt: dict, connect_timeout: int = 6) -> list[str]:
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}",
           "-o", "ControlPath=none", "-p", tgt["port"]]
    key = cfg.get("SSH_RSA_PRIVATE_KEY", "").strip()
    if key and os.path.exists(os.path.expanduser(key)):
        cmd += ["-i", os.path.expanduser(key)]
    cmd.append(f'{tgt["user"]}@{tgt["host"]}')
    return cmd


# ── formatting helpers ──────────────────────────────────────────────────────
def humansize(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "K", "M", "G", "T", "P"):
        if n < 1024 or unit == "P":
            if unit == "B":
                return f"{n:.0f}B"
            return f"{n:.0f}{unit}" if n >= 100 else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.0f}P"


def human_age(secs) -> str:
    if secs is None:
        return "never"
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h"


def run(cmd, timeout=20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return 255, str(e)


# ── probes ──────────────────────────────────────────────────────────────────
def tailscale_map() -> dict:
    """Map lowercased hostname AND tailnet IPv4 -> {name, ip} for tailscale nodes.

    'name' is the short MagicDNS label (first component of the DNSName). Empty
    dict if tailscale isn't installed / not up — everything degrades gracefully.
    """
    if not shutil.which("tailscale"):
        return {}
    rc, out = run(["tailscale", "status", "--json"], timeout=6)
    if rc != 0 or "{" not in out:
        return {}
    try:
        # run() merges stderr (tailscale emits a version-skew warning there),
        # so decode just the leading JSON object and ignore any trailing text.
        data, _ = json.JSONDecoder().raw_decode(out[out.index("{"):])
    except (json.JSONDecodeError, ValueError):
        return {}
    m: dict = {}
    nodes = [data.get("Self", {})] + list((data.get("Peer") or {}).values())
    for n in nodes:
        if not n:
            continue
        dns = (n.get("DNSName") or "").rstrip(".")
        name = dns.split(".")[0] if dns else (n.get("HostName") or "")
        ip4 = next((ip for ip in (n.get("TailscaleIPs") or []) if ":" not in ip), None)
        entry = {"name": name, "ip": ip4, "dns": dns}
        if n.get("HostName"):
            m.setdefault(n["HostName"].lower(), entry)
        if ip4:
            m[ip4] = entry
    return m


def resolve_ssh_host(host: str) -> str | None:
    """Ask ssh what a Host alias actually resolves to (no network hit)."""
    rc, out = run(["ssh", "-G", host], timeout=6)
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.lower().startswith("hostname "):
            return line.split(None, 1)[1].strip()
    return None


def probe_local(sync_dir: Path, baseline=None) -> dict:
    d = {"reach": sync_dir.is_dir(), "rsync": bool(shutil.which("rsync")),
         "host": socket.gethostname()}
    if not d["reach"]:
        return d
    files = size = changed = 0
    for root, dirs, fs in os.walk(sync_dir):
        if OSYNC_DIR in Path(root).parts:
            dirs[:] = [x for x in dirs if x != OSYNC_DIR]
            continue
        if OSYNC_DIR in dirs:
            dirs.remove(OSYNC_DIR)
        for f in fs:
            fp = Path(root) / f
            try:
                stt = fp.stat()
                size += stt.st_size
                files += 1
                # git-style live diff: files touched since the last sync are the
                # local changes waiting to push (mtime beats the baseline).
                if baseline is not None and stt.st_mtime > baseline:
                    changed += 1
            except OSError:
                pass
    d["files"], d["size"] = files, size
    d["changed"] = changed if baseline is not None else None
    try:
        st = os.statvfs(sync_dir)
        d["free"] = st.f_bavail * st.f_frsize
        d["disk_total"] = st.f_blocks * st.f_frsize
        d["disk_used"] = (st.f_blocks - st.f_bfree) * st.f_frsize
    except OSError:
        d["free"] = d["disk_total"] = d["disk_used"] = None
    wd = sync_dir / OSYNC_DIR

    def _count(p):  # rglob can race osync writing soft-deletes/backups mid-sync
        try:
            return sum(1 for x in p.rglob("*") if x.is_file()) if p.is_dir() else 0
        except OSError:
            return 0
    d["deleted"] = _count(wd / "deleted")
    d["backup"] = _count(wd / "backup")
    return d


REMOTE_PROBE = r'''
p=%s
b=%s
echo "REACH=1"
command -v rsync >/dev/null && echo "RSYNC=1" || echo "RSYNC=0"
echo "HOST=$(hostname 2>/dev/null)"
if [ -d "$p" ]; then
  echo "FILES=$(find "$p" -type f -not -path '*/.osync_workdir/*' 2>/dev/null | wc -l)"
  echo "SIZE=$(du -sb "$p" --exclude=.osync_workdir 2>/dev/null | cut -f1)"
  df -PB1 "$p" 2>/dev/null | awk 'NR==2{print "DISK_TOTAL="$2"\nDISK_USED="$3"\nFREE="$4}'
  echo "DELETED=$(find "$p/.osync_workdir/deleted" -type f 2>/dev/null | wc -l)"
  echo "BACKUP=$(find "$p/.osync_workdir/backup" -type f 2>/dev/null | wc -l)"
  if [ -n "$b" ]; then
    echo "CHANGED=$(find "$p" -type f -not -path '*/.osync_workdir/*' -newermt @"$b" 2>/dev/null | wc -l)"
  fi
  echo "DIR=1"
else
  echo "DIR=0"
fi
'''


def probe_remote(cfg: dict, tgt: dict, baseline=None) -> dict:
    import shlex
    script = REMOTE_PROBE % (shlex.quote(tgt["path"]),
                             shlex.quote(str(int(baseline)) if baseline else ""))
    rc, out = run(ssh_prefix(cfg, tgt) + [script], timeout=20)
    if rc != 0:
        return {"reach": False, "err": out.strip().splitlines()[-1] if out.strip() else "unreachable"}
    d = {"reach": True}
    for line in out.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        d[k.lower()] = v.strip()
    d["rsync"] = d.get("rsync") == "1"
    for k in ("files", "size", "free", "deleted", "backup", "disk_total", "disk_used", "changed"):
        try:
            d[k] = int(d[k])
        except (KeyError, ValueError):
            d[k] = None
    d["dir_exists"] = d.get("dir") == "1"
    return d


def sync_running(sync_dir: Path) -> bool:
    """Cheap, local-only 'is a sync in progress?' — osync holds a lock file at
    <sync>/.osync_workdir/lock for the whole run. No ssh, no pgrep, so it's fine
    to poll often (used for the live spinner + skip-probe-during-sync)."""
    try:
        return (Path(sync_dir) / OSYNC_DIR / "lock").exists()
    except OSError:
        return False


def _state_text(p: Path) -> str | None:
    # osync rewrites these constantly mid-sync, so read defensively — a file that
    # vanishes between check and read must not raise (it just reads as unknown).
    try:
        return p.read_text(errors="replace").strip()
    except OSError:
        return None


def _state_mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def probe_state(cfg: dict, sync_dir: Path) -> dict:
    inst = cfg.get("INSTANCE_ID", "")
    sd = sync_dir / OSYNC_DIR / "state"
    d = {"instance": inst, "running": False, "last_ts": None,
         "init_action": None, "tgt_action": None, "resume": None}
    d["last_ts"] = _state_mtime(sd / f"initiator-last-action-{inst}")
    d["init_action"] = _state_text(sd / f"initiator-last-action-{inst}")
    d["tgt_action"] = _state_text(sd / f"target-last-action-{inst}")
    d["resume"] = _state_text(sd / f"resume-count-{inst}")
    # running? lock file (fast) or a live osync process for this config
    d["running"] = sync_running(sync_dir)
    rc2, out = run(["pgrep", "-fa", "osync.sh"], timeout=5)
    if rc2 == 0 and inst:
        for line in out.splitlines():
            if "--dry" in line:  # ignore our own pending dry-run checks
                continue
            if inst in line or cfg.get("_configfile", "") in line:
                d["running"] = True
                break
    return d


def health(state: dict, remote: dict, local: dict) -> tuple[str, str]:
    """Return (label, colour-name). Name is mapped to ANSI or curses per front-end."""
    if state["running"]:
        return "RUNNING", "blue"
    if state["last_ts"] is None:
        return "NEVER RUN", "grey"
    if not local.get("reach"):
        return "NO LOCAL DIR", "red"
    if not remote.get("reach", True):
        return "TARGET UNREACHABLE", "yellow"
    bad = (state.get("init_action") not in (None, "synced")
           or state.get("tgt_action") not in (None, "synced")
           or (state.get("resume") not in (None, "0", "")))
    if bad:
        return "ERROR", "red"
    if time.time() - state["last_ts"] > STALE_AFTER:
        return "STALE", "yellow"
    return "HEALTHY", "green"


# ── rendering ───────────────────────────────────────────────────────────────
def term_width() -> int:
    return max(56, min(shutil.get_terminal_size((100, 40)).columns, 104))


def pad(s: str, w: int) -> str:
    return s + " " * max(0, w - strip_ansi(s))


def box(title: str, rows: list[str], width: int, color: str) -> list[str]:
    inner = width - 2
    top = f"{color}╭─ {BOLD}{title}{RESET}{color} " + "─" * max(0, inner - strip_ansi(title) - 3) + f"╮{RESET}"
    out = [top]
    for r in rows:
        out.append(f"{color}│{RESET} " + pad(r, inner - 2) + f" {color}│{RESET}")
    out.append(f"{color}╰" + "─" * inner + f"╯{RESET}")
    return out


def kv(k: str, v: str, kw: int = 13) -> str:
    return f"{GREY}{pad(k, kw)}{RESET}{v}"


def dot(ok, warn=False) -> str:
    if warn:
        return f"{YELLOW}●{RESET}"
    return f"{GREEN}●{RESET}" if ok else f"{RED}●{RESET}"


def render(cfg, tgt, state, local, remote, width) -> str:
    st, col = health(state, remote, local)
    col = ANSI_BY_NAME.get(col, "")
    age = human_age(time.time() - state["last_ts"] if state["last_ts"] else None)
    stype = cfg.get("SYNC_TYPE", "").strip() or "bidirectional"
    lines: list[str] = []

    # header / health banner
    badge = f"{col}{BOLD}● {st}{RESET}"
    sub = f"{GREY}last sync{RESET} {WHITE}{age}{RESET}   {GREY}mode{RESET} {WHITE}{stype}{RESET}"
    if state["running"]:
        sub = f"{BLUE}sync in progress…{RESET}   " + sub
    lines += box(f"osync · {cfg.get('INSTANCE_ID', '?')}", [f"{badge}    {sub}"], width, col)
    lines.append("")

    # devices table — role + real hostname, with a Tailscale identity sub-line
    def dev_lines(role, r):
        reach = r.get("reach", False)
        rs = "✓" if r.get("rsync") else ("—" if not reach else "✗")
        files = "—" if r.get("files") is None else str(r.get("files"))
        size = humansize(r.get("size")) if r.get("size") is not None else "—"
        free = humansize(r.get("free")) if r.get("free") is not None else "—"
        host = r.get("host") or (r.get("ts") or {}).get("name") or "—"
        cell = pad(f"{CYAN}▸{RESET} {pad(role, 9)} {WHITE}{host}{RESET}", 30)
        out = [f"{cell}{pad(dot(reach), 4)}{pad(rs, 8)}{pad(files, 8)}{pad(size, 9)}{free}"]
        ts = r.get("ts")
        if ts and ts.get("name"):
            ip = ts.get("ip") or ""
            out.append(f"  {DIM}↳ tailscale{RESET} {GREY}{ts['name']}{('  ·  ' + ip) if ip else ''}{RESET}")
        return out

    tgt_dev = remote if tgt.get("remote") else local
    hdr = f"{GREY}{pad('  device', 30)}{pad('up', 4)}{pad('rsync', 8)}{pad('files', 8)}{pad('size', 9)}free{RESET}"
    drows = [hdr] + dev_lines("local", local) + dev_lines("remote", tgt_dev)
    if tgt.get("remote") and not remote.get("reach", False):
        drows.append(f"{YELLOW}  ! remote: {remote.get('err', 'unreachable')}{RESET}")
    lines += box("devices", drows, width, GREY)
    lines.append("")

    # sync state
    res = state.get("init_action") or "—"
    res_c = GREEN if res == "synced" else (GREY if res == "—" else RED)
    resume = state.get("resume")
    resume_txt = f"{GREEN}0 (clean){RESET}" if resume in ("0", None) else f"{RED}{resume} (retried){RESET}"
    lastrun = time.strftime("%Y-%m-%d %H:%M", time.localtime(state["last_ts"])) if state["last_ts"] else "never"
    srows = [
        kv("last run", f"{WHITE}{lastrun}{RESET}  {GREY}({age} ago){RESET}"),
        kv("result", f"{res_c}{res}{RESET}   remote: {res_c if state.get('tgt_action')=='synced' else GREY}{state.get('tgt_action') or '—'}{RESET}"),
        kv("resume", resume_txt),
        kv("running", f"{BLUE}yes{RESET}" if state["running"] else f"{GREY}no{RESET}"),
    ]
    lines += box("sync state", srows, width, col)
    lines.append("")

    # paths
    prows = [
        kv("local", f"{WHITE}{cfg.get('INITIATOR_SYNC_DIR','?')}{RESET}", 11),
        kv("remote", f"{WHITE}{tgt.get('user','')}@{tgt.get('host','')}:{tgt.get('path','?')}{RESET}" if tgt.get("remote") else cfg.get("TARGET_SYNC_DIR", "?"), 11),
        kv("workdir", f"{DIM}{cfg.get('INITIATOR_SYNC_DIR','')}/{OSYNC_DIR}{RESET}", 11),
        kv("log", f"{DIM}{cfg.get('LOGFILE','—') or '—'}{RESET}", 11),
        kv("config", f"{DIM}{cfg.get('_configfile','')}{RESET}", 11),
    ]
    lines += box("paths", prows, width, CYAN)
    lines.append("")

    # safety net
    sd_days = cfg.get("SOFT_DELETE_DAYS", "?")
    cb_days = cfg.get("CONFLICT_BACKUP_DAYS", "?")
    li, ri = local.get("deleted", 0), (remote.get("deleted") if tgt.get("remote") else local.get("deleted"))
    lb, rb = local.get("backup", 0), (remote.get("backup") if tgt.get("remote") else local.get("backup"))
    sd_on = cfg.get("SOFT_DELETE", "true") == "true"
    cb_on = cfg.get("CONFLICT_BACKUP", "true") == "true"
    netrows = [
        kv("soft-delete", (f"{GREEN}on{RESET}" if sd_on else f"{GREY}off{RESET}") +
           f"   local {WHITE}{li}{RESET} / remote {WHITE}{ri if ri is not None else '—'}{RESET}   {GREY}kept {sd_days}d{RESET}", 14),
        kv("conflict-bkp", (f"{GREEN}on{RESET}" if cb_on else f"{GREY}off{RESET}") +
           f"   local {WHITE}{lb}{RESET} / remote {WHITE}{rb if rb is not None else '—'}{RESET}   {GREY}kept {cb_days}d{RESET}", 14),
        kv("winner", f"{GREEN}newest edit{RESET}   {GREY}· tie → {'local' if cfg.get('CONFLICT_PREVALANCE')=='initiator' else 'remote'} (same-timestamp only){RESET}", 14),
    ]
    excl = cfg.get("RSYNC_EXCLUDE_PATTERN", "").strip()
    if excl:
        netrows.append(kv("excludes", f"{YELLOW}{excl}{RESET}", 14))
    lines += box("safety net", netrows, width, MAGENTA)

    # pending (only if computed)
    if state.get("pending") is not None:
        p = state["pending"]
        lines.append("")
        prc = GREEN if p["total"] == 0 else YELLOW
        txt = (f"{GREEN}in sync — nothing pending{RESET}" if p["total"] == 0 else
               f"{YELLOW}{p['total']} pending{RESET}  "
               f"{GREY}↑ to remote{RESET} {p['tu']}u/{p['td']}d  {GREY}↓ to local{RESET} {p['iu']}u/{p['id']}d")
        lines += box("pending (dry-run)", [txt], width, prc)

    return "\n".join(lines)


# ── actions ─────────────────────────────────────────────────────────────────
def parse_log_last_run(logfile) -> dict | None:
    """Parse the last COMPLETED osync run from its log. The vendored osync is
    version-pinned (versions.env), so this format is stable and safe to rely on.
    Returns what the last sync actually moved, per direction, or None."""
    if not logfile:
        return None
    p = Path(os.path.expanduser(logfile))
    if not p.is_file():
        return None
    try:  # only the tail matters; logs can be large
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    begins = [m.start() for m in re.finditer(r"- osync [\d.]+ script begin\.", text)]
    if not begins:
        return None
    bounds = begins + [len(text)]
    block = None  # newest run that actually finished (the live one may still run)
    for i in range(len(begins) - 1, -1, -1):
        seg = text[bounds[i]:bounds[i + 1]]
        if "osync finished." in seg:
            block = seg
            break
    if block is None:
        return None

    def grab(pat):
        m = re.search(pat, block)
        return int(m.group(1)) if m else 0
    iu, tu = grab(r"Initiator has (\d+) updates"), grab(r"Target has (\d+) updates")
    idl, tdl = grab(r"Initiator has (\d+) deletions"), grab(r"Target has (\d+) deletions")
    return {"pushed": tu + tdl, "pulled": iu + idl, "push_del": tdl, "pull_del": idl,
            "total": iu + tu + idl + tdl,
            "ok": re.search(r"\b(CRITICAL|ERROR)\b", block) is None}


def compute_pending(cfg_path: Path) -> dict | None:
    """Authoritative osync dry-run: exact updates + deletions in each direction.
    Slower than the live probe counts (a full rsync comparison over ssh), so it's
    used for the deletion guard and the on-demand exact check, not live display."""
    rc, out = run([OSYNC_BIN, str(cfg_path), "--dry", "--summary", "--no-prefix"], timeout=180)
    def grab(pat):
        m = re.search(pat, out)
        return int(m.group(1)) if m else 0
    iu = grab(r"Initiator has (\d+) updates")
    tu = grab(r"Target has (\d+) updates")
    idl = grab(r"Initiator has (\d+) deletions")
    tdl = grab(r"Target has (\d+) deletions")
    if not re.search(r"osync finished", out):
        return None
    return {"iu": iu, "tu": tu, "id": idl, "td": tdl, "total": iu + tu + idl + tdl}


def pick_config(explicit: str | None) -> Path:
    if explicit:
        return Path(os.path.expanduser(explicit)).resolve()
    confs = sorted(CONFIG_HOME.glob("*.conf")) if CONFIG_HOME.is_dir() else []
    if not confs:
        sys.exit(f"osync-dash: no configs in {CONFIG_HOME} (use -c PATH)")
    if len(confs) == 1:
        return confs[0]
    if shutil.which("fzf") and sys.stdin.isatty():
        try:
            sel = subprocess.run(["fzf", "--prompt=osync config> ", "--height=40%"],
                                 input="\n".join(str(c) for c in confs),
                                 text=True, capture_output=True).stdout.strip()
            if sel:
                return Path(sel)
        except Exception:
            pass
    return confs[0]


def load(cfg_path: Path):
    cfg = parse_config(cfg_path)
    cfg["_configfile"] = str(cfg_path)
    tgt = parse_target(cfg.get("TARGET_SYNC_DIR", ""))
    return cfg, tgt


# ── host / config management ─────────────────────────────────────────────────
def list_configs() -> list[Path]:
    return sorted(CONFIG_HOME.glob("*.conf")) if CONFIG_HOME.is_dir() else []


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "host"


def target_uri(user: str, host: str, port: str, path: str) -> str:
    port = str(port or "22")
    p = path if path.startswith("/") else "/" + path
    return f"ssh://{user}@{host}:{port}/{p}"  # extra leading slash => absolute remote path


CONFIG_TEMPLATE = """\
###### osync configuration — generated by osync-dash
###### two-way sync: {initiator}  <->  {active_label}

[GENERAL]
CONFIG_FILE_REVISION=1.3.0
INSTANCE_ID="{instance}"
INITIATOR_SYNC_DIR="{initiator}"
TARGET_SYNC_DIR="{target_uri}"
SSH_RSA_PRIVATE_KEY="{key}"
SSH_PASSWORD_FILE=""
_REMOTE_TOKEN=SomeAlphaNumericToken9
CREATE_DIRS=true
LOGFILE="{logfile}"
MINIMUM_SPACE=10240
BANDWIDTH=0
SUDO_EXEC=false
RSYNC_EXECUTABLE=rsync
RSYNC_REMOTE_PATH=""
RSYNC_PATTERN_FIRST=include
RSYNC_INCLUDE_PATTERN=""
RSYNC_EXCLUDE_PATTERN="{exclude}"
RSYNC_INCLUDE_FROM=""
RSYNC_EXCLUDE_FROM=""
PATH_SEPARATOR_CHAR=";"
INITIATOR_CUSTOM_STATE_DIR=""
TARGET_CUSTOM_STATE_DIR=""

## osync-dash: endpoint bookkeeping (ignored by osync). Mode ts|ssh|both;
## in 'both', osync-dash prefers Tailscale and falls back to SSH.
DASH_TARGET_USER="{user}"
DASH_TARGET_PATH="{path}"
DASH_TS_HOST="{ts_host}"
DASH_TS_PORT="{ts_port}"
DASH_SSH_HOST="{ssh_host}"
DASH_SSH_PORT="{ssh_port}"
DASH_ENDPOINT_MODE="{mode}"

[REMOTE_OPTIONS]
SSH_COMPRESSION=false
SSH_IGNORE_KNOWN_HOSTS=false
SSH_CONTROLMASTER=false
REMOTE_HOST_PING=false
REMOTE_3RD_PARTY_HOSTS=""

[MISC_OPTIONS]
RSYNC_OPTIONAL_ARGS="{rsync_args}"
PRESERVE_PERMISSIONS=true
## owner/group off: replicas usually run as different users, and preserving
## them needs sudo on the target. Flip to true if both sides share the user.
PRESERVE_OWNER=false
PRESERVE_GROUP=false
PRESERVE_EXECUTABILITY=true
PRESERVE_ACL=false
PRESERVE_XATTR=false
COPY_SYMLINKS=false
KEEP_DIRLINKS=false
PRESERVE_HARDLINKS=false
CHECKSUM=false
RSYNC_COMPRESS=true
SOFT_MAX_EXEC_TIME=7200
HARD_MAX_EXEC_TIME=10600
KEEP_LOGGING=1801
MIN_WAIT=60
MAX_WAIT=7200

[BACKUP_DELETE_OPTIONS]
LOG_CONFLICTS=false
ALERT_CONFLICTS=false
CONFLICT_BACKUP=true
CONFLICT_BACKUP_MULTIPLE=false
CONFLICT_BACKUP_DAYS={conflict_backup_days}
CONFLICT_PREVALANCE=initiator
SOFT_DELETE=true
SOFT_DELETE_DAYS={soft_delete_days}
SKIP_DELETION=
SYNC_TYPE={sync_type}

[RESUME_OPTIONS]
RESUME_SYNC=true
RESUME_TRY=2
FORCE_STRANGER_LOCK_RESUME=false
PARTIAL=false
DELTA_COPIES=true

[ALERT_OPTIONS]
DESTINATION_MAILS=""
ALWAYS_SEND_MAILS=false
MAIL_BODY_CHARSET=""
SENDER_MAIL="alert@your.system.tld"
SMTP_SERVER=smtp.your.isp.tld
SMTP_PORT=25
SMTP_ENCRYPTION=none
SMTP_USER=
SMTP_PASSWORD=

[EXECUTION_HOOKS]
LOCAL_RUN_BEFORE_CMD=""
LOCAL_RUN_AFTER_CMD=""
REMOTE_RUN_BEFORE_CMD=""
REMOTE_RUN_AFTER_CMD=""
MAX_EXEC_TIME_PER_CMD_BEFORE=0
MAX_EXEC_TIME_PER_CMD_AFTER=0
STOP_ON_CMD_ERROR=true
RUN_AFTER_CMD_ON_ERROR=false
"""


DIRECTION_SYNC = {"send": "initiator2target", "receive": "target2initiator", "bidir": ""}
SYNC_DIRECTION = {v: k for k, v in DIRECTION_SYNC.items()}


def direction_of(cfg: dict) -> str:
    return SYNC_DIRECTION.get(cfg.get("SYNC_TYPE", "").strip(), "bidir")


def mode_of(cfg: dict) -> str:
    return cfg.get("DASH_ENDPOINT_MODE", "") or ""


def conf_text(*, instance, initiator_dir, user, path, key="~/.ssh/id_ed25519",
              ts_host="", ts_port="22", ssh_host="", ssh_port="22",
              mode="ts", direction="bidir", exclude="",
              soft_delete_days=30, conflict_backup_days=30) -> str:
    """Render a complete osync .conf for one connection (no disk writes)."""
    # INSTANCE_ID must be the name VERBATIM — osync keys all of its state/tree
    # files by it, so slugging (e.g. documents_remote -> documents-remote) would
    # orphan an existing replica and trigger a bogus full re-sync. Slug is only
    # safe for on-disk file names (log, generated .conf).
    slug = _slug(instance)
    # primary host: TS unless mode is ssh-only
    host, port = (ssh_host, ssh_port) if mode == "ssh" else (ts_host, ts_port)
    initiator = os.path.expanduser(initiator_dir)
    # push/pull (one-way) connections carry rsync --update: never clobber a file
    # that's newer on the receiver. That's what makes "newest change wins" true
    # in the mesh, where each device just pushes its own changes out. bidir keeps
    # osync's own conflict resolution instead.
    rsync_args = "--update" if direction in ("send", "receive") else ""
    return CONFIG_TEMPLATE.format(
        instance=instance, initiator=initiator,
        target_uri=target_uri(user, host, port, path),
        active_label=f"{user}@{host}:{path}",
        key=os.path.expanduser(key) if key else "",
        logfile=os.path.expanduser(f"~/.cache/osync/{slug}.log"),
        user=user, path=path, ts_host=ts_host, ts_port=ts_port or "22",
        ssh_host=ssh_host, ssh_port=ssh_port or "22", mode=mode,
        sync_type=DIRECTION_SYNC.get(direction, ""), exclude=exclude, rsync_args=rsync_args,
        soft_delete_days=soft_delete_days, conflict_backup_days=conflict_backup_days)


def create_config(*, instance, **kw) -> Path:
    """Write a new standalone osync .conf into CONFIG_HOME. Raises on collision."""
    CONFIG_HOME.mkdir(parents=True, exist_ok=True)
    cfg_path = CONFIG_HOME / f"{_slug(instance)}.conf"
    if cfg_path.exists():
        raise FileExistsError(f"{cfg_path} already exists")
    cfg_path.write_text(conf_text(instance=instance, **kw))
    Path(os.path.expanduser("~/.cache/osync")).mkdir(parents=True, exist_ok=True)
    return cfg_path


def _rewrite_conf(path: Path, updates: dict):
    """Replace/append `KEY="val"` lines in-place (values quoted)."""
    lines = path.read_text().splitlines()
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m and m.group(1) in updates:
            lines[i] = f'{m.group(1)}="{updates[m.group(1)]}"'
            done.add(m.group(1))
    for k, v in updates.items():
        if k not in done:
            lines.append(f'{k}="{v}"')
    path.write_text("\n".join(lines) + "\n")
    return done


def endpoints(cfg: dict) -> list[tuple[str, str, str]]:
    """Ordered (which, host, port) to try, from DASH_ENDPOINT_MODE. 'both' → TS
    first then SSH. Legacy configs with no mode fall back to their TARGET host."""
    mode = mode_of(cfg)
    ts = ("ts", cfg.get("DASH_TS_HOST", ""), cfg.get("DASH_TS_PORT", "22"))
    ssh = ("ssh", cfg.get("DASH_SSH_HOST", ""), cfg.get("DASH_SSH_PORT", "22"))
    order = {"ts": [ts], "ssh": [ssh], "both": [ts, ssh]}.get(mode)
    if not order:
        t = parse_target(cfg.get("TARGET_SYNC_DIR", ""))
        return [("current", t.get("host", ""), t.get("port", "22"))]
    return [(w, h, p) for (w, h, p) in order if h]


def available_modes(cfg: dict) -> list[str]:
    has_ts, has_ssh = bool(cfg.get("DASH_TS_HOST")), bool(cfg.get("DASH_SSH_HOST"))
    m = []
    if has_ts:
        m.append("ts")
    if has_ssh:
        m.append("ssh")
    if has_ts and has_ssh:
        m.append("both")
    return m


def cycle_mode(cfg_path: Path) -> tuple[str, str]:
    """Cycle DASH_ENDPOINT_MODE through the configured endpoints; point
    TARGET_SYNC_DIR at the new mode's primary host. Returns (mode, message)."""
    cfg = parse_config(cfg_path)
    modes = available_modes(cfg)
    if len(modes) < 2:
        return "", "only one endpoint configured (add another in setup)"
    cur = mode_of(cfg)
    new = modes[(modes.index(cur) + 1) % len(modes)] if cur in modes else modes[0]
    user = cfg.get("DASH_TARGET_USER", "") or parse_target(cfg.get("TARGET_SYNC_DIR", "")).get("user", "")
    path = cfg.get("DASH_TARGET_PATH", "") or parse_target(cfg.get("TARGET_SYNC_DIR", "")).get("path", "")
    which, host, port = endpoints({**cfg, "DASH_ENDPOINT_MODE": new})[0]
    _rewrite_conf(cfg_path, {"DASH_ENDPOINT_MODE": new,
                             "TARGET_SYNC_DIR": target_uri(user, host, port, path)})
    label = {"ts": "Tailscale", "ssh": "plain SSH", "both": "both (TS→SSH fallback)"}[new]
    return new, f"endpoint: {label}"


def set_direction(cfg_path: Path, direction: str) -> str:
    _rewrite_conf(cfg_path, {"SYNC_TYPE": DIRECTION_SYNC.get(direction, "")})
    return direction


# ── compose file (one TOML → many connections, docker-compose style) ─────────
MODE_CYCLE = {"ts": "Tailscale", "ssh": "plain SSH", "both": "both (TS→SSH fallback)"}


def parse_compose() -> tuple[dict, list[dict]]:
    """Return (defaults, connections) from the compose TOML. Empty if absent."""
    if not COMPOSE_FILE.exists():
        return dict(COMPOSE_DEFAULTS), []
    try:
        with open(COMPOSE_FILE, "rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError):
        return dict(COMPOSE_DEFAULTS), []
    defaults = {**COMPOSE_DEFAULTS, **(data.get("defaults") or {})}
    conns = [c for c in (data.get("connection") or []) if c.get("name")]
    return defaults, conns


def parse_settings() -> dict:
    if not COMPOSE_FILE.exists():
        return dict(SETTINGS_DEFAULTS)
    try:
        with open(COMPOSE_FILE, "rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError):
        return dict(SETTINGS_DEFAULTS)
    return {**SETTINGS_DEFAULTS, **(data.get("settings") or {})}


def save_settings(updates: dict) -> dict:
    s = {**parse_settings(), **updates}
    defaults, conns = parse_compose()
    dump_compose(defaults, conns, s)
    return s


def _toml_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


# key order for readable, stable output
_CONN_ORDER = ["name", "local", "remote", "direction", "mode", "user", "key",
               "ts_host", "ts_port", "ssh_host", "ssh_port", "exclude",
               "auto", "interval", "at"]


def dump_compose(defaults: dict, conns: list[dict], settings: dict | None = None) -> None:
    """Serialise the compose file (tool-managed; hand comments are not kept).
    `settings` defaults to the file's current [settings] so any writer preserves
    it without having to know about it."""
    if settings is None:
        settings = parse_settings()
    lines = ["# osync-dash compose — one file, many sync connections.",
             "# Managed by osync-dash (a add · t endpoint · d direction), editable by hand.",
             "# Each [[connection]] is a two-way osync job between this machine and a host.",
             "", "[settings]"]
    for k, v in settings.items():
        lines.append(f"{k} = {_toml_val(v)}")
    lines += ["", "[defaults]"]
    for k, v in defaults.items():
        lines.append(f"{k} = {_toml_val(v)}")
    for c in conns:
        lines.append("")
        lines.append("[[connection]]")
        keys = [k for k in _CONN_ORDER if k in c] + [k for k in c if k not in _CONN_ORDER]
        for k in keys:
            lines.append(f"{k} = {_toml_val(c[k])}")
    COMPOSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    COMPOSE_FILE.write_text("\n".join(lines) + "\n")


def add_connection(conn: dict) -> str:
    defaults, conns = parse_compose()
    if any(c.get("name") == conn["name"] for c in conns):
        raise FileExistsError(f"a connection named '{conn['name']}' already exists")
    conns.append(conn)
    dump_compose(defaults, conns)
    return conn["name"]


def update_connection(name: str, updates: dict) -> None:
    defaults, conns = parse_compose()
    for c in conns:
        if c.get("name") == name:
            c.update(updates)
    dump_compose(defaults, conns)


def remove_connection(name: str) -> None:
    defaults, conns = parse_compose()
    dump_compose(defaults, [c for c in conns if c.get("name") != name])
    if have_systemd():
        _write_units(name, "off", 15, "")  # stop + remove any auto units
    gen = GEN_DIR / f"{_slug(name)}.conf"
    if gen.exists():
        gen.unlink()


def _conn_fields(conn: dict, defaults: dict) -> dict:
    """Map a compose connection (+ defaults) to conf_text() kwargs."""
    g = lambda k, d="": conn.get(k, defaults.get(k, d))  # noqa: E731
    return dict(
        instance=conn["name"], initiator_dir=conn.get("local", "~"),
        user=g("user"), path=conn.get("remote", "~"),
        key=g("key", "~/.ssh/id_ed25519"),
        ts_host=conn.get("ts_host", ""), ts_port=str(conn.get("ts_port", "22")),
        ssh_host=conn.get("ssh_host", ""), ssh_port=str(conn.get("ssh_port", "22")),
        mode=conn.get("mode", "ts"), direction=conn.get("direction", "bidir"),
        exclude=conn.get("exclude", ""),
        soft_delete_days=g("soft_delete_days", 30),
        conflict_backup_days=g("conflict_backup_days", 30))


def materialize(conn: dict, defaults: dict) -> Path:
    """Write the real osync .conf for a connection into GEN_DIR; return its path."""
    GEN_DIR.mkdir(parents=True, exist_ok=True)
    p = GEN_DIR / f"{_slug(conn['name'])}.conf"
    p.write_text(conf_text(**_conn_fields(conn, defaults)))
    Path(os.path.expanduser("~/.cache/osync")).mkdir(parents=True, exist_ok=True)
    return p


def connection_to_cfg(conn: dict, defaults: dict) -> tuple[dict, dict]:
    """Materialise + parse one connection into the (cfg, tgt) pair the renderers
    and probes expect — identical shape to a hand-written osync .conf."""
    p = materialize(conn, defaults)
    cfg = parse_config(p)
    cfg["_configfile"] = str(p)
    cfg["_connection"] = conn["name"]
    cfg["_auto"] = conn.get("auto", "off")
    cfg["_interval"] = parse_duration(conn.get("interval", "15m"))["human"]
    cfg["_at"] = conn.get("at", "")
    tgt = parse_target(cfg.get("TARGET_SYNC_DIR", ""))
    return cfg, tgt


def load_all() -> list[tuple[str, dict, dict]]:
    """Every connection as (name, cfg, tgt). Migrates legacy confs on first run."""
    migrate_legacy()
    defaults, conns = parse_compose()
    out = []
    for conn in conns:
        try:
            cfg, tgt = connection_to_cfg(conn, defaults)
            out.append((conn["name"], cfg, tgt))
        except Exception:  # noqa: BLE001 — skip a broken entry, keep the rest
            continue
    return out


def cycle_mode_conn(name: str) -> tuple[str, str]:
    """Cycle a connection's endpoint mode (ts→ssh→both) in the compose file."""
    defaults, conns = parse_compose()
    conn = next((c for c in conns if c.get("name") == name), None)
    if not conn:
        return "", "connection not found"
    has_ts, has_ssh = bool(conn.get("ts_host")), bool(conn.get("ssh_host"))
    modes = [m for m, ok in (("ts", has_ts), ("ssh", has_ssh),
                             ("both", has_ts and has_ssh)) if ok]
    if len(modes) < 2:
        return "", "only one endpoint configured (add another in setup)"
    cur = conn.get("mode", "ts")
    new = modes[(modes.index(cur) + 1) % len(modes)] if cur in modes else modes[0]
    update_connection(name, {"mode": new})
    return new, f"endpoint: {MODE_CYCLE[new]}"


def set_direction_conn(name: str, direction: str) -> None:
    update_connection(name, {"direction": direction})


# ── notifications + deletion guard ───────────────────────────────────────────
# absolute path to the launcher, for systemd ExecStart (resolves through the
# ~/.local/bin symlink back to the repo).
LAUNCHER = Path(__file__).resolve().parent / "osync-dash"


def notify(title: str, body: str = "") -> None:
    """Best-effort desktop notification via the configured command. The command
    is a setting (default notify-send) so this stays portable — any tool taking
    `<cmd> [args] TITLE BODY` works (notify-send, dunstify, a wrapper script…)."""
    s = parse_settings()
    if not s.get("notify", True):
        return
    cmd = str(s.get("notify_cmd", "notify-send")).strip()
    if not cmd:
        return
    parts = cmd.split()
    exe = parts[0]
    if not (shutil.which(exe) or os.path.exists(os.path.expanduser(exe))):
        return
    try:
        run(parts + [title, body], timeout=6)
    except Exception:  # noqa: BLE001
        pass


def pending_deletions(pending: dict | None) -> int:
    return 0 if not pending else int(pending.get("id", 0)) + int(pending.get("td", 0))


def guarded_sync(name: str) -> int:
    """Run one sync for `name` — but first do a dry-run to (a) skip the real
    sync entirely when nothing has changed, so a periodic timer doesn't spin up
    a full osync pass every interval for no reason, and (b) refuse if it would
    propagate more deletions than `delete_guard`. This is what the systemd units
    call, so both protections apply to *automatic* syncs, not just manual ones.
    Returns an exit code (0 ok/idle · 3 blocked · other = osync's code)."""
    defaults, conns = parse_compose()
    conn = next((c for c in conns if c.get("name") == name), None)
    if not conn:
        sys.stderr.write(f"osync-dash: no connection '{name}'\n")
        return 2
    cfg, _ = connection_to_cfg(conn, defaults)
    pend = compute_pending(cfg["_configfile"])
    # in sync → don't do the work. (pend is None when the dry-run couldn't be
    # computed, e.g. target unreachable — fall through and let osync report it.)
    if pend is not None and pend["total"] == 0:
        sys.stderr.write(f"{name}: already in sync — nothing to do\n")
        return 0
    guard = int(parse_settings().get("delete_guard", 0) or 0)
    dels = pending_deletions(pend)
    if guard > 0 and dels > guard:
        msg = f"{name}: {dels} deletions exceed the guard of {guard} — sync skipped."
        notify("osync-dash · sync blocked", msg)
        sys.stderr.write(msg + " Raise delete_guard or sync manually to override.\n")
        return 3
    rc = subprocess.run([OSYNC_BIN, cfg["_configfile"], "--summary", "--no-prefix"]).returncode
    if rc != 0:
        notify("osync-dash · sync failed", f"{name}: osync exited with code {rc}")
    return rc


# ── automatic sync via systemd --user units ──────────────────────────────────
# off → manual only · change → osync --on-changes daemon (inotify) · periodic →
# oneshot service fired by a timer every `interval` minutes.
SYSTEMD_USER_DIR = Path(os.path.expanduser("~/.config/systemd/user"))
AUTO_MODES = ["off", "change", "periodic"]
AUTO_LABEL = {"off": "off (manual)", "change": "on file change", "periodic": "periodic"}

_SVC_CHANGE = """\
[Unit]
Description=osync-dash continuous sync — {name}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin
ExecStart={osync} {conf} --on-changes --silent
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""

_SVC_ONESHOT = """\
[Unit]
Description=osync-dash scheduled sync — {name}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin
ExecStart={launcher} --guarded-sync {name}
"""

_TIMER = """\
[Unit]
Description=osync-dash periodic sync timer — {name}

[Timer]
OnActiveSec={interval}
OnUnitActiveSec={interval}
Persistent=true

[Install]
WantedBy=timers.target
"""

_TIMER_CAL = """\
[Unit]
Description=osync-dash scheduled sync timer — {name}

[Timer]
OnCalendar={calendar}
Persistent=true

[Install]
WantedBy=timers.target
"""


_DUR_SECS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
_DUR_SYS = {"m": "min", "h": "h", "d": "d", "w": "w"}  # systemd time-span units


def _norm_unit(u: str) -> str:
    u = (u or "m").lower()
    if u in ("m", "min", "minute", "minutes"):
        return "m"
    if u in ("h", "hr", "hour", "hours"):
        return "h"
    if u in ("d", "day", "days"):
        return "d"
    if u in ("w", "week", "weeks"):
        return "w"
    return "m"


def parse_duration(spec, default="15m") -> dict:
    """Normalise a schedule spec into {store, systemd, seconds, human}.

    Accepts '15m', '90m', '6h', '24h', '2d', '10d', '1w' (or a bare int =
    minutes, for legacy compose files). Anything unparseable falls back to
    `default`."""
    s = str(spec).strip() if spec not in (None, "") else default
    m = re.match(r"^(\d+)\s*([A-Za-z]*)$", s)
    if not m:
        m = re.match(r"^(\d+)\s*([A-Za-z]*)$", default)
    n = max(1, int(m.group(1)))
    u = _norm_unit(m.group(2))
    # store stays compact ('15m', what the user types); human/systemd spell
    # minutes as 'min' so the display reads '15min', not '15mm'/'15m'.
    return {"store": f"{n}{u}", "systemd": f"{n}{_DUR_SYS[u]}",
            "seconds": n * _DUR_SECS[u], "human": f"{n}{_DUR_SYS[u]}"}


def _unit_base(name: str) -> str:
    return f"osync-dash-{_slug(name)}"


def have_systemd() -> bool:
    return bool(shutil.which("systemctl"))


def _systemctl(*args, timeout=15) -> tuple[int, str]:
    return run(["systemctl", "--user", *args], timeout=timeout)


def auto_mode(conn: dict) -> str:
    m = conn.get("auto", "off")
    return m if m in AUTO_MODES else "off"


def _write_units(name: str, mode: str, interval, conf: str, calendar: str = "") -> None:
    base = _unit_base(name)
    svc, tmr = SYSTEMD_USER_DIR / f"{base}.service", SYSTEMD_USER_DIR / f"{base}.timer"
    # tear the old pair down first, whatever it was
    _systemctl("disable", "--now", f"{base}.timer")
    _systemctl("disable", "--now", f"{base}.service")
    for f in (svc, tmr):
        if f.exists():
            f.unlink()
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    if mode == "change":
        svc.write_text(_SVC_CHANGE.format(name=name, osync=OSYNC_BIN, conf=conf))
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", f"{base}.service")
    elif mode == "periodic":
        svc.write_text(_SVC_ONESHOT.format(name=name, launcher=LAUNCHER))
        if calendar:
            tmr.write_text(_TIMER_CAL.format(name=name, calendar=calendar))
        else:
            tmr.write_text(_TIMER.format(name=name, interval=parse_duration(interval)["systemd"]))
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", f"{base}.timer")
    else:
        _systemctl("daemon-reload")


def set_auto(name: str, mode: str, interval=None, calendar: str | None = None) -> tuple[str, str]:
    """Persist a connection's auto mode in the compose file and (re)configure
    its systemd --user units. `calendar` (a systemd OnCalendar spec) takes
    precedence over `interval` for periodic mode. Returns (mode, message)."""
    if mode not in AUTO_MODES:
        mode = "off"
    defaults, conns = parse_compose()
    conn = next((c for c in conns if c.get("name") == name), None)
    if not conn:
        return "off", "connection not found"
    conn["auto"] = mode
    if calendar:
        conn["at"] = calendar.strip()
    elif interval:
        conn["interval"] = parse_duration(interval)["store"]
        conn.pop("at", None)  # switching back to an interval clears any calendar
    dump_compose(defaults, conns)
    if not have_systemd():
        return mode, f"auto = {mode} saved (no systemctl — start osync yourself)"
    if mode == "change" and not shutil.which("inotifywait"):
        return mode, "on-change needs inotify-tools (inotifywait) installed"
    conf = str(materialize(conn, defaults))
    at = conn.get("at", "") if calendar or conn.get("at") else ""
    try:
        _write_units(name, mode, conn.get("interval", "15m"), conf, calendar=at)
    except Exception as e:  # noqa: BLE001
        return mode, f"auto = {mode}, but unit setup failed: {e}"
    if mode != "periodic":
        when = ""
    elif at:
        when = f" (at {at})"
    else:
        when = f" (every {parse_duration(conn.get('interval', '15m'))['human']})"
    return mode, f"auto sync: {AUTO_LABEL[mode]}{when}"


def cycle_auto(name: str) -> tuple[str, str]:
    defaults, conns = parse_compose()
    conn = next((c for c in conns if c.get("name") == name), None)
    if not conn:
        return "off", "connection not found"
    cur = auto_mode(conn)
    return set_auto(name, AUTO_MODES[(AUTO_MODES.index(cur) + 1) % len(AUTO_MODES)])


_SPAN_UNITS = {"us": 1e-6, "ms": 1e-3, "s": 1, "sec": 1, "seconds": 1,
               "m": 60, "min": 60, "minutes": 60, "h": 3600, "hr": 3600, "hours": 3600,
               "d": 86400, "day": 86400, "days": 86400,
               "w": 604800, "week": 604800, "weeks": 604800}


def _parse_span(s) -> float | None:
    """Parse a systemd time span like '1d 8h 8min 15.04s' into seconds."""
    total, found = 0.0, False
    for tok in str(s).split():
        m = re.match(r"^([\d.]+)([a-z]+)$", tok)
        if m and m.group(2) in _SPAN_UNITS:
            total += float(m.group(1)) * _SPAN_UNITS[m.group(2)]
            found = True
    return total if found else None


def _show(unit: str, props: list[str]) -> dict:
    rc, out = _systemctl("show", unit, "-p", ",".join(props), timeout=6)
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            d[k] = v
    return d


def auto_status(name: str) -> dict:
    """Live systemd state for a connection's auto units, for the card display.
    {loaded, active, failed, next_secs, last_ok}. All best-effort / cheap."""
    st = {"loaded": False, "active": False, "failed": False,
          "next_ts": None, "last_ok": None}
    if not have_systemd():
        return st
    base = _unit_base(name)
    tm = _show(f"{base}.timer", ["LoadState", "ActiveState",
                                 "NextElapseUSecRealtime", "NextElapseUSecMonotonic"])
    sv = _show(f"{base}.service", ["LoadState", "ActiveState", "Result"])
    st["loaded"] = "loaded" in (tm.get("LoadState"), sv.get("LoadState"))
    st["active"] = "active" in (tm.get("ActiveState"), sv.get("ActiveState"))
    st["failed"] = ("failed" in (tm.get("ActiveState"), sv.get("ActiveState"))
                    or sv.get("Result") not in (None, "", "success"))
    res = sv.get("Result")
    if res:
        st["last_ok"] = res == "success"
    # interval timers (OnActiveSec/OnUnitActiveSec) report a MONOTONIC next
    # elapse, which systemctl prints as a span ("1d 8h 8min 15s") relative to
    # boot — parse it and diff against time.monotonic(). (Calendar timers use a
    # realtime date string we don't bother parsing; the countdown just hides.)
    raw = tm.get("NextElapseUSecMonotonic", "")
    mono = _parse_span(raw)
    if mono is None and raw.isdigit():
        mono = int(raw) / 1e6
    if mono is not None:
        # store an ABSOLUTE wall-clock target so the card can tick the countdown
        # down every second without re-querying systemd.
        st["next_ts"] = time.time() + max(0, mono - time.monotonic())
    return st


def migrate_legacy() -> bool:
    """One-time: if there's no compose yet but standalone *.conf files exist,
    fold them into the compose file. Leaves the old confs in place."""
    if COMPOSE_FILE.exists():
        return False
    legacy = list_configs()
    if not legacy:
        return False
    conns = []
    for p in legacy:
        c = parse_config(p)
        t = parse_target(c.get("TARGET_SYNC_DIR", ""))
        conns.append({
            "name": c.get("INSTANCE_ID", p.stem),
            "local": c.get("INITIATOR_SYNC_DIR", "~"),
            "remote": c.get("DASH_TARGET_PATH", "") or t.get("path", "~"),
            "direction": direction_of(c),
            "mode": c.get("DASH_ENDPOINT_MODE", "") or "ts",
            "user": c.get("DASH_TARGET_USER", "") or t.get("user", ""),
            "key": c.get("SSH_RSA_PRIVATE_KEY", "") or "~/.ssh/id_ed25519",
            "ts_host": c.get("DASH_TS_HOST", "") or (t.get("host", "") if not c.get("DASH_SSH_HOST") else ""),
            "ts_port": int(c.get("DASH_TS_PORT", "22") or 22),
            "ssh_host": c.get("DASH_SSH_HOST", ""),
            "ssh_port": int(c.get("DASH_SSH_PORT", "22") or 22),
            "exclude": c.get("RSYNC_EXCLUDE_PATTERN", ""),
        })
    dump_compose(dict(COMPOSE_DEFAULTS), conns)
    return True


# ── directory listing (for the add-host autocomplete) ────────────────────────
def list_local_dirs(base: str) -> list[str]:
    b = Path(os.path.expanduser(base or "~"))
    if not b.is_dir():
        b = b.parent if b.parent.is_dir() else Path.home()
    try:
        return sorted((e.name for e in os.scandir(b) if e.is_dir()), key=str.lower)
    except OSError:
        return []


def list_remote_dirs(user, host, port, key, base) -> list[str]:
    """List subdirectory names of `base` on the remote (for autocomplete)."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "ControlPath=none", "-p", str(port or "22")]
    if key and os.path.exists(os.path.expanduser(key)):
        cmd += ["-i", os.path.expanduser(key)]
    b = base or "~"
    # ls -1Ap: one per line, dirs get trailing /. keep only dirs.
    remote_cmd = f'ls -1Ap {b} 2>/dev/null | grep "/$"'
    rc, out = run(cmd + [f"{user}@{host}", remote_cmd], timeout=12)
    if rc != 0:
        return []
    return [ln[:-1] for ln in out.splitlines() if ln.endswith("/")]


def ts_devices() -> list[dict]:
    """Tailscale peers as {name, dns, ip, os, online}, online + linux first."""
    m = tailscale_map()
    if not shutil.which("tailscale"):
        return []
    rc, out = run(["tailscale", "status", "--json"], timeout=6)
    if rc != 0 or "{" not in out:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(out[out.index("{"):])
    except (json.JSONDecodeError, ValueError):
        return []
    devs = []
    for n in (data.get("Peer") or {}).values():
        dns = (n.get("DNSName") or "").rstrip(".")
        ip4 = next((ip for ip in (n.get("TailscaleIPs") or []) if ":" not in ip), "")
        devs.append({"name": dns.split(".")[0] if dns else n.get("HostName", ""),
                     "dns": dns, "ip": ip4, "os": n.get("OS", ""),
                     "online": bool(n.get("Online"))})
    devs.sort(key=lambda d: (not d["online"], d["os"] != "linux", d["name"].lower()))
    return devs


# ── mesh beacons: advertise this node's pushes so peers can see them ─────────
# A push connection drops a small beacon in the target's ~/.config/osync/incoming
# over ssh. The receiving node reads those to show "← from <node>" cards — a
# decentralised netmap, no central registry. Liveness comes from osync's own
# target-side state mtime, so beacons stay static metadata.
INCOMING_DIR = CONFIG_HOME / "incoming"
_REMOTE_INCOMING = "~/.config/osync/incoming"


def _beacon_name(node: str, conn_name: str) -> str:
    return f"{_slug(node)}--{_slug(conn_name)}.toml"


def node_name() -> str:
    return parse_settings().get("node") or socket.gethostname()


def advertise(conn: dict, defaults: dict) -> bool:
    """Drop/refresh a beacon on a push target. Best-effort; push only."""
    if conn.get("direction", "bidir") != "send":
        return False
    try:
        cfg, tgt = connection_to_cfg(conn, defaults)
    except Exception:  # noqa: BLE001
        return False
    if not tgt.get("remote"):
        return False
    body = (f'from_node = {_toml_val(node_name())}\n'
            f'connection = {_toml_val(conn["name"])}\n'
            f'source_dir = {_toml_val(os.path.expanduser(conn.get("local", "")))}\n'
            f'dir = {_toml_val(tgt.get("path", ""))}\n'
            'direction = "send"\n')
    name = _beacon_name(node_name(), conn["name"])
    cmd = ssh_prefix(cfg, tgt) + [f"mkdir -p {_REMOTE_INCOMING} && cat > {_REMOTE_INCOMING}/{name}"]
    try:
        return subprocess.run(cmd, input=body, text=True, capture_output=True,
                              timeout=15).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def unadvertise(conn: dict, defaults: dict) -> None:
    try:
        cfg, tgt = connection_to_cfg(conn, defaults)
    except Exception:  # noqa: BLE001
        return
    if not tgt.get("remote"):
        return
    name = _beacon_name(node_name(), conn["name"])
    try:
        subprocess.run(ssh_prefix(cfg, tgt) + [f"rm -f {_REMOTE_INCOMING}/{name}"],
                       capture_output=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        pass


def advertise_all() -> None:
    defaults, conns = parse_compose()
    for c in conns:
        if c.get("direction") == "send":
            advertise(c, defaults)


def incoming_beacons() -> list[dict]:
    if not INCOMING_DIR.is_dir():
        return []
    out = []
    for p in sorted(INCOMING_DIR.glob("*.toml")):
        try:
            with open(p, "rb") as f:
                b = tomllib.load(f)
        except (tomllib.TOMLDecodeError, OSError):
            continue
        if b.get("from_node") and b.get("dir"):
            out.append(b)
    return out


def probe_incoming(beacon: dict) -> dict:
    """Status of one incoming push, seen from THIS (receiving) node's side."""
    d = Path(os.path.expanduser(beacon.get("dir", "")))
    inst = beacon.get("connection", "")
    last_ts = _state_mtime(d / OSYNC_DIR / "state" / f"target-last-action-{inst}")
    info = probe_local(d) if d.is_dir() else {"reach": False, "files": None, "size": None}
    return {"from_node": beacon.get("from_node", "?"), "connection": inst,
            "dir": str(d), "source_dir": beacon.get("source_dir", ""),
            "last_ts": last_ts, "files": info.get("files"), "size": info.get("size"),
            "reach": info.get("reach", False)}


def gather(cfg, tgt, local_only=False):
    sync_dir = Path(os.path.expanduser(cfg.get("INITIATOR_SYNC_DIR", "")))
    # baseline = last successful sync time; both replicas were identical then, so
    # anything with a newer mtime is a live change waiting to sync (git-style).
    inst = cfg.get("INSTANCE_ID", "")
    baseline = _state_mtime(sync_dir / OSYNC_DIR / "state" / f"initiator-last-action-{inst}")
    local = probe_local(sync_dir, baseline)
    remote = {"reach": False, "err": "skipped"}
    if tgt.get("remote") and not local_only:
        user = tgt.get("user", "")
        path = tgt.get("path", "")
        last = None
        for which, host, port in endpoints(cfg):
            r = probe_remote(cfg, {**tgt, "host": host, "port": port}, baseline)
            last = r
            if r.get("reach"):
                r["endpoint_used"] = which
                # keep the config pointed at the reachable endpoint for osync runs
                if host != tgt.get("host") and cfg.get("_configfile"):
                    _rewrite_conf(Path(cfg["_configfile"]),
                                  {"TARGET_SYNC_DIR": target_uri(user, host, port, path)})
                    cfg["TARGET_SYNC_DIR"] = target_uri(user, host, port, path)
                    tgt["host"], tgt["port"] = host, port
                remote = r
                break
        else:
            remote = last or remote

    state = probe_state(cfg, sync_dir)
    state["last_run"] = parse_log_last_run(cfg.get("LOGFILE"))
    ts = tailscale_map()
    if ts:
        local["ts"] = ts.get((local.get("host") or "").lower())
        if tgt.get("remote"):
            ident = ts.get((remote.get("host") or "").lower())
            if not ident:
                ip = resolve_ssh_host(tgt.get("host", ""))
                ident = ts.get(ip) if ip else None
            remote["ts"] = ident
            if not remote.get("host") and ident:
                remote["host"] = ident.get("name")
    return state, local, remote



def main():
    args = sys.argv[1:]
    o = {"config": None, "watch": False, "interval": 6, "check": False,
         "sync": False, "log": False, "local_only": False, "fast": False,
         "print": False}
    passthru = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            print(__doc__); return
        elif a in ("-c", "--config"):
            i += 1; o["config"] = args[i]
        elif a == "--guarded-sync":
            i += 1
            sys.exit(guarded_sync(args[i]))  # systemd ExecStart entry point
        elif a in ("-w", "--watch"):
            o["watch"] = True
        elif a in ("-p", "--print", "--once"):
            o["print"] = True
        elif a in ("-i", "--interval"):
            i += 1; o["interval"] = float(args[i])
        elif a in ("-f", "--fast", "--no-check"):
            o["fast"] = True
        elif a == "--check":
            o["check"] = True
        elif a == "--sync":
            o["sync"] = True
        elif a == "--log":
            o["log"] = True
        elif a == "--local-only":
            o["local_only"] = True
        elif a == "--no-color":
            global _COLOR
            _COLOR = False
        elif a == "--":
            passthru = args[i + 1:]; break
        else:
            sys.exit(f"osync-dash: unknown option {a} (see --help)")
        i += 1

    # Connections come from the compose file. -c NAME|PATH narrows to one:
    # a name matches a compose connection; a path loads a standalone .conf.
    if o["config"] and os.path.exists(os.path.expanduser(o["config"])):
        cfg, tgt = load(Path(os.path.expanduser(o["config"])))
        conns = [(cfg.get("INSTANCE_ID", "?"), cfg, tgt)]
    else:
        conns = load_all()
        if o["config"]:
            conns = [c for c in conns if c[0] == o["config"]] or conns
    if not conns:
        sys.exit(f"osync-dash: no connections. Add one in {COMPOSE_FILE} "
                 f"or launch the TUI and press 'a'.")

    def one_shot(cfg, tgt, pending=False):
        state, local, remote = gather(cfg, tgt, o["local_only"])
        if pending:
            print(f"{GREY}computing pending changes (dry run)…{RESET}", flush=True)
            state["pending"] = compute_pending(cfg["_configfile"])
        return render(cfg, tgt, state, local, remote, term_width())

    if o["log"]:
        _, cfg, _ = conns[0]
        logf = os.path.expanduser(cfg.get("LOGFILE", ""))
        if logf and os.path.exists(logf):
            subprocess.run([os.environ.get("PAGER", "less"), "+G", logf])
        else:
            sys.exit("osync-dash: no log file found")
        return

    # --sync is a one-off action on the first (or -c selected) connection.
    if o["sync"]:
        name, cfg, tgt = conns[0]
        print(f"{CYAN}▶ running osync {name}…{RESET}\n")
        subprocess.run([OSYNC_BIN, cfg["_configfile"], "--summary", "--no-prefix"] + passthru)
        print("\n" + one_shot(cfg, tgt, pending=not o["fast"]))
        return

    # osync_core only does the one-shot render; the interactive TUI lives in
    # osync_tui.py (Textual) and is launched by the osync-dash wrapper.
    for i, (_, cfg, tgt) in enumerate(conns):
        if i:
            print()
        print(one_shot(cfg, tgt, pending=(o["check"] or not o["fast"])))


if __name__ == "__main__":
    main()
