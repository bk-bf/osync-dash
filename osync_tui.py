#!/usr/bin/env python3
"""osync-dash — Textual TUI front-end.

Launched by the `osync-dash` wrapper under the project virtualenv (Textual is
the only third-party dependency). All data gathering lives in osync_core; this
module is purely the interactive presentation layer.
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import osync_core as core  # noqa: E402

from rich import box  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402
from textual import work  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.containers import Horizontal, VerticalScroll  # noqa: E402
from textual.widgets import Footer, Header, Static  # noqa: E402

# health colour-name -> Rich colour (used inside Text/Table renderables)
RC = {"green": "green3", "yellow": "yellow", "red": "red3", "blue": "deep_sky_blue1",
      "cyan": "cyan", "grey": "grey62", "white": "white", "magenta": "plum2"}
# health colour-name -> Textual-valid border colour (Textual's CSS parser does
# NOT accept Rich names like grey50/plum2, so borders use hex).
BORDER = {"green": "#5fd75f", "yellow": "#d7af5f", "red": "#ff5f5f", "blue": "#5fafff",
          "cyan": "#5fd7d7", "grey": "#808080", "white": "#d0d0d0", "magenta": "#d787d7"}


def _n(v, dash="—"):
    return dash if v is None else str(v)


def _hsize(v):
    return core.humansize(v) if v is not None else "—"


# ── renderables ──────────────────────────────────────────────────────────────
def health_render(cfg, state, local, remote) -> Text:
    st, cname = core.health(state, remote, local)
    c = RC.get(cname, "white")
    age = core.human_age(core.time.time() - state["last_ts"] if state["last_ts"] else None)
    stype = cfg.get("SYNC_TYPE", "").strip() or "bidirectional"
    t = Text()
    t.append("● ", style=f"bold {c}")
    t.append(st, style=f"bold {c}")
    if state["running"]:
        t.append("   sync in progress…", style="deep_sky_blue1")
    t.append(f"      last sync ", style="grey62")
    t.append(age, style="white")
    t.append("      mode ", style="grey62")
    t.append(stype, style="white")
    return t


def devices_render(cfg, tgt, local, remote) -> Table:
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, show_edge=False)
    t.add_column("device", justify="left", header_style="grey50")
    t.add_column("host", justify="left", header_style="grey50", ratio=1)  # absorbs slack
    t.add_column("up", justify="center", header_style="grey50")
    t.add_column("rsync", justify="center", header_style="grey50")
    t.add_column("files", justify="right", header_style="grey50")
    t.add_column("size", justify="right", header_style="grey50")
    t.add_column("free", justify="right", header_style="grey50")

    def row(role, r):
        reach = r.get("reach", False)
        up = Text("●", style="green3" if reach else "red3")
        rs = "✓" if r.get("rsync") else ("—" if not reach else "✗")
        host = r.get("host") or (r.get("ts") or {}).get("name") or "—"
        hcell = Text(host, style="white")
        ts = r.get("ts")
        if ts and ts.get("name"):
            ip = ts.get("ip") or ""
            hcell.append(f"\n↳ {ts['name']}" + (f"  ·  {ip}" if ip else ""), style="dim")
        t.add_row(Text(role, style="cyan"), hcell, up, rs,
                  _n(r.get("files")), _hsize(r.get("size")), _hsize(r.get("free")))

    row("initiator", local)
    row("target", remote if tgt.get("remote") else local)
    if tgt.get("remote") and not remote.get("reach", False):
        t.add_row(Text("!", style="yellow"),
                  Text(str(remote.get("err", "unreachable")), style="yellow"), "", "", "", "", "")
    return t


def _kv(rows) -> Table:
    t = Table(box=None, expand=True, show_header=False, pad_edge=False)
    t.add_column(style="grey62", no_wrap=True, width=13)
    t.add_column(ratio=1)  # value column absorbs the panel width, keeps key snug
    for k, v in rows:
        t.add_row(k, v)
    return t


def state_render(cfg, state) -> Table:
    res = state.get("init_action") or "—"
    rc = "green3" if res == "synced" else ("grey62" if res == "—" else "red3")
    resume = state.get("resume")
    lastrun = (core.time.strftime("%Y-%m-%d %H:%M", core.time.localtime(state["last_ts"]))
               if state["last_ts"] else "never")
    age = core.human_age(core.time.time() - state["last_ts"] if state["last_ts"] else None)
    ta = state.get("tgt_action")
    result = Text(res, style=rc)
    result.append("   target: ", style="grey62")
    result.append(_n(ta), style="green3" if ta == "synced" else "grey62")
    resume_t = (Text("0 (clean)", style="green3") if resume in ("0", None)
                else Text(f"{resume} (retried)", style="red3"))
    return _kv([
        ("last run", Text(f"{lastrun}  ({age} ago)", style="white")),
        ("result", result),
        ("resume", resume_t),
        ("running", Text("yes", style="deep_sky_blue1") if state["running"] else Text("no", style="grey62")),
    ])


def paths_render(cfg, tgt) -> Table:
    tgt_s = (f"{tgt.get('user','')}@{tgt.get('host','')}:{tgt.get('path','?')}"
             if tgt.get("remote") else cfg.get("TARGET_SYNC_DIR", "?"))
    return _kv([
        ("initiator", Text(cfg.get("INITIATOR_SYNC_DIR", "?"), style="white")),
        ("target", Text(tgt_s, style="white")),
        ("workdir", Text(f"{cfg.get('INITIATOR_SYNC_DIR','')}/{core.OSYNC_DIR}", style="dim")),
        ("log", Text(cfg.get("LOGFILE", "—") or "—", style="dim")),
        ("config", Text(cfg.get("_configfile", ""), style="dim")),
    ])


def safety_render(cfg, tgt, local, remote) -> Table:
    li = local.get("deleted", 0)
    ri = remote.get("deleted") if tgt.get("remote") else local.get("deleted")
    lb = local.get("backup", 0)
    rb = remote.get("backup") if tgt.get("remote") else local.get("backup")
    sd_on = cfg.get("SOFT_DELETE", "true") == "true"
    cb_on = cfg.get("CONFLICT_BACKUP", "true") == "true"

    def line(on, li, ri, days):
        t = Text("on  ", style="green3") if on else Text("off ", style="grey62")
        t.append(f"init {li} / target {_n(ri)}", style="default")
        t.append(f"   kept {days}d", style="grey62")
        return t

    winner = Text("newest mtime", style="green3")
    winner.append(f"   · tie → {cfg.get('CONFLICT_PREVALANCE','initiator')} (same-timestamp only)", style="grey62")
    rows = [
        ("soft-delete", line(sd_on, li, ri, cfg.get("SOFT_DELETE_DAYS", "?"))),
        ("conflict-bkp", line(cb_on, lb, rb, cfg.get("CONFLICT_BACKUP_DAYS", "?"))),
        ("winner", winner),
    ]
    excl = cfg.get("RSYNC_EXCLUDE_PATTERN", "").strip()
    if excl:
        rows.append(("excludes", Text(excl, style="yellow")))
    return _kv(rows)


def pending_render(pending, running) -> Text:
    if running:
        return Text("computing dry-run…", style="grey62")
    if pending is None:
        t = Text("press ", style="grey62")
        t.append("c", style="white")
        t.append(" to check for pending changes", style="grey62")
        return t
    if pending["total"] == 0:
        return Text("in sync — nothing pending", style="green3")
    t = Text(f"{pending['total']} pending", style="yellow")
    t.append(f"    →target {pending['tu']}u/{pending['td']}d", style="grey62")
    t.append(f"    →init {pending['iu']}u/{pending['id']}d", style="grey62")
    return t


# ── panel widget ─────────────────────────────────────────────────────────────
class Panel(Static):
    """A titled, bordered box that renders a Rich renderable."""

    def __init__(self, title: str, pid: str, border: str = "grey50"):
        super().__init__(id=pid)
        self.border_title = title
        self.styles.border = ("round", border)
        self.styles.padding = (0, 1)

    def set_border(self, color: str):
        self.styles.border = ("round", color)


# ── app ──────────────────────────────────────────────────────────────────────
class OsyncDash(App):
    CSS = """
    Screen { background: $surface; }
    #grid { height: auto; }
    .row { height: auto; }
    Panel { height: auto; margin: 0 1 1 1; }
    #p_health { margin-top: 1; }
    #p_devices { width: 3fr; }
    #p_state   { width: 2fr; }
    #p_paths   { width: 1fr; }
    #p_safety  { width: 1fr; }
    """
    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("c", "check", "Check pending"),
        ("s", "sync", "Sync now"),
        ("l", "log", "Log"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, cfg, tgt, cfg_path, local_only=False, interval=6, want_pending=True):
        super().__init__()
        self.cfg, self.tgt, self.cfg_path = cfg, tgt, cfg_path
        self.local_only = local_only
        self.interval = max(2, interval)
        self.want_pending = want_pending
        self.data = None
        self.pending = None
        self.pending_running = False
        self.title = f"osync-dash · {cfg.get('INSTANCE_ID', '?')}"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="grid"):
            yield Panel("health", "p_health", BORDER["cyan"])
            with Horizontal(classes="row"):
                yield Panel("devices", "p_devices", BORDER["grey"])
                yield Panel("sync state", "p_state", BORDER["cyan"])
            with Horizontal(classes="row"):
                yield Panel("paths", "p_paths", BORDER["cyan"])
                yield Panel("safety net", "p_safety", BORDER["magenta"])
            yield Panel("pending (dry-run)", "p_pending", BORDER["grey"])
        yield Footer()

    def on_mount(self):
        for pid in ("p_health", "p_devices", "p_state", "p_paths", "p_safety"):
            self.query_one(f"#{pid}", Panel).update(Text("gathering…", style="grey62"))
        self.query_one("#p_pending", Panel).update(pending_render(None, False))
        self.refresh_data()
        if self.want_pending:
            self.check_pending()
        self.set_interval(self.interval, self.refresh_data)

    # workers (threaded so ssh never blocks the UI) ------------------------
    @work(thread=True, exclusive=True, group="refresh")
    def refresh_data(self):
        try:
            data = core.gather(self.cfg, self.tgt, self.local_only)
        except Exception as e:
            self.call_from_thread(self.notify, f"refresh failed: {e}", severity="error")
            return
        self.call_from_thread(self._apply, data)

    @work(thread=True, exclusive=True, group="pending")
    def check_pending(self):
        self.pending_running = True
        self.call_from_thread(self._apply_pending)
        try:
            p = core.compute_pending(self.cfg_path)
        except Exception as e:
            p = None
            self.call_from_thread(self.notify, f"check failed: {e}", severity="error")
        self.pending_running = False
        self.pending = p
        self.call_from_thread(self._apply_pending)

    # UI updates (main thread) ---------------------------------------------
    def _apply(self, data):
        self.data = data
        state, local, remote = data
        _, cname = core.health(state, remote, local)
        p = self.query_one("#p_health", Panel)
        p.set_border(BORDER.get(cname, "#d0d0d0"))
        p.update(health_render(self.cfg, state, local, remote))
        self.query_one("#p_devices", Panel).update(devices_render(self.cfg, self.tgt, local, remote))
        self.query_one("#p_state", Panel).update(state_render(self.cfg, state))
        self.query_one("#p_paths", Panel).update(paths_render(self.cfg, self.tgt))
        self.query_one("#p_safety", Panel).update(safety_render(self.cfg, self.tgt, local, remote))
        self.sub_title = "refreshed"

    def _apply_pending(self):
        p = self.query_one("#p_pending", Panel)
        p.update(pending_render(self.pending, self.pending_running))
        if not self.pending_running and self.pending is not None:
            p.set_border(BORDER["green"] if self.pending["total"] == 0 else BORDER["yellow"])

    # actions ---------------------------------------------------------------
    def action_refresh(self):
        self.sub_title = "refreshing…"
        self.refresh_data()

    def action_check(self):
        self.check_pending()

    def action_sync(self):
        with self.suspend():
            os.system("clear")
            print("\033[36m▶ Running osync sync…\033[0m\n")
            subprocess.run([core.OSYNC_BIN, str(self.cfg_path), "--summary", "--no-prefix"])
            try:
                input("\n[done] Press Enter to return… ")
            except EOFError:
                pass
        self.refresh_data()
        if self.want_pending:
            self.check_pending()

    def action_log(self):
        logf = os.path.expanduser(self.cfg.get("LOGFILE", "") or "")
        if not (logf and os.path.exists(logf)):
            self.notify("no log file found", severity="warning")
            return
        with self.suspend():
            subprocess.run([os.environ.get("PAGER", "less"), "+G", logf])


def parse_args(argv):
    o = {"config": None, "local_only": False, "interval": 6, "fast": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-c", "--config"):
            i += 1; o["config"] = argv[i]
        elif a in ("-i", "--interval"):
            i += 1; o["interval"] = int(float(argv[i]))
        elif a == "--local-only":
            o["local_only"] = True
        elif a in ("-f", "--fast", "--no-check"):
            o["fast"] = True
        i += 1
    return o


def main():
    o = parse_args(sys.argv[1:])
    cfg_path = core.pick_config(o["config"])
    cfg, tgt = core.load(cfg_path)
    OsyncDash(cfg, tgt, cfg_path, local_only=o["local_only"],
              interval=o["interval"], want_pending=not o["fast"]).run()


if __name__ == "__main__":
    main()
