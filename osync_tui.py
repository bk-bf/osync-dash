#!/usr/bin/env python3
"""osync-dash — Textual TUI front-end (ayu-themed, btop-inspired).

Launched by the `osync-dash` wrapper under the project virtualenv. All data
gathering + config management lives in osync_core; this is presentation only.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import osync_core as core  # noqa: E402

from rich.console import Group  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402
from textual import on, work  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual.theme import Theme  # noqa: E402
from textual.widgets import (Button, Footer, Header, Input, Label, OptionList,  # noqa: E402
                             RadioButton, RadioSet, Select, Static)
from textual.widgets.option_list import Option  # noqa: E402

# ── ayu palette (from btop's ayu.theme) ──────────────────────────────────────
BG, FG = "#0B0E14", "#BFBDB6"
GOLD, MINT, GREEN = "#E6B450", "#95E6CB", "#4CBF99"
LAV, TAN, SALMON, BLUE = "#DFBFFF", "#E6B673", "#F28779", "#73D0FF"
LINE, MUTED, WHITE = "#565B66", "#8A909E", "#E6E4DE"

# health colour-name (core.health) -> ayu hex
HC = {"green": MINT, "yellow": GOLD, "red": SALMON, "blue": BLUE,
      "cyan": MINT, "grey": MUTED, "white": FG, "magenta": LAV}
# per-panel accent
ACCENT = {"health": GOLD, "devices": MINT, "state": LAV,
          "paths": TAN, "safety": SALMON, "pending": BLUE}
# osync's internal role names -> friendly local/remote (GitHub/Dropbox style)
ROLE = {"initiator": "local", "target": "remote"}

AYU = Theme(
    name="ayu", primary=GOLD, secondary=MINT, accent=LAV, foreground=FG,
    background=BG, surface="#0F141C", panel="#11161F",
    success=MINT, warning=GOLD, error=SALMON, dark=True,
    variables={
        "border": LINE, "border-blurred": LINE,
        "footer-key-foreground": GOLD, "footer-description-foreground": FG,
        "block-cursor-foreground": BG, "block-cursor-background": GOLD,
        "input-selection-background": f"{GOLD} 35%",
    },
)


# ── helpers ──────────────────────────────────────────────────────────────────
def _n(v, dash="—"):
    return dash if v is None else str(v)


def _hs(v):
    return core.humansize(v) if v is not None else "—"


def _interp(a, b, t):
    a, b = a.lstrip("#"), b.lstrip("#")
    ar, ag, ab = int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)
    br, bg, bb = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
    return f"#{round(ar+(br-ar)*t):02x}{round(ag+(bg-ag)*t):02x}{round(ab+(bb-ab)*t):02x}"


def grad_bar(frac, width, c1, c2, bg=LINE) -> Text:
    """A btop-style gradient meter bar."""
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    t = Text()
    for i in range(width):
        if i < filled:
            t.append("█", style=_interp(c1, c2, i / max(1, width - 1)))
        else:
            t.append("─", style=bg)
    return t


# ── renderables ──────────────────────────────────────────────────────────────
def health_render(cfg, state, local, remote) -> Text:
    st, cname = core.health(state, remote, local)
    c = HC.get(cname, FG)
    age = core.human_age(core.time.time() - state["last_ts"] if state["last_ts"] else None)
    stype = cfg.get("SYNC_TYPE", "").strip() or "bidirectional"
    t = Text()
    t.append("● ", style=f"bold {c}")
    t.append(st, style=f"bold {c}")
    if state["running"]:
        t.append("   ⟳ sync running", style=BLUE)
    t.append("      last sync ", style=MUTED)
    t.append(age, style=WHITE)
    t.append("      mode ", style=MUTED)
    t.append(stype, style=WHITE)
    return t


def device_block(role, r, accent) -> Group:
    reach = r.get("reach", False)
    host = r.get("host") or (r.get("ts") or {}).get("name") or "—"
    l1 = Text()
    l1.append("▎", style=accent)
    l1.append(f" {role:<9} ", style=f"bold {accent}")
    l1.append(host, style=f"bold {WHITE}")
    ts = r.get("ts")
    if ts and ts.get("name"):
        l1.append(f"   ↳ {ts['name']}", style=MUTED)
        if ts.get("ip"):
            l1.append(f" · {ts['ip']}", style=LINE)
    l2 = Text("   ")
    l2.append("● online" if reach else "● offline", style=MINT if reach else SALMON)
    l2.append("    rsync ", style=MUTED)
    l2.append("✓" if r.get("rsync") else ("—" if not reach else "✗"), style=MINT if r.get("rsync") else MUTED)
    if r.get("files") is not None:
        l2.append(f"    {r['files']} files", style=FG)
    if r.get("size") is not None:
        l2.append(f" · {core.humansize(r['size'])}", style=MUTED)
    rows = [l1, l2]
    dt, du = r.get("disk_total"), r.get("disk_used")
    if dt and du is not None and dt > 0:
        frac = du / dt
        l3 = Text("   disk ")
        l3.append("▕", style=LINE)
        l3.append_text(grad_bar(frac, 22, MINT, GREEN))
        l3.append("▏", style=LINE)
        l3.append(f" {frac*100:.0f}%", style=MINT)
        l3.append(f"   {core.humansize(du)} used · {core.humansize(r.get('free'))} free", style=MUTED)
        rows.append(l3)
    return Group(*rows)


def devices_render(cfg, tgt, local, remote) -> Group:
    parts = [device_block("local", local, MINT), Text(""),
             device_block("remote", remote if tgt.get("remote") else local, GOLD)]
    if tgt.get("remote") and not remote.get("reach", False):
        parts.append(Text(f"   ! {remote.get('err', 'unreachable')}", style=SALMON))
    return Group(*parts)


def _kv(rows) -> Table:
    t = Table(box=None, expand=True, show_header=False, pad_edge=False)
    t.add_column(style=MUTED, no_wrap=True, width=13)
    t.add_column(ratio=1)
    for k, v in rows:
        t.add_row(k, v)
    return t


def state_render(cfg, state) -> Table:
    res = state.get("init_action") or "—"
    rc = MINT if res == "synced" else (MUTED if res == "—" else SALMON)
    resume = state.get("resume")
    lastrun = (core.time.strftime("%Y-%m-%d %H:%M", core.time.localtime(state["last_ts"]))
               if state["last_ts"] else "never")
    age = core.human_age(core.time.time() - state["last_ts"] if state["last_ts"] else None)
    ta = state.get("tgt_action")
    result = Text(res, style=rc)
    result.append("   remote ", style=MUTED)
    result.append(_n(ta), style=MINT if ta == "synced" else MUTED)
    return _kv([
        ("last run", Text(f"{lastrun}  ({age} ago)", style=WHITE)),
        ("result", result),
        ("resume", Text("0 clean", style=MINT) if resume in ("0", None) else Text(f"{resume} retried", style=SALMON)),
        ("running", Text("yes", style=BLUE) if state["running"] else Text("no", style=MUTED)),
    ])


MODE_LABEL = {"ts": ("Tailscale", MINT), "ssh": ("plain SSH", GOLD),
              "both": ("both · TS→SSH", BLUE), "": ("", MUTED)}
DIR_LABEL = {"send": ("→ send to remote", GOLD), "receive": ("← receive from remote", BLUE),
             "bidir": ("⇄ bidirectional", MINT)}


def paths_render(cfg, tgt, remote=None) -> Table:
    tgt_s = (f"{tgt.get('user','')}@{tgt.get('host','')}:{tgt.get('path','?')}"
             if tgt.get("remote") else cfg.get("TARGET_SYNC_DIR", "?"))
    mode = core.mode_of(cfg)
    mlabel, mcol = MODE_LABEL.get(mode, ("", MUTED))
    conn = Text()
    if mlabel:
        conn.append(mlabel, style=mcol)
    used = (remote or {}).get("endpoint_used")
    if used and mode == "both":
        conn.append(f"  · live: {'TS' if used=='ts' else 'SSH'}",
                    style=MINT if used == "ts" else GOLD)
    dlabel, dcol = DIR_LABEL.get(core.direction_of(cfg), ("", MUTED))
    return _kv([
        ("local", Text(cfg.get("INITIATOR_SYNC_DIR", "?"), style=WHITE)),
        ("remote", Text(tgt_s, style=WHITE)),
        ("direction", Text(dlabel, style=dcol)),
        ("connection", conn if mlabel else Text("—", style=MUTED)),
        ("log", Text(cfg.get("LOGFILE", "—") or "—", style=MUTED)),
        ("config", Text(cfg.get("_configfile", ""), style=MUTED)),
    ])


def safety_render(cfg, tgt, local, remote) -> Table:
    li, ri = local.get("deleted", 0), (remote.get("deleted") if tgt.get("remote") else local.get("deleted"))
    lb, rb = local.get("backup", 0), (remote.get("backup") if tgt.get("remote") else local.get("backup"))
    sd = cfg.get("SOFT_DELETE", "true") == "true"
    cb = cfg.get("CONFLICT_BACKUP", "true") == "true"

    def ln(on, a, b, days):
        t = Text("on  ", style=MINT) if on else Text("off ", style=MUTED)
        t.append(f"local {a} / remote {_n(b)}", style=FG)
        t.append(f"   kept {days}d", style=MUTED)
        return t

    winner = Text("newest edit", style=MINT)
    winner.append(f"   · tie → {ROLE.get(cfg.get('CONFLICT_PREVALANCE',''), 'local')}", style=MUTED)
    rows = [("soft-delete", ln(sd, li, ri, cfg.get("SOFT_DELETE_DAYS", "?"))),
            ("conflict-bkp", ln(cb, lb, rb, cfg.get("CONFLICT_BACKUP_DAYS", "?"))),
            ("winner", winner)]
    excl = cfg.get("RSYNC_EXCLUDE_PATTERN", "").strip()
    if excl:
        rows.append(("excludes", Text(excl, style=GOLD)))
    return _kv(rows)


def pending_render(pending, running) -> Text:
    if running:
        return Text("⟳ computing dry-run…", style=MUTED)
    if pending is None:
        t = Text("press ", style=MUTED)
        t.append("c", style=f"bold {GOLD}")
        t.append(" to check for pending changes", style=MUTED)
        return t
    if pending["total"] == 0:
        return Text("✓ in sync — nothing pending", style=MINT)
    t = Text(f"⚠ {pending['total']} pending", style=GOLD)
    t.append(f"     ↑ to remote {pending['tu']}u / {pending['td']}d", style=MUTED)
    t.append(f"     ↓ to local {pending['iu']}u / {pending['id']}d", style=MUTED)
    return t


# ── panel widget ─────────────────────────────────────────────────────────────
class Panel(Static):
    def __init__(self, title, pid, accent):
        super().__init__(id=pid)
        self.border_title = f" {title} "
        self._accent = accent

    def on_mount(self):
        self.set_accent(self._accent)

    def set_accent(self, color):
        self._accent = color
        self.styles.border = ("round", color)
        self.styles.border_title_color = color


# ── directory autocomplete field ─────────────────────────────────────────────
class DirField(Vertical):
    """Input + live dropdown of matching subdirectories (local or remote).

    `resolver` None → completes local paths. Otherwise a callable returning
    (user, host, port, key) — or None when the connection isn't set yet — and
    the field lists remote dirs over ssh (cached per connection+parent)."""

    def __init__(self, fid, placeholder="", value="~/", resolver=None):
        super().__init__(id=fid, classes="dirfield")
        self._placeholder = placeholder
        self._initial = value
        self.resolver = resolver
        self.cache = {}

    def compose(self) -> ComposeResult:
        yield Input(value=self._initial, placeholder=self._placeholder, id="in")
        ol = OptionList(id="opts")
        ol.display = False
        yield ol

    @property
    def value(self) -> str:
        return self.query_one("#in", Input).value

    def set_value(self, v):
        self.query_one("#in", Input).value = v

    def _split(self, v):
        v = v or ""
        if v in ("", "~", "~/"):
            return ("~/", "")
        if v.endswith("/"):
            return (v, "")
        if "/" not in v:
            return ("~/", v)
        i = v.rfind("/")
        return (v[:i + 1], v[i + 1:])

    def _dirs(self, parent):
        if self.resolver is None:
            return core.list_local_dirs(parent), "ok"
        conn = self.resolver()
        if not conn:
            return [], "noconn"
        key = (conn, parent)
        if key in self.cache:
            return self.cache[key], "ok"
        self._fetch(conn, parent, key)
        return [], "fetching"

    @work(thread=True, exclusive=True, group="dirfetch")
    def _fetch(self, conn, parent, key):
        dirs = core.list_remote_dirs(*conn, parent)
        self.cache[key] = dirs
        self.app.call_from_thread(self._refresh)

    def _refresh(self):
        parent, prefix = self._split(self.value)
        dirs, status = self._dirs(parent)
        ol = self.query_one("#opts", OptionList)
        ol.clear_options()
        if status == "noconn":
            ol.add_option(Option(Text("set the remote user + host first", style=MUTED), id="__x__"))
            ol.display = True
            return
        if status == "fetching":
            ol.add_option(Option(Text("listing…", style=MUTED), id="__x__"))
            ol.display = True
            return
        pl = prefix.lower()
        matches = sorted((d for d in dirs if pl in d.lower()),
                         key=lambda d: (not d.lower().startswith(pl), d.lower()))
        if not matches:
            ol.display = False
            return
        for d in matches[:8]:
            ol.add_option(Option(Text(d + "/", style=WHITE), id=d))
        ol.display = True

    def on_mount(self):
        self._refresh()

    @on(Input.Changed, "#in")
    def _changed(self, e):
        e.stop()
        self._refresh()

    @on(OptionList.OptionSelected, "#opts")
    def _selected(self, e):
        e.stop()
        if e.option.id == "__x__":
            return
        parent, _ = self._split(self.value)
        self.set_value(parent + e.option.id + "/")
        self.query_one("#in", Input).focus()
        self._refresh()

    def on_key(self, e):
        if e.key == "down" and self.query_one("#opts", OptionList).display:
            self.query_one("#opts", OptionList).focus()
            e.stop()


# ── host-switch popup ────────────────────────────────────────────────────────
class HostPicker(ModalScreen):
    """A floating list of hosts; the dashboard stays visible (dimmed) behind."""

    CSS = """
    HostPicker { align: center middle; background: $background 55%; }
    #hp { width: 56; height: auto; max-height: 80%; padding: 1 2;
          background: $panel; border: round $secondary; }
    #hp .h { color: $secondary; text-style: bold; margin-bottom: 1; }
    #hp .hint { color: $foreground-muted; margin-top: 1; }
    OptionList { border: none; background: $panel; height: auto; max-height: 18; padding: 0; }
    """
    BINDINGS = [("escape", "cancel", "Cancel"), ("a", "add", "Add host")]

    def __init__(self, configs, current):
        super().__init__()
        self.configs = configs
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="hp"):
            yield Static("⇅ switch host", classes="h")
            opts = []
            for p in self.configs:
                c = core.parse_config(p)
                name = c.get("INSTANCE_ID", p.stem)
                via = {"ts": "tailscale", "ssh": "ssh", "both": "ts→ssh"}.get(c.get("DASH_ENDPOINT_MODE", ""), "")
                t = Text("● " if p == self.current else "  ",
                         style=MINT if p == self.current else MUTED)
                t.append(name, style=f"bold {WHITE}" if p == self.current else FG)
                if via:
                    t.append(f"   {via}", style=MUTED)
                opts.append(Option(t, id=str(p)))
            ol = OptionList(*opts, id="ol")
            yield ol
            yield Static("↑↓ move · enter switch · a add · esc close", classes="hint")

    def on_mount(self):
        ol = self.query_one("#ol", OptionList)
        try:
            ol.highlighted = next(i for i, p in enumerate(self.configs) if p == self.current)
        except StopIteration:
            pass
        ol.focus()

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected):
        self.dismiss(Path(event.option.id))

    def action_cancel(self):
        self.dismiss(None)

    def action_add(self):
        self.dismiss("__add__")


# ── add-host modal (dynamic, mesh) ───────────────────────────────────────────
MODES = [("Tailscale", "ts"), ("Plain SSH", "ssh"), ("Both", "both")]
DIRS = [("bidir", "bidir"), ("send", "send"), ("receive", "receive")]


class AddHost(ModalScreen):
    CSS = """
    AddHost { align: center middle; background: $background 55%; }
    #box { width: 82; height: auto; max-height: 90%; padding: 1 2; background: $panel;
           border: round $primary; }
    #box .h { color: $primary; text-style: bold; }
    #box .sub { color: $foreground-muted; margin-bottom: 1; }
    #form { height: auto; max-height: 26; overflow-y: auto; scrollbar-size-vertical: 1; }
    #box Label { color: $foreground-muted; margin-top: 1; }
    #box Input { border: tall $panel-lighten-2; }
    #box RadioSet { border: none; height: auto; layout: horizontal; }
    .dirfield { height: auto; }
    .dirfield OptionList { border: round $panel-lighten-2; background: $boost;
                          height: auto; max-height: 7; }
    #ssh_row, #ts_row { height: auto; }
    #err { color: $error; height: auto; }
    #btns { height: auto; align-horizontal: right; margin-top: 1; }
    #btns Button { margin-left: 2; }
    .cols { height: auto; }
    .cols Input { width: 1fr; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, devices):
        super().__init__()
        self.devices = devices

    def _mode(self):
        return MODES[self.query_one("#i_mode", RadioSet).pressed_index or 0][1]

    def _conn(self):
        """Current (user, host, port, key) for remote-dir listing, or None."""
        user = self.query_one("#i_user", Input).value.strip()
        mode = self._mode()
        host = (self.query_one("#i_ts", Input).value.strip() if mode != "ssh"
                else self.query_one("#i_ssh", Input).value.strip())
        port = (self.query_one("#i_tsport", Input).value.strip() if mode != "ssh"
                else self.query_one("#i_sshport", Input).value.strip()) or "22"
        key = self.query_one("#i_key", Input).value.strip()
        return (user, host, port, key) if (user and host) else None

    def compose(self) -> ComposeResult:
        opts = [(f"{d['name']}  ·  {d['os']}{'  ●' if d['online'] else '  ○'}", d["dns"] or d["name"])
                for d in self.devices]
        opts.append(("✎  manual entry", "__manual__"))
        with Vertical(id="box"):
            yield Static("＋ add a sync host", classes="h")
            yield Static("import a Tailscale device (or enter a host), then pick dirs to sync", classes="sub")
            with VerticalScroll(id="form"):
                yield Label("device")
                yield Select(opts, prompt="pick a Tailscale device…", id="i_device", allow_blank=True)

                yield Label("reach it via")
                with RadioSet(id="i_mode"):
                    yield RadioButton("Tailscale", value=True)
                    yield RadioButton("Plain SSH")
                    yield RadioButton("Both  (TS→SSH)")

                with Vertical(id="ts_row"):
                    yield Label("Tailscale host  ·  port")
                    with Horizontal(classes="cols"):
                        yield Input(placeholder="host.tailXXXX.ts.net / alias", id="i_ts")
                        yield Input(value="22", id="i_tsport")
                with Vertical(id="ssh_row"):
                    yield Label("plain-SSH host  ·  port")
                    with Horizontal(classes="cols"):
                        yield Input(placeholder="192.168.1.50 / host.example.com", id="i_ssh")
                        yield Input(value="22", id="i_sshport")

                yield Label("remote user  ·  ssh key")
                with Horizontal(classes="cols"):
                    yield Input(placeholder="ubuntu", id="i_user")
                    yield Input(value=os.path.expanduser("~/.ssh/id_ed25519"), id="i_key")

                yield Label("remote directory (type to autocomplete on that device)")
                yield DirField("i_rpath", placeholder="~/sync", resolver=self._conn)

                yield Label("local directory (this machine)")
                yield DirField("i_lpath", placeholder="~/sync")

                yield Label("direction")
                with RadioSet(id="i_dir"):
                    yield RadioButton("⇄ Bidirectional", value=True)
                    yield RadioButton("→ Send")
                    yield RadioButton("← Receive")

                yield Label("name")
                yield Input(placeholder="desktop / nas / vps", id="i_name")

            yield Static("", id="err")
            with Horizontal(id="btns"):
                yield Button("Cancel", id="cancel")
                yield Button("Create", variant="primary", id="create")

    def on_mount(self):
        self._sync_mode_rows()

    def _sync_mode_rows(self):
        mode = self._mode()
        self.query_one("#ts_row").display = mode in ("ts", "both")
        self.query_one("#ssh_row").display = mode in ("ssh", "both")

    @on(RadioSet.Changed, "#i_mode")
    def _mode_changed(self, e):
        self._sync_mode_rows()

    @on(Select.Changed, "#i_device")
    def _device_changed(self, e):
        if e.value in (Select.BLANK, "__manual__", None):
            return
        self.query_one("#i_ts", Input).value = str(e.value)
        if not self.query_one("#i_name", Input).value.strip():
            self.query_one("#i_name", Input).value = str(e.value).split(".")[0]

    def action_cancel(self):
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self):
        self.dismiss(None)

    @on(Button.Pressed, "#create")
    def _create(self):
        g = lambda i: self.query_one(f"#{i}", Input).value.strip()  # noqa: E731
        mode = self._mode()
        direction = DIRS[self.query_one("#i_dir", RadioSet).pressed_index or 0][1]
        name, user = g("i_name"), g("i_user")
        rpath = self.query_one("#i_rpath", DirField).value.strip()
        lpath = self.query_one("#i_lpath", DirField).value.strip()
        ts_host, ssh_host = g("i_ts"), g("i_ssh")
        err = self.query_one("#err", Static)
        need = [n for n, v in (("name", name), ("user", user),
                               ("remote dir", rpath), ("local dir", lpath)) if not v]
        if mode in ("ts", "both") and not ts_host:
            need.append("Tailscale host")
        if mode in ("ssh", "both") and not ssh_host:
            need.append("SSH host")
        if need:
            err.update("missing: " + ", ".join(need))
            return
        try:
            p = core.create_config(
                instance=name, initiator_dir=lpath, user=user, path=rpath, key=g("i_key"),
                ts_host=ts_host, ts_port=g("i_tsport") or "22",
                ssh_host=ssh_host, ssh_port=g("i_sshport") or "22",
                mode=mode, direction=direction)
        except FileExistsError as e:
            err.update(str(e)); return
        except Exception as e:  # noqa: BLE001
            err.update(f"error: {e}"); return
        self.dismiss(p)


