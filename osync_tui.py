#!/usr/bin/env python3
"""osync-dash — Textual TUI front-end (ayu-themed, btop-inspired).

One compose file (~/.config/osync/osync-dash.toml) defines many sync
connections; each is rendered as its own always-expanded card. Data gathering
and compose management live in osync_core; this module is presentation only.
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
from textual.containers import Horizontal, Vertical, VerticalScroll  # noqa: E402
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
# osync's internal role names -> friendly local/remote (GitHub/Dropbox style)
ROLE = {"initiator": "local", "target": "remote"}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

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


MODE_LABEL = {"ts": ("Tailscale", MINT), "ssh": ("plain SSH", GOLD),
              "both": ("both · TS→SSH", BLUE), "": ("", MUTED)}
DIR_LABEL = {"send": ("→ send to remote", GOLD), "receive": ("← receive from remote", BLUE),
             "bidir": ("⇄ bidirectional", MINT)}
DIR_ARROW = {"send": "→", "receive": "←", "bidir": "⇄"}


# ── card renderables ─────────────────────────────────────────────────────────
def device_line(role, r, accent) -> Group:
    """One replica: identity row + a stats/disk row underneath."""
    reach = r.get("reach", False)
    host = r.get("host") or (r.get("ts") or {}).get("name") or "—"
    l1 = Text("  ")
    l1.append("▎", style=accent)
    l1.append(f" {role:<7} ", style=f"bold {accent}")
    l1.append(host, style=f"bold {WHITE}")
    l1.append("   ● " + ("online" if reach else "offline"),
              style=MINT if reach else SALMON)
    l1.append("   rsync ", style=MUTED)
    l1.append("✓" if r.get("rsync") else ("—" if not reach else "✗"),
              style=MINT if r.get("rsync") else MUTED)
    ts = r.get("ts")
    if ts and ts.get("name"):
        l1.append(f"   ↳ {ts['name']}", style=MUTED)
        if ts.get("ip"):
            l1.append(f" · {ts['ip']}", style=LINE)
    rows = [l1]
    dt, du = r.get("disk_total"), r.get("disk_used")
    l2 = Text("    ")
    if r.get("files") is not None:
        l2.append(f"{r['files']} files", style=FG)
    if r.get("size") is not None:
        l2.append(f" · {core.humansize(r['size'])}", style=MUTED)
    if dt and du is not None and dt > 0:
        frac = du / dt
        l2.append("    disk ", style=MUTED)
        l2.append_text(grad_bar(frac, 18, MINT, GREEN))
        l2.append(f" {frac*100:.0f}%", style=MINT)
        l2.append(f"  {core.humansize(r.get('free'))} free", style=MUTED)
    if len(l2.plain.strip()):
        rows.append(l2)
    return Group(*rows)


def pushpull_line(cfg, state, pending, pending_running, spin) -> Text:
    """↑push / ↓pull legs — spin while a sync runs, else show queued counts."""
    running = state.get("running")
    d = core.direction_of(cfg)
    frame = SPIN[spin % len(SPIN)]
    push_active = running and d in ("send", "bidir")
    pull_active = running and d in ("receive", "bidir")

    def leg(active, arrow, label, cnt):
        s = Text()
        if active:
            s.append(f"{frame} ", style=GOLD)
            s.append(f"{label} ", style=f"bold {GOLD}")
            s.append("transferring…", style=GOLD)
        else:
            hot = bool(cnt)
            s.append(f"{arrow} ", style=BLUE if hot else MUTED)
            s.append(f"{label} ", style=MUTED)
            if pending_running:
                s.append("checking…", style=MUTED)
            elif cnt is None:
                s.append("—", style=LINE)
            elif hot:
                s.append(f"{cnt} queued", style=BLUE)
            else:
                s.append("idle", style=MINT)
        return s

    push = None if pending is None else pending["tu"] + pending["td"]
    pull = None if pending is None else pending["iu"] + pending["id"]
    t = Text("  ")
    t.append_text(leg(push_active, "↑", "push", push))
    t.append("        ")
    t.append_text(leg(pull_active, "↓", "pull", pull))
    return t


def _kv(rows, kw=11) -> Table:
    t = Table(box=None, expand=True, show_header=False, pad_edge=False, padding=(0, 0, 0, 2))
    t.add_column(style=MUTED, no_wrap=True, width=kw)
    t.add_column(ratio=1)
    for k, v in rows:
        t.add_row(k, v)
    return t


def card_body(name, cfg, tgt, data, pending, pending_running, spin) -> Group:
    if data is None:
        return Group(Text("  gathering…", style=MUTED))
    state, local, remote = data
    st, cname = core.health(state, remote, local)
    c = HC.get(cname, FG)
    age = core.human_age(core.time.time() - state["last_ts"] if state["last_ts"] else None)
    lastrun = (core.time.strftime("%Y-%m-%d %H:%M", core.time.localtime(state["last_ts"]))
               if state["last_ts"] else "never")

    # header: health · last-sync · result
    res = state.get("init_action") or "—"
    rc = MINT if res == "synced" else (MUTED if res == "—" else SALMON)
    ta = state.get("tgt_action")
    resume = state.get("resume")
    hdr = Text("  ")
    hdr.append("● ", style=f"bold {c}")
    hdr.append(st, style=f"bold {c}")
    if state["running"]:
        hdr.append(f"  {SPIN[spin % len(SPIN)]} running", style=BLUE)
    hdr.append(f"    last sync {age} ago", style=MUTED)
    hdr.append(f"  ({lastrun})", style=LINE)

    res_t = Text("  ")
    res_t.append("result ", style=MUTED)
    res_t.append(res, style=rc)
    res_t.append("  ·  remote ", style=MUTED)
    res_t.append(_n(ta), style=MINT if ta == "synced" else MUTED)
    res_t.append("  ·  resume ", style=MUTED)
    res_t.append("0 clean" if resume in ("0", None) else f"{resume} retried",
                 style=MINT if resume in ("0", None) else SALMON)

    dev_target = remote if tgt.get("remote") else local
    parts = [hdr, pushpull_line(cfg, state, pending, pending_running, spin), res_t,
             Text(""), device_line("local", local, MINT),
             device_line("remote", dev_target, GOLD)]
    if tgt.get("remote") and not remote.get("reach", False):
        parts.append(Text(f"    ! {remote.get('err', 'unreachable')}", style=SALMON))

    # paths + connection + safety, all inline (no collapsing)
    tgt_s = (f"{tgt.get('user','')}@{tgt.get('host','')}:{tgt.get('path','?')}"
             if tgt.get("remote") else cfg.get("TARGET_SYNC_DIR", "?"))
    mlabel, mcol = MODE_LABEL.get(core.mode_of(cfg), ("", MUTED))
    conn = Text(mlabel or "—", style=mcol if mlabel else MUTED)
    used = (remote or {}).get("endpoint_used")
    if used and core.mode_of(cfg) == "both":
        conn.append(f"  · live {'TS' if used == 'ts' else 'SSH'}",
                    style=MINT if used == "ts" else GOLD)

    li, ri = local.get("deleted", 0), (remote.get("deleted") if tgt.get("remote") else local.get("deleted"))
    lb, rb = local.get("backup", 0), (remote.get("backup") if tgt.get("remote") else local.get("backup"))
    sd = cfg.get("SOFT_DELETE", "true") == "true"
    cb = cfg.get("CONFLICT_BACKUP", "true") == "true"
    safety = Text()
    safety.append("soft-delete ", style=MUTED)
    safety.append("on" if sd else "off", style=MINT if sd else MUTED)
    safety.append(f" {li}/{ri if ri is not None else '—'}", style=FG)
    safety.append(f" {cfg.get('SOFT_DELETE_DAYS','?')}d", style=LINE)
    safety.append("   conflict-bkp ", style=MUTED)
    safety.append("on" if cb else "off", style=MINT if cb else MUTED)
    safety.append(f" {lb}/{rb if rb is not None else '—'}", style=FG)
    safety.append(f" {cfg.get('CONFLICT_BACKUP_DAYS','?')}d", style=LINE)
    safety.append("   winner ", style=MUTED)
    safety.append("newest", style=MINT)

    auto = cfg.get("_auto", "off")
    auto_t = Text()
    if auto == "change":
        auto_t.append("⟳ on file change", style=MINT)
    elif auto == "periodic":
        auto_t.append(f"⟳ every {cfg.get('_interval', '15min')}", style=MINT)
    else:
        auto_t.append("off — manual (press s)", style=MUTED)

    rows = [
        ("local", Text(cfg.get("INITIATOR_SYNC_DIR", "?"), style=WHITE)),
        ("remote", Text(tgt_s, style=WHITE)),
        ("via", conn),
        ("auto-sync", auto_t),
        ("safety", safety),
        ("log", Text(cfg.get("LOGFILE", "—") or "—", style=MUTED)),
    ]
    excl = cfg.get("RSYNC_EXCLUDE_PATTERN", "").strip()
    if excl:
        rows.append(("excludes", Text(excl, style=GOLD)))
    parts += [Text(""), _kv(rows)]
    return Group(*parts)


# ── connection card widget ───────────────────────────────────────────────────
class ConnectionCard(Static):
    can_focus = True

    def __init__(self, name, direction):
        super().__init__(id=f"card-{core._slug(name)}")
        self.conn_name = name
        self.set_title(name, direction)

    def set_title(self, name, direction):
        arrow = DIR_ARROW.get(direction, "⇄")
        self.border_title = f" {name}  {arrow} "


# ── add-connection modal (dynamic, mesh) ─────────────────────────────────────
MODES = [("Tailscale", "ts"), ("Plain SSH", "ssh"), ("Both", "both")]
DIRS = [("bidir", "bidir"), ("send", "send"), ("receive", "receive")]


class DirField(Vertical):
    """Input + live dropdown of matching subdirectories (local or remote)."""

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
            yield Static("＋ add a sync connection", classes="h")
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
        conn = {"name": name, "local": lpath, "remote": rpath, "direction": direction,
                "mode": mode, "user": user, "key": g("i_key"),
                "ts_host": ts_host, "ts_port": int(g("i_tsport") or 22),
                "ssh_host": ssh_host, "ssh_port": int(g("i_sshport") or 22)}
        try:
            core.add_connection(conn)
        except FileExistsError as e:
            err.update(str(e)); return
        except Exception as e:  # noqa: BLE001
            err.update(f"error: {e}"); return
        self.dismiss(name)


# ── auto-sync modal ──────────────────────────────────────────────────────────
AUTO_PRESETS = [("1 minute", "1m"), ("5 minutes", "5m"), ("15 minutes", "15m"),
                ("30 minutes", "30m"), ("1 hour", "1h"), ("6 hours", "6h"),
                ("12 hours", "12h"), ("24 hours", "24h"), ("2 days", "2d"),
                ("3 days", "3d"), ("1 week", "1w"), ("2 weeks", "2w")]
AUTO_RADIO = [("off", "Off — manual only"),
              ("change", "On file change  (live, inotify)"),
              ("periodic", "Every…  (scheduled)")]


class AutoSyncModal(ModalScreen):
    CSS = """
    AutoSyncModal { align: center middle; background: $background 55%; }
    #ab { width: 64; height: auto; max-height: 90%; padding: 1 2; background: $panel;
          border: round $secondary; }
    #ab .h { color: $secondary; text-style: bold; }
    #ab .sub { color: $foreground-muted; margin-bottom: 1; }
    #ab Label { color: $foreground-muted; margin-top: 1; }
    #ab RadioSet { border: none; height: auto; }
    #sched { height: auto; }
    #ab Input { border: tall $panel-lighten-2; }
    #abtns { height: auto; align-horizontal: right; margin-top: 1; }
    #abtns Button { margin-left: 2; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, name, cur_mode, cur_interval):
        super().__init__()
        self.cname = name
        self.cur_mode = cur_mode if cur_mode in ("off", "change", "periodic") else "off"
        self.cur_interval = cur_interval or "15m"

    def compose(self) -> ComposeResult:
        with Vertical(id="ab"):
            yield Static(f"⟳ auto-sync · {self.cname}", classes="h")
            yield Static("how should this connection sync on its own?", classes="sub")
            with RadioSet(id="a_mode"):
                for key, label in AUTO_RADIO:
                    yield RadioButton(label, value=(key == self.cur_mode))
            with Vertical(id="sched"):
                yield Label("run every (pick one, or type a custom interval)")
                preset_match = next((v for _, v in AUTO_PRESETS if v == self.cur_interval), None)
                yield Select(AUTO_PRESETS, value=preset_match or Select.BLANK,
                             prompt="pick an interval…", id="a_preset", allow_blank=True)
                yield Input(value="" if preset_match else self.cur_interval,
                            placeholder="custom: 90m · 3d · 2w", id="a_custom")
            with Horizontal(id="abtns"):
                yield Button("Cancel", id="a_cancel")
                yield Button("Save", variant="primary", id="a_save")

    def on_mount(self):
        self._sync_sched()

    def _mode(self):
        idx = self.query_one("#a_mode", RadioSet).pressed_index or 0
        return AUTO_RADIO[idx][0]

    def _sync_sched(self):
        self.query_one("#sched").display = self._mode() == "periodic"

    @on(RadioSet.Changed, "#a_mode")
    def _mode_changed(self, e):
        self._sync_sched()

    def action_cancel(self):
        self.dismiss(None)

    @on(Button.Pressed, "#a_cancel")
    def _cancel(self):
        self.dismiss(None)

    @on(Button.Pressed, "#a_save")
    def _save(self):
        mode = self._mode()
        interval = None
        if mode == "periodic":
            custom = self.query_one("#a_custom", Input).value.strip()
            sel = self.query_one("#a_preset", Select).value
            interval = custom or (sel if sel != Select.BLANK else self.cur_interval)
        self.dismiss((mode, interval))


