#!/usr/bin/env python3
"""osync-dash — terminal dashboard for osync sync jobs (core / one-shot renderer).

Reads an osync config, probes both replicas (local + remote over ssh), and
renders health, devices, paths and the soft-delete/backup safety net. This
core is pure Python stdlib; the interactive TUI (osync_tui.py) adds Textual.

Usage:
    osync-dash [-c CONFIG] [options]

Run with no arguments for the interactive Textual TUI (auto-refreshing,
keyboard-driven). In the TUI:  r refresh · c check pending · s sync · l log
· q quit.

Piped or with --print it falls back to a one-shot render of the same panels.

Options:
    -c, --config PATH   osync .conf to inspect. Default: auto-discover in
                        ~/.config/osync (fzf-picks if more than one).
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
from pathlib import Path

OSYNC_BIN = shutil.which("osync.sh") or "/usr/local/bin/osync.sh"
OSYNC_DIR = ".osync_workdir"
CONFIG_HOME = Path(os.path.expanduser("~/.config/osync"))
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


def probe_local(sync_dir: Path) -> dict:
    d = {"reach": sync_dir.is_dir(), "rsync": bool(shutil.which("rsync")),
         "host": socket.gethostname()}
    if not d["reach"]:
        return d
    files = size = 0
    for root, dirs, fs in os.walk(sync_dir):
        if OSYNC_DIR in Path(root).parts:
            dirs[:] = [x for x in dirs if x != OSYNC_DIR]
            continue
        if OSYNC_DIR in dirs:
            dirs.remove(OSYNC_DIR)
        for f in fs:
            fp = Path(root) / f
            try:
                size += fp.stat().st_size
                files += 1
            except OSError:
                pass
    d["files"], d["size"] = files, size
    try:
        st = os.statvfs(sync_dir)
        d["free"] = st.f_bavail * st.f_frsize
        d["disk_total"] = st.f_blocks * st.f_frsize
        d["disk_used"] = (st.f_blocks - st.f_bfree) * st.f_frsize
    except OSError:
        d["free"] = d["disk_total"] = d["disk_used"] = None
    wd = sync_dir / OSYNC_DIR
    d["deleted"] = sum(1 for _ in (wd / "deleted").rglob("*") if _.is_file()) if (wd / "deleted").is_dir() else 0
    d["backup"] = sum(1 for _ in (wd / "backup").rglob("*") if _.is_file()) if (wd / "backup").is_dir() else 0
    return d


REMOTE_PROBE = r'''
p=%s
echo "REACH=1"
command -v rsync >/dev/null && echo "RSYNC=1" || echo "RSYNC=0"
echo "HOST=$(hostname 2>/dev/null)"
if [ -d "$p" ]; then
  echo "FILES=$(find "$p" -type f -not -path '*/.osync_workdir/*' 2>/dev/null | wc -l)"
  echo "SIZE=$(du -sb "$p" --exclude=.osync_workdir 2>/dev/null | cut -f1)"
  df -PB1 "$p" 2>/dev/null | awk 'NR==2{print "DISK_TOTAL="$2"\nDISK_USED="$3"\nFREE="$4}'
  echo "DELETED=$(find "$p/.osync_workdir/deleted" -type f 2>/dev/null | wc -l)"
  echo "BACKUP=$(find "$p/.osync_workdir/backup" -type f 2>/dev/null | wc -l)"
  echo "DIR=1"
else
  echo "DIR=0"
fi
'''


def probe_remote(cfg: dict, tgt: dict) -> dict:
    import shlex
    script = REMOTE_PROBE % shlex.quote(tgt["path"])
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
    for k in ("files", "size", "free", "deleted", "backup", "disk_total", "disk_used"):
        try:
            d[k] = int(d[k])
        except (KeyError, ValueError):
            d[k] = None
    d["dir_exists"] = d.get("dir") == "1"
    return d


def probe_state(cfg: dict, sync_dir: Path) -> dict:
    inst = cfg.get("INSTANCE_ID", "")
    sd = sync_dir / OSYNC_DIR / "state"
    d = {"instance": inst, "running": False, "last_ts": None,
         "init_action": None, "tgt_action": None, "resume": None}
    la = sd / f"initiator-last-action-{inst}"
    if la.exists():
        d["last_ts"] = la.stat().st_mtime
        d["init_action"] = la.read_text(errors="replace").strip()
    ta = sd / f"target-last-action-{inst}"
    if ta.exists():
        d["tgt_action"] = ta.read_text(errors="replace").strip()
    rc = sd / f"resume-count-{inst}"
    if rc.exists():
        d["resume"] = rc.read_text(errors="replace").strip()
    # running? lock file in state dir or a live osync process for this config
    if sd.is_dir() and any(p.name.endswith(".lock") or p.name == "lock" for p in sd.iterdir()):
        d["running"] = True
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
    drows = [hdr] + dev_lines("initiator", local) + dev_lines("target", tgt_dev)
    if tgt.get("remote") and not remote.get("reach", False):
        drows.append(f"{YELLOW}  ! target: {remote.get('err', 'unreachable')}{RESET}")
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
        kv("result", f"{res_c}{res}{RESET}   target: {res_c if state.get('tgt_action')=='synced' else GREY}{state.get('tgt_action') or '—'}{RESET}"),
        kv("resume", resume_txt),
        kv("running", f"{BLUE}yes{RESET}" if state["running"] else f"{GREY}no{RESET}"),
    ]
    lines += box("sync state", srows, width, col)
    lines.append("")

    # paths
    prows = [
        kv("initiator", f"{WHITE}{cfg.get('INITIATOR_SYNC_DIR','?')}{RESET}", 11),
        kv("target", f"{WHITE}{tgt.get('user','')}@{tgt.get('host','')}:{tgt.get('path','?')}{RESET}" if tgt.get("remote") else cfg.get("TARGET_SYNC_DIR", "?"), 11),
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
           f"   init {WHITE}{li}{RESET} / target {WHITE}{ri if ri is not None else '—'}{RESET}   {GREY}kept {sd_days}d{RESET}", 14),
        kv("conflict-bkp", (f"{GREEN}on{RESET}" if cb_on else f"{GREY}off{RESET}") +
           f"   init {WHITE}{lb}{RESET} / target {WHITE}{rb if rb is not None else '—'}{RESET}   {GREY}kept {cb_days}d{RESET}", 14),
        kv("winner", f"{GREEN}newest mtime{RESET}   {GREY}· tie → {cfg.get('CONFLICT_PREVALANCE','initiator')} (same-timestamp only){RESET}", 14),
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
               f"{GREY}→target{RESET} {p['tu']}u/{p['td']}d  {GREY}→init{RESET} {p['iu']}u/{p['id']}d")
        lines += box("pending (dry-run)", [txt], width, prc)

    return "\n".join(lines)


# ── actions ─────────────────────────────────────────────────────────────────
def compute_pending(cfg_path: Path) -> dict | None:
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
RSYNC_EXCLUDE_PATTERN=""
RSYNC_INCLUDE_FROM=""
RSYNC_EXCLUDE_FROM=""
PATH_SEPARATOR_CHAR=";"
INITIATOR_CUSTOM_STATE_DIR=""
TARGET_CUSTOM_STATE_DIR=""

## osync-dash: dual endpoint bookkeeping (ignored by osync). Toggle rewrites
## TARGET_SYNC_DIR between the Tailscale and the plain-SSH host.
DASH_TARGET_USER="{user}"
DASH_TARGET_PATH="{path}"
DASH_TS_HOST="{ts_host}"
DASH_TS_PORT="{ts_port}"
DASH_SSH_HOST="{ssh_host}"
DASH_SSH_PORT="{ssh_port}"
DASH_ACTIVE="{active}"

[REMOTE_OPTIONS]
SSH_COMPRESSION=false
SSH_IGNORE_KNOWN_HOSTS=false
SSH_CONTROLMASTER=false
REMOTE_HOST_PING=false
REMOTE_3RD_PARTY_HOSTS=""

[MISC_OPTIONS]
RSYNC_OPTIONAL_ARGS=""
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
CONFLICT_BACKUP_DAYS=30
CONFLICT_PREVALANCE=initiator
SOFT_DELETE=true
SOFT_DELETE_DAYS=30
SKIP_DELETION=
SYNC_TYPE=

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


def create_config(*, instance, initiator_dir, user, path, key="~/.ssh/id_ed25519",
                   ts_host="", ts_port="22", ssh_host="", ssh_port="22",
                   active="ts") -> Path:
    """Write a new osync .conf for a host. Returns the path. Raises on collision."""
    CONFIG_HOME.mkdir(parents=True, exist_ok=True)
    inst = _slug(instance)
    cfg_path = CONFIG_HOME / f"{inst}.conf"
    if cfg_path.exists():
        raise FileExistsError(f"{cfg_path} already exists")
    host = ts_host if active == "ts" else ssh_host
    port = ts_port if active == "ts" else ssh_port
    initiator = os.path.expanduser(initiator_dir)
    text = CONFIG_TEMPLATE.format(
        instance=inst, initiator=initiator,
        target_uri=target_uri(user, host, port, path),
        active_label=f"{user}@{host}:{path}",
        key=os.path.expanduser(key) if key else "",
        logfile=os.path.expanduser(f"~/.cache/osync/{inst}.log"),
        user=user, path=path, ts_host=ts_host, ts_port=ts_port or "22",
        ssh_host=ssh_host, ssh_port=ssh_port or "22", active=active)
    cfg_path.write_text(text)
    Path(os.path.expanduser("~/.cache/osync")).mkdir(parents=True, exist_ok=True)
    return cfg_path


def _rewrite_conf(path: Path, updates: dict):
    """Replace `KEY="val"` lines in-place (values quoted); keys must already exist."""
    lines = path.read_text().splitlines()
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m and m.group(1) in updates:
            k = m.group(1)
            lines[i] = f'{k}="{updates[k]}"'
            done.add(k)
    path.write_text("\n".join(lines) + "\n")
    return done


def endpoint_of(cfg: dict) -> str:
    """'ts' | 'ssh' | '' — which endpoint the config is currently pointed at."""
    return cfg.get("DASH_ACTIVE", "")


def has_dual_endpoints(cfg: dict) -> bool:
    return bool(cfg.get("DASH_TS_HOST") and cfg.get("DASH_SSH_HOST"))


def toggle_endpoint(cfg_path: Path) -> tuple[str, str]:
    """Flip a config between its Tailscale and plain-SSH endpoint.

    Returns (new_active, message). Requires DASH_* metadata (configs created by
    osync-dash have it); returns ('', reason) if it can't toggle.
    """
    cfg = parse_config(cfg_path)
    if not has_dual_endpoints(cfg):
        return "", "no alternate endpoint (add SSH + Tailscale hosts in setup)"
    cur = cfg.get("DASH_ACTIVE", "ts")
    new = "ssh" if cur == "ts" else "ts"
    user = cfg.get("DASH_TARGET_USER") or (parse_target(cfg.get("TARGET_SYNC_DIR", "")).get("user") or "")
    path = cfg.get("DASH_TARGET_PATH") or (parse_target(cfg.get("TARGET_SYNC_DIR", "")).get("path") or "")
    host = cfg["DASH_TS_HOST"] if new == "ts" else cfg["DASH_SSH_HOST"]
    port = cfg.get("DASH_TS_PORT", "22") if new == "ts" else cfg.get("DASH_SSH_PORT", "22")
    _rewrite_conf(cfg_path, {
        "TARGET_SYNC_DIR": target_uri(user, host, port, path),
        "DASH_ACTIVE": new,
    })
    label = "Tailscale" if new == "ts" else "plain SSH"
    return new, f"switched to {label}: {user}@{host}"


def gather(cfg, tgt, local_only=False):
    sync_dir = Path(os.path.expanduser(cfg.get("INITIATOR_SYNC_DIR", "")))
    local = probe_local(sync_dir)
    remote = probe_remote(cfg, tgt) if (tgt.get("remote") and not local_only) else {"reach": False, "err": "skipped"}
    state = probe_state(cfg, sync_dir)

    ts = tailscale_map()
    if ts:
        local["ts"] = ts.get((local.get("host") or "").lower())
        if tgt.get("remote"):
            ident = ts.get((remote.get("host") or "").lower())
            if not ident:  # ssh probe may have failed; match via the alias's IP
                ip = resolve_ssh_host(tgt.get("host", ""))
                ident = ts.get(ip) if ip else None
            remote["ts"] = ident
            if not remote.get("host") and ident:  # surface a name even when offline
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

    cfg_path = pick_config(o["config"])
    cfg, tgt = load(cfg_path)

    if o["log"]:
        logf = os.path.expanduser(cfg.get("LOGFILE", ""))
        if logf and os.path.exists(logf):
            subprocess.run([os.environ.get("PAGER", "less"), "+G", logf])
        else:
            sys.exit("osync-dash: no log file found")
        return

    def one_shot(pending=False):
        state, local, remote = gather(cfg, tgt, o["local_only"])
        if pending:
            print(f"{GREY}computing pending changes (dry run)…{RESET}", flush=True)
            state["pending"] = compute_pending(cfg_path)
        return render(cfg, tgt, state, local, remote, term_width())

    # --sync is a one-off action: run, print the result, exit.
    if o["sync"]:
        print(f"{CYAN}▶ running osync {cfg.get('INSTANCE_ID','')}…{RESET}\n")
        subprocess.run([OSYNC_BIN, str(cfg_path), "--summary", "--no-prefix"] + passthru)
        print("\n" + one_shot(pending=not o["fast"]))
        return

    # osync_core only does the one-shot render; the interactive TUI lives in
    # osync_tui.py (Textual) and is launched by the osync-dash wrapper.
    print(one_shot(pending=(o["check"] or not o["fast"])))


if __name__ == "__main__":
    main()