# ── app ──────────────────────────────────────────────────────────────────────
class OsyncDash(App):
    CSS = """
    Screen { background: $background; }
    #grid { height: auto; padding: 0 1; }
    .row { height: auto; }
    Panel { height: auto; margin: 0 1 1 1; padding: 0 1; background: $panel; }
    #p_health { margin: 1 1 1 1; }
    #p_devices { width: 3fr; }
    #p_state   { width: 2fr; }
    #p_paths, #p_safety { width: 1fr; }
    """
    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("c", "check", "Check"),
        ("s", "sync", "Sync"),
        ("t", "cycle_mode", "Endpoint"),
        ("d", "cycle_direction", "Direction"),
        ("n", "next_host", "Host"),
        ("a", "add_host", "Add"),
        ("l", "log", "Log"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, cfg_path, local_only=False, interval=6, want_pending=True):
        super().__init__()
        self.configs = core.list_configs()
        self.cfg_path = Path(cfg_path)
        if self.cfg_path not in self.configs and self.cfg_path.exists():
            self.configs.append(self.cfg_path)
        self.local_only = local_only
        self.interval = max(2, interval)
        self.want_pending = want_pending
        self.data = None
        self.pending = None
        self.pending_running = False
        self._load_cfg()

    def _load_cfg(self):
        self.cfg, self.tgt = core.load(self.cfg_path)
        self.title = "osync-dash"
        mode = core.mode_of(self.cfg)
        via = {"ts": " · Tailscale", "ssh": " · SSH", "both": " · TS→SSH"}.get(mode, "")
        arrow = {"send": " →", "receive": " ←", "bidir": " ⇄"}.get(core.direction_of(self.cfg), "")
        self.sub_title = f"{self.cfg.get('INSTANCE_ID','?')}{via}{arrow}"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="grid"):
            yield Panel("health", "p_health", GOLD)
            with Horizontal(classes="row"):
                yield Panel("devices", "p_devices", MINT)
                yield Panel("sync state", "p_state", LAV)
            with Horizontal(classes="row"):
                yield Panel("paths", "p_paths", TAN)
                yield Panel("safety net", "p_safety", SALMON)
            yield Panel("pending", "p_pending", BLUE)
        yield Footer()

    def on_mount(self):
        self.register_theme(AYU)
        self.theme = "ayu"
        for pid in ("p_health", "p_devices", "p_state", "p_paths", "p_safety"):
            self.query_one(f"#{pid}", Panel).update(Text("gathering…", style=MUTED))
        self.query_one("#p_pending", Panel).update(pending_render(None, False))
        self.refresh_data()
        if self.want_pending:
            self.check_pending()
        self.set_interval(self.interval, self.refresh_data)

    # workers -------------------------------------------------------------
    @work(thread=True, exclusive=True, group="refresh")
    def refresh_data(self):
        try:
            data = core.gather(self.cfg, self.tgt, self.local_only)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.notify, f"refresh failed: {e}", severity="error")
            return
        self.call_from_thread(self._apply, data)

    @work(thread=True, exclusive=True, group="pending")
    def check_pending(self):
        self.pending_running = True
        self.call_from_thread(self._apply_pending)
        try:
            p = core.compute_pending(self.cfg_path)
        except Exception:  # noqa: BLE001
            p = None
        self.pending_running = False
        self.pending = p
        self.call_from_thread(self._apply_pending)

    # ui updates ----------------------------------------------------------
    def _apply(self, data):
        self.data = data
        state, local, remote = data
        _, cname = core.health(state, remote, local)
        h = self.query_one("#p_health", Panel)
        h.set_accent(HC.get(cname, FG))
        h.update(health_render(self.cfg, state, local, remote))
        self.query_one("#p_devices", Panel).update(devices_render(self.cfg, self.tgt, local, remote))
        self.query_one("#p_state", Panel).update(state_render(self.cfg, state))
        self.query_one("#p_paths", Panel).update(paths_render(self.cfg, self.tgt, remote))
        self.query_one("#p_safety", Panel).update(safety_render(self.cfg, self.tgt, local, remote))

    def _apply_pending(self):
        p = self.query_one("#p_pending", Panel)
        p.update(pending_render(self.pending, self.pending_running))
        if not self.pending_running and self.pending is not None:
            p.set_accent(MINT if self.pending["total"] == 0 else GOLD)

    # actions -------------------------------------------------------------
    def action_refresh(self):
        self.refresh_data()

    def action_check(self):
        self.check_pending()

    def action_sync(self):
        with self.suspend():
            os.system("clear")
            print(f"\033[38;2;230;180;80m▶ Running osync — {self.cfg.get('INSTANCE_ID','')}\033[0m\n")
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

    def action_cycle_mode(self):
        new, msg = core.cycle_mode(self.cfg_path)
        if not new:
            self.notify(msg, severity="warning")
            return
        self._load_cfg()
        self.notify(msg, severity="information")
        self.refresh_data()

    def action_cycle_direction(self):
        order = ["bidir", "send", "receive"]
        cur = core.direction_of(self.cfg)
        new = order[(order.index(cur) + 1) % 3] if cur in order else "bidir"
        core.set_direction(self.cfg_path, new)
        self._load_cfg()
        self.notify(f"direction: {DIR_LABEL[new][0]}", severity="information")
        self.refresh_data()

    def _switch_to(self, path, msg=None):
        self.cfg_path = Path(path)
        self._load_cfg()
        if msg:
            self.notify(msg, severity="information")
        self.refresh_data()
        if self.want_pending:
            self.check_pending()

    def action_next_host(self):
        self.configs = core.list_configs() or self.configs
        self.push_screen(HostPicker(self.configs, self.cfg_path), self._picked_host)

    def _picked_host(self, result):
        if result is None:
            return
        if result == "__add__":
            self.action_add_host()
            return
        if Path(result) != self.cfg_path:
            self._switch_to(result, f"→ {core.parse_config(Path(result)).get('INSTANCE_ID','?')}")

    def action_add_host(self):
        self.push_screen(AddHost(core.ts_devices()), self._added)

    def _added(self, path):
        if not path:
            return
        self.configs = core.list_configs()
        self._switch_to(path, f"created host '{core.parse_config(Path(path)).get('INSTANCE_ID','?')}'")


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
    OsyncDash(cfg_path, local_only=o["local_only"], interval=o["interval"],
              want_pending=not o["fast"]).run()


if __name__ == "__main__":
    main()