# ── app ──────────────────────────────────────────────────────────────────────
class OsyncDash(App):
    CSS = """
    Screen { background: $background; scrollbar-size: 0 0; }
    #cards {
        height: 1fr; padding: 1 1;
        scrollbar-size-vertical: 1;
        scrollbar-background: $background;
        scrollbar-background-hover: $background;
        scrollbar-background-active: $background;
        scrollbar-color: $background;
        scrollbar-color-hover: $secondary;
        scrollbar-color-active: $secondary;
    }
    #empty { height: auto; padding: 2 3; color: $foreground-muted; }
    ConnectionCard {
        height: auto; margin: 0 0 1 0; padding: 1 1; background: $panel;
        border: round $panel-lighten-2; border-title-color: $foreground-muted;
    }
    ConnectionCard:focus {
        border: round $secondary; border-title-color: $secondary;
        background: $boost;
    }
    """
    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("c", "check", "Check"),
        ("s", "sync", "Sync"),
        ("t", "cycle_mode", "Endpoint"),
        ("d", "cycle_direction", "Direction"),
        ("A", "auto_sync", "Auto-sync"),
        ("a", "add_host", "Add"),
        ("l", "log", "Log"),
        ("down,j", "focus_next_card", "Next"),
        ("up,k", "focus_prev_card", "Prev"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, local_only=False, interval=6, want_pending=True):
        super().__init__()
        self.local_only = local_only
        self.interval = max(2, interval)
        self.want_pending = want_pending
        self.conns = []                # [(name, cfg, tgt)]
        self.data = {}                 # name -> (state, local, remote)
        self.pending = {}              # name -> pending dict | None
        self.pending_running = {}      # name -> bool
        self._spin = 0
        self._load_compose()

    def _load_compose(self):
        self.conns = core.load_all()
        self.title = "osync-dash"
        n = len(self.conns)
        self.sub_title = f"{n} connection{'' if n == 1 else 's'}"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield VerticalScroll(id="cards")
        yield Footer()

    def on_mount(self):
        self.register_theme(AYU)
        self.theme = "ayu"
        self._build_cards()
        self.set_interval(self.interval, self.action_refresh)
        self.set_interval(0.12, self._tick)

    # cards ---------------------------------------------------------------
    def _build_cards(self):
        box = self.query_one("#cards", VerticalScroll)
        box.remove_children()
        if not self.conns:
            box.mount(Static(
                "No sync connections yet.\n\n"
                "Press  a  to add one — it lands in ~/.config/osync/osync-dash.toml.",
                id="empty"))
            return
        first = None
        for name, cfg, _ in self.conns:
            card = ConnectionCard(name, core.direction_of(cfg))
            box.mount(card)
            card.update(Text("  gathering…", style=MUTED))
            first = first or card
        for name, cfg, tgt in self.conns:
            self.refresh_one(name, cfg, tgt)
            if self.want_pending:
                self.check_pending(name)
        if first:
            first.focus()

    def _card(self, name) -> ConnectionCard | None:
        try:
            return self.query_one(f"#card-{core._slug(name)}", ConnectionCard)
        except Exception:  # noqa: BLE001
            return None

    def _render_card(self, name):
        card = self._card(name)
        if not card:
            return
        cfg, tgt = next(((c, t) for n, c, t in self.conns if n == name), (None, None))
        if cfg is None:
            return
        card.update(card_body(name, cfg, tgt, self.data.get(name),
                              self.pending.get(name), self.pending_running.get(name, False),
                              self._spin))

    def _tick(self):
        """Advance the spinner; only re-render cards that are actively working."""
        self._spin += 1
        for name in list(self.data):
            busy = self.pending_running.get(name) or \
                (self.data.get(name) and self.data[name][0].get("running"))
            if busy:
                self._render_card(name)

    # workers -------------------------------------------------------------
    @work(thread=True, group="refresh", exclusive=False)
    def refresh_one(self, name, cfg, tgt):
        try:
            data = core.gather(cfg, tgt, self.local_only)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.notify, f"{name}: refresh failed: {e}", severity="error")
            return
        self.call_from_thread(self._apply, name, data)

    @work(thread=True, group="pending", exclusive=False)
    def check_pending(self, name):
        cfg = next((c for n, c, _ in self.conns if n == name), None)
        if cfg is None:
            return
        self.pending_running[name] = True
        self.call_from_thread(self._render_card, name)
        try:
            p = core.compute_pending(cfg["_configfile"])
        except Exception:  # noqa: BLE001
            p = None
        self.pending_running[name] = False
        self.pending[name] = p
        self.call_from_thread(self._render_card, name)

    def _apply(self, name, data):
        self.data[name] = data
        self._render_card(name)

    # actions -------------------------------------------------------------
    def _focused_name(self):
        f = self.focused
        if isinstance(f, ConnectionCard):
            return f.conn_name
        return self.conns[0][0] if self.conns else None

    def action_refresh(self):
        for name, cfg, tgt in self.conns:
            self.refresh_one(name, cfg, tgt)

    def action_check(self):
        name = self._focused_name()
        if name:
            self.check_pending(name)

    def action_sync(self):
        name = self._focused_name()
        cfg = next((c for n, c, _ in self.conns if n == name), None)
        if not cfg:
            return
        with self.suspend():
            os.system("clear")
            print(f"\033[38;2;230;180;80m▶ Running osync — {name}\033[0m\n")
            subprocess.run([core.OSYNC_BIN, cfg["_configfile"], "--summary", "--no-prefix"])
            try:
                input("\n[done] Press Enter to return… ")
            except EOFError:
                pass
        tgt = next((t for n, _, t in self.conns if n == name), None)
        self.refresh_one(name, cfg, tgt)
        if self.want_pending:
            self.check_pending(name)

    def action_log(self):
        name = self._focused_name()
        cfg = next((c for n, c, _ in self.conns if n == name), None)
        logf = os.path.expanduser((cfg or {}).get("LOGFILE", "") or "")
        if not (logf and os.path.exists(logf)):
            self.notify("no log file found", severity="warning")
            return
        with self.suspend():
            subprocess.run([os.environ.get("PAGER", "less"), "+G", logf])

    def action_cycle_mode(self):
        name = self._focused_name()
        if not name:
            return
        new, msg = core.cycle_mode_conn(name)
        if not new:
            self.notify(msg, severity="warning")
            return
        self._reload_conn(name)
        self.notify(msg, severity="information")
        self.refresh_one(*self._triple(name))

    def action_cycle_direction(self):
        name = self._focused_name()
        cfg = next((c for n, c, _ in self.conns if n == name), None)
        if not cfg:
            return
        order = ["bidir", "send", "receive"]
        cur = core.direction_of(cfg)
        new = order[(order.index(cur) + 1) % 3] if cur in order else "bidir"
        core.set_direction_conn(name, new)
        self._reload_conn(name)
        card = self._card(name)
        if card:
            card.set_title(name, new)
        self.notify(f"direction: {DIR_LABEL[new][0]}", severity="information")
        self._render_card(name)

    def action_auto_sync(self):
        name = self._focused_name()
        cfg = next((c for n, c, _ in self.conns if n == name), None)
        if not cfg:
            return
        self.push_screen(
            AutoSyncModal(name, cfg.get("_auto", "off"), cfg.get("_interval", "15m")),
            lambda res: self._apply_auto(name, res))

    def _apply_auto(self, name, res):
        if res is None:
            return
        mode, interval = res
        self.notify("configuring auto-sync…", timeout=2)
        self._do_set_auto(name, mode, interval)

    @work(thread=True, group="auto", exclusive=False)
    def _do_set_auto(self, name, mode, interval):
        _, msg = core.set_auto(name, mode, interval)
        self.call_from_thread(self._after_auto, name, msg)

    def _after_auto(self, name, msg):
        self._reload_conn(name)
        self.notify(msg, severity="information")
        self._render_card(name)

    def action_focus_next_card(self):
        self.screen.focus_next()

    def action_focus_prev_card(self):
        self.screen.focus_previous()

    def action_add_host(self):
        self.push_screen(AddHost(core.ts_devices()), self._added)

    def _added(self, name):
        if not name:
            return
        self._load_compose()
        self._build_cards()
        self.notify(f"created connection '{name}'", severity="information")

    # helpers -------------------------------------------------------------
    def _triple(self, name):
        return next(((n, c, t) for n, c, t in self.conns if n == name), (name, None, None))

    def _reload_conn(self, name):
        defaults, conns = core.parse_compose()
        conn = next((c for c in conns if c.get("name") == name), None)
        if not conn:
            return
        cfg, tgt = core.connection_to_cfg(conn, defaults)
        self.conns = [(n, cfg, tgt) if n == name else (n, c, t)
                      for n, c, t in self.conns]


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
    OsyncDash(local_only=o["local_only"], interval=o["interval"],
              want_pending=not o["fast"]).run()


if __name__ == "__main__":
    main()
