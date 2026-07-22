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
                             RadioButton, RadioSet, Select, Static, Switch)
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
DIR_LABEL = {"send": ("→ push to peer", GOLD), "receive": ("← pull from peer", BLUE),
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


def pushpull_line(cfg, tgt, state, local, remote, spin) -> Text:
    """↑push / ↓pull legs from the live probe: files changed on each side since
    the last sync (fast, refreshed every cycle). Spins while a sync is running.
    These are a mtime-based estimate; press c for the exact osync dry-run."""
    running = state.get("running")
    d = core.direction_of(cfg)
    frame = SPIN[spin % len(SPIN)]
    dev_remote = remote if tgt.get("remote") else local
    push_cnt = local.get("changed")            # local changes → push to remote
    pull_cnt = dev_remote.get("changed")       # remote changes → pull to local

    def leg(arrow, verb, cnt, relevant):
        s = Text()
        if not relevant:                        # one-way sync: other leg is off
            s.append(f"{arrow} {verb} ", style=LINE)
            s.append("off", style=LINE)
            return s
        # only show "transferring" for a direction that actually has changes to
        # move (cnt None = never synced yet → assume the first sync moves it).
        # osync works one direction at a time, so with changes on just one side
        # only that leg lights up — never both for nothing.
        if running and (cnt is None or cnt > 0):
            s.append(f"{frame} ", style=GOLD)
            s.append(f"{verb} ", style=f"bold {GOLD}")
            s.append("transferring…", style=GOLD)
            return s
        hot = bool(cnt)
        s.append(f"{arrow} ", style=BLUE if hot else MUTED)
        s.append(f"{verb} ", style=MUTED)
        if cnt is None:
            s.append("—", style=LINE)
        elif hot:
            s.append(f"{cnt} to {verb}", style=BLUE)
        else:
            s.append("in sync", style=MINT)
        return s

    push_rel, pull_rel = d in ("send", "bidir"), d in ("receive", "bidir")
    t = Text("  ")
    t.append_text(leg("↑", "push", push_cnt, push_rel))
    t.append("        ")
    t.append_text(leg("↓", "pull", pull_cnt, pull_rel))
    return t


def _kv(rows, kw=11) -> Table:
    t = Table(box=None, expand=True, show_header=False, pad_edge=False, padding=(0, 0, 0, 2))
    t.add_column(style=MUTED, no_wrap=True, width=kw)
    t.add_column(ratio=1)
    for k, v in rows:
        t.add_row(k, v)
    return t


def auto_line(cfg, auto_st) -> Text:
    auto = cfg.get("_auto", "off")
    if auto == "off":
        return Text("off — manual (press s)", style=MUTED)
    if auto == "change":
        desc = "on file change"
    elif cfg.get("_at"):
        desc = f"at {cfg['_at']}"
    else:
        desc = f"every {cfg.get('_interval', '15min')}"
    st = auto_st or {}
    t = Text()
    if st.get("failed"):
        t.append("⚠ ", style=SALMON)
        t.append(desc, style=SALMON)
        t.append("  · last run failed — check log (l)", style=SALMON)
    elif st.get("active"):
        t.append("⟳ ", style=MINT)
        t.append(desc, style=MINT)
        if st.get("last_ok"):
            t.append("  · last ok", style=MUTED)
    else:
        t.append(desc, style=GOLD)
        t.append("  · not running" + ("" if core.have_systemd() else " (no systemd)"), style=GOLD)
    return t


def card_body(name, cfg, tgt, data, spin, auto_st=None) -> Group:
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
    # what the last run actually moved — parsed from osync's (pinned) log
    lr = state.get("last_run")
    if lr and (lr["pushed"] or lr["pulled"]):
        res_t.append("  ·  moved ", style=MUTED)
        res_t.append(f"↑{lr['pushed']} ↓{lr['pulled']}",
                     style=MINT if lr.get("ok") else SALMON)

    dev_target = remote if tgt.get("remote") else local
    parts = [hdr, pushpull_line(cfg, tgt, state, local, remote, spin), res_t,
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

    rows = [
        ("local", Text(cfg.get("INITIATOR_SYNC_DIR", "?"), style=WHITE)),
        ("remote", Text(tgt_s, style=WHITE)),
        ("via", conn),
        ("auto-sync", auto_line(cfg, auto_st)),
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


class IncomingCard(Static):
    """Read-only card for a push arriving from a peer (discovered via beacon)."""
    def __init__(self, key, from_node, conn):
        super().__init__(id=f"in-{core._slug(key)}", classes="incoming")
        self.border_title = f" ← {from_node} · {conn} "


def incoming_body(inc) -> Group:
    hdr = Text("  ")
    hdr.append("← ", style=BLUE)
    hdr.append(f"from {inc['from_node']}", style=f"bold {WHITE}")
    if inc.get("last_ts"):
        hdr.append(f"    received {core.human_age(core.time.time() - inc['last_ts'])} ago", style=MUTED)
    else:
        hdr.append("    nothing received yet", style=LINE)
    l2 = Text("  ")
    l2.append("into ", style=MUTED)
    l2.append(inc["dir"], style=WHITE)
    if inc.get("files") is not None:
        l2.append(f"  ·  {inc['files']} files", style=MUTED)
    if inc.get("size") is not None:
        l2.append(f" · {core.humansize(inc['size'])}", style=MUTED)
    l3 = Text("  ")
    l3.append("source ", style=MUTED)
    l3.append(f"{inc['from_node']}:{inc.get('source_dir', '')}", style=LINE)
    return Group(hdr, l2, l3)


# ── add-connection modal (dynamic, mesh) ─────────────────────────────────────
MODES = [("Tailscale", "ts"), ("Plain SSH", "ssh"), ("Both", "both")]
# order matches the radio buttons in AddHost (push-first is the mesh default)
DIRS = [("send", "send"), ("receive", "receive"), ("bidir", "bidir")]


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

    def __init__(self, devices, edit=None):
        super().__init__()
        self.devices = devices
        self.edit = edit  # a compose connection dict when editing, else None

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
            yield Static("✎ edit connection" if self.edit else "＋ add a sync connection", classes="h")
            yield Static("adjust hosts, dirs, direction — the name stays fixed" if self.edit
                         else "import a Tailscale device (or enter a host), then pick dirs to sync",
                         classes="sub")
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
                    yield RadioButton("→ Push  (this node → peer, newest wins)", value=True)
                    yield RadioButton("← Pull  (peer → this node)")
                    yield RadioButton("⇄ Bidirectional  (merge)")

                yield Label("name")
                yield Input(placeholder="desktop / nas / vps", id="i_name")

            yield Static("", id="err")
            with Horizontal(id="btns"):
                yield Button("Cancel", id="cancel")
                yield Button("Create", variant="primary", id="create")

    def on_mount(self):
        if self.edit:
            self._prefill(self.edit)
        self._sync_mode_rows()

    def _prefill(self, e):
        def sv(wid, val):
            self.query_one(f"#{wid}", Input).value = str(val or "")
        sv("i_name", e.get("name"))
        self.query_one("#i_name", Input).disabled = True
        sv("i_user", e.get("user"))
        sv("i_key", e.get("key") or os.path.expanduser("~/.ssh/id_ed25519"))
        sv("i_ts", e.get("ts_host")); sv("i_tsport", e.get("ts_port") or "22")
        sv("i_ssh", e.get("ssh_host")); sv("i_sshport", e.get("ssh_port") or "22")
        self.query_one("#i_rpath", DirField).set_value(e.get("remote") or "~/")
        self.query_one("#i_lpath", DirField).set_value(e.get("local") or "~/")
        self._press(self.query_one("#i_mode", RadioSet),
                    [i for i, (_, v) in enumerate(MODES) if v == e.get("mode", "ts")])
        self._press(self.query_one("#i_dir", RadioSet),
                    [i for i, (_, v) in enumerate(DIRS) if v == e.get("direction", "bidir")])

    def _press(self, rs, idxs):
        idx = idxs[0] if idxs else 0
        for i, btn in enumerate(rs.query(RadioButton)):
            btn.value = (i == idx)

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
            if self.edit:
                core.update_connection(name, conn)
            else:
                core.add_connection(conn)
        except FileExistsError as e:
            err.update(str(e)); return
        except Exception as e:  # noqa: BLE001
            err.update(f"error: {e}"); return
        self.dismiss(("edit" if self.edit else "add", name))


# ── auto-sync modal ──────────────────────────────────────────────────────────
AUTO_PRESETS = [("1 minute", "1m"), ("5 minutes", "5m"), ("15 minutes", "15m"),
                ("30 minutes", "30m"), ("1 hour", "1h"), ("6 hours", "6h"),
                ("12 hours", "12h"), ("24 hours", "24h"), ("2 days", "2d"),
                ("3 days", "3d"), ("1 week", "1w"), ("2 weeks", "2w")]
CAL_PRESETS = [("hourly", "hourly"), ("daily 03:00", "*-*-* 03:00:00"),
               ("weekdays 09:00", "Mon..Fri 09:00"), ("weekends 12:00", "Sat,Sun 12:00"),
               ("weekly · Mon 03:00", "Mon *-*-* 03:00:00"),
               ("monthly · 1st 04:00", "*-*-01 04:00:00")]
AUTO_RADIO = [("off", "Off — manual only"),
              ("change", "On file change  (live, inotify)"),
              ("periodic", "Scheduled…")]
SCHED_RADIO = [("interval", "Every…  (fixed interval)"),
               ("calendar", "At…  (calendar: times / weekdays)")]


class AutoSyncModal(ModalScreen):
    CSS = """
    AutoSyncModal { align: center middle; background: $background 55%; }
    #ab { width: 66; height: auto; max-height: 90%; padding: 1 2; background: $panel;
          border: round $secondary; }
    #ab .h { color: $secondary; text-style: bold; }
    #ab .sub { color: $foreground-muted; margin-bottom: 1; }
    #ab Label { color: $foreground-muted; margin-top: 1; }
    #ab RadioSet { border: none; height: auto; }
    #sched, #iv_row, #cal_row { height: auto; }
    #ab Input { border: tall $panel-lighten-2; }
    #abtns { height: auto; align-horizontal: right; margin-top: 1; }
    #abtns Button { margin-left: 2; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, name, cur_mode, cur_interval, cur_at=""):
        super().__init__()
        self.cname = name
        self.cur_mode = cur_mode if cur_mode in ("off", "change", "periodic") else "off"
        self.cur_interval = cur_interval or "15m"
        self.cur_at = cur_at or ""
        self.iv_match = next((v for _, v in AUTO_PRESETS if v == self.cur_interval), None)
        self.cal_match = next((v for _, v in CAL_PRESETS if v == self.cur_at), None)

    def compose(self) -> ComposeResult:
        with Vertical(id="ab"):
            yield Static(f"⟳ auto-sync · {self.cname}", classes="h")
            yield Static("how should this connection sync on its own?", classes="sub")
            with RadioSet(id="a_mode"):
                for key, label in AUTO_RADIO:
                    yield RadioButton(label, value=(key == self.cur_mode))
            with Vertical(id="sched"):
                with RadioSet(id="a_sched"):
                    for key, label in SCHED_RADIO:
                        yield RadioButton(label, value=(key == ("calendar" if self.cur_at else "interval")))
                with Vertical(id="iv_row"):
                    yield Label("run every (pick one, or type a custom interval)")
                    yield Select(AUTO_PRESETS, prompt="pick an interval…", id="a_preset", allow_blank=True)
                    yield Input(value="" if self.iv_match else self.cur_interval,
                                placeholder="custom: 90m · 3d · 2w", id="a_custom")
                with Vertical(id="cal_row"):
                    yield Label("run at (pick one, or a custom systemd OnCalendar)")
                    yield Select(CAL_PRESETS, prompt="pick a schedule…", id="a_calpreset", allow_blank=True)
                    yield Input(value="" if self.cal_match else self.cur_at,
                                placeholder="custom: Mon..Fri 18:00 · *-*-* 02:30:00", id="a_calcustom")
            with Horizontal(id="abtns"):
                yield Button("Cancel", id="a_cancel")
                yield Button("Save", variant="primary", id="a_save")

    def on_mount(self):
        if self.iv_match:
            self.query_one("#a_preset", Select).value = self.iv_match
        if self.cal_match:
            self.query_one("#a_calpreset", Select).value = self.cal_match
        self._sync_vis()

    def _mode(self):
        return AUTO_RADIO[self.query_one("#a_mode", RadioSet).pressed_index or 0][0]

    def _sched_kind(self):
        return SCHED_RADIO[self.query_one("#a_sched", RadioSet).pressed_index or 0][0]

    def _sync_vis(self):
        periodic = self._mode() == "periodic"
        self.query_one("#sched").display = periodic
        self.query_one("#iv_row").display = periodic and self._sched_kind() == "interval"
        self.query_one("#cal_row").display = periodic and self._sched_kind() == "calendar"

    @on(RadioSet.Changed)
    def _radio_changed(self, e):
        self._sync_vis()

    def action_cancel(self):
        self.dismiss(None)

    @on(Button.Pressed, "#a_cancel")
    def _cancel(self):
        self.dismiss(None)

    @on(Button.Pressed, "#a_save")
    def _save(self):
        mode = self._mode()
        interval = calendar = None
        if mode == "periodic":
            if self._sched_kind() == "calendar":
                cc = self.query_one("#a_calcustom", Input).value.strip()
                cs = self.query_one("#a_calpreset", Select).value
                calendar = cc or (cs if cs != Select.BLANK else self.cur_at) or "daily"
            else:
                ic = self.query_one("#a_custom", Input).value.strip()
                iss = self.query_one("#a_preset", Select).value
                interval = ic or (iss if iss != Select.BLANK else self.cur_interval)
        self.dismiss((mode, interval, calendar))


# ── settings + confirm modals ────────────────────────────────────────────────
class SettingsModal(ModalScreen):
    CSS = """
    SettingsModal { align: center middle; background: $background 55%; }
    #sb { width: 66; height: auto; padding: 1 2; background: $panel; border: round $primary; }
    #sb .h { color: $primary; text-style: bold; }
    #sb .sub { color: $foreground-muted; margin-bottom: 1; }
    #sb Label { color: $foreground-muted; margin-top: 1; }
    #sb Input { border: tall $panel-lighten-2; }
    .switchrow { height: auto; margin-top: 1; }
    .switchrow Label { width: 1fr; margin-top: 1; }
    #err2 { color: $error; height: auto; }
    #sbtns { height: auto; align-horizontal: right; margin-top: 1; }
    #sbtns Button { margin-left: 2; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, settings):
        super().__init__()
        self.settings = settings

    def compose(self) -> ComposeResult:
        s = self.settings
        with Vertical(id="sb"):
            yield Static("⚙ settings", classes="h")
            yield Static("global · saved to [settings] in the compose file", classes="sub")
            with Horizontal(classes="switchrow"):
                yield Label("desktop notifications (sync failures + blocked syncs)")
                yield Switch(value=bool(s.get("notify", True)), id="s_notify")
            yield Label("notify command — portable: notify-send · dunstify · a script")
            yield Input(value=str(s.get("notify_cmd", "notify-send")), id="s_cmd")
            yield Label("block a sync that would delete more than N files (0 = off)")
            yield Input(value=str(s.get("delete_guard", 25)), id="s_guard")
            yield Static("", id="err2")
            with Horizontal(id="sbtns"):
                yield Button("Cancel", id="s_cancel")
                yield Button("Save", variant="primary", id="s_save")

    def action_cancel(self):
        self.dismiss(None)

    @on(Button.Pressed, "#s_cancel")
    def _cancel(self):
        self.dismiss(None)

    @on(Button.Pressed, "#s_save")
    def _save(self):
        guard = self.query_one("#s_guard", Input).value.strip()
        if not guard.isdigit():
            self.query_one("#err2", Static).update("deletion guard must be a whole number (0 = off)")
            return
        self.dismiss({
            "notify": self.query_one("#s_notify", Switch).value,
            "notify_cmd": self.query_one("#s_cmd", Input).value.strip() or "notify-send",
            "delete_guard": int(guard),
        })


class ConfirmModal(ModalScreen):
    CSS = """
    ConfirmModal { align: center middle; background: $background 65%; }
    #cb { width: 62; height: auto; padding: 1 2; background: $panel; border: round $warning; }
    #cb .h { color: $warning; text-style: bold; margin-bottom: 1; }
    #cbtns { height: auto; align-horizontal: right; margin-top: 1; }
    #cbtns Button { margin-left: 2; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title, body, ok_label="Confirm", ok_variant="error"):
        super().__init__()
        self.title_text = title
        self.body_text = body
        self.ok_label = ok_label
        self.ok_variant = ok_variant

    def compose(self) -> ComposeResult:
        with Vertical(id="cb"):
            yield Static(self.title_text, classes="h")
            yield Static(self.body_text)
            with Horizontal(id="cbtns"):
                yield Button("Cancel", id="c_no")
                yield Button(self.ok_label, variant=self.ok_variant, id="c_yes")

    def action_cancel(self):
        self.dismiss(False)

    @on(Button.Pressed, "#c_no")
    def _no(self):
        self.dismiss(False)

    @on(Button.Pressed, "#c_yes")
    def _yes(self):
        self.dismiss(True)


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
    #meshdiv { height: auto; padding: 1 1 0 1; color: $foreground-muted; text-style: bold; }
    IncomingCard {
        height: auto; margin: 0 0 1 0; padding: 1 1; background: $panel;
        border: round $accent; border-title-color: $accent;
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
        ("e", "edit_host", "Edit"),
        ("x", "delete_host", "Delete"),
        ("l", "log", "Log"),
        ("comma", "settings", "Settings"),
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
        self.auto = {}                 # name -> auto_status dict
        self.incoming = []             # probed incoming-push dicts (peers → me)
        self._spin = 0
        self._load_compose()

    def _load_compose(self):
        self.conns = core.load_all()
        node = core.parse_settings().get("node") or core.socket.gethostname()
        self.title = f"osync-dash · {node}"   # this node's view of the mesh
        n = len(self.conns)
        self.sub_title = f"{n} connection{'' if n == 1 else 's'}"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield VerticalScroll(id="cards")
        yield Footer()

    def on_mount(self):
        self.register_theme(AYU)
        self.theme = "ayu"
        self._build_cards()                                     # also discovers incoming
        self._advertise()                                       # tell peers I push to them
        self.set_interval(self.interval, self.action_refresh)   # slow: probe (ssh)
        self.set_interval(1.0, self._clock_tick)                # fast: live time
        self.set_interval(0.12, self._tick)                     # spinner animation
        self.set_interval(1.5, self._poll_running)              # live sync detection

    # cards ---------------------------------------------------------------
    def _build_cards(self):
        box = self.query_one("#cards", VerticalScroll)
        box.remove_children()
        if not self.conns:
            box.mount(Static(
                "No outgoing connections on this node yet.\n\n"
                "Press  a  to add one (a push to a peer). Incoming pushes from\n"
                "other nodes appear below automatically.",
                id="empty"))
        first = None
        for name, cfg, _ in self.conns:
            card = ConnectionCard(name, core.direction_of(cfg))
            box.mount(card)
            card.update(Text("  gathering…", style=MUTED))
            first = first or card
        for name, cfg, tgt in self.conns:
            self.refresh_one(name, cfg, tgt)
        if first:
            first.focus()
        self._refresh_incoming()  # re-add incoming cards after the rebuild

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
                              self._spin, self.auto.get(name)))

    def _tick(self):
        """Advance the spinner; only re-render cards with a sync in progress."""
        self._spin += 1
        for name in list(self.data):
            d = self.data.get(name)
            if d and d[0].get("running"):
                self._render_card(name)

    def _clock_tick(self):
        """Re-render every card each second so the time fields (last sync N ago,
        next in N, disk/counts labels) stay live — pure render from cached data,
        no probing. The expensive ssh gather stays on its own slow interval."""
        for name in list(self.data):
            self._render_card(name)

    # workers -------------------------------------------------------------
    @work(thread=True, group="refresh", exclusive=False)
    def refresh_one(self, name, cfg, tgt):
        sync_dir = os.path.expanduser(cfg.get("INITIATOR_SYNC_DIR", ""))
        if core.sync_running(sync_dir):
            # a sync is in progress — don't probe. osync is rewriting the tree
            # and using the ssh link, so a probe now races it and the in-flight
            # numbers are meaningless. The 1.5 s lock poll keeps the spinner
            # going; _apply_running re-probes the moment the sync finishes.
            self.call_from_thread(self._mark_running, name)
            return
        try:
            data = core.gather(cfg, tgt, self.local_only)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.notify, f"{name}: refresh failed: {e}", severity="error")
            return
        try:
            auto = core.auto_status(name)
        except Exception:  # noqa: BLE001
            auto = None
        self.call_from_thread(self._apply, name, data, auto)

    def _mark_running(self, name):
        d = self.data.get(name)
        if d and not d[0].get("running"):
            d[0]["running"] = True
            self._render_card(name)

    @work(thread=True, group="pending", exclusive=False)
    def check_pending(self, name):
        """On-demand authoritative dry-run — the exact osync figure (incl.
        deletions/excludes the live estimate can't see), shown as a toast."""
        cfg = next((c for n, c, _ in self.conns if n == name), None)
        if cfg is None:
            return
        self.call_from_thread(self.notify, f"{name}: exact dry-run…", timeout=2)
        try:
            p = core.compute_pending(cfg["_configfile"])
        except Exception:  # noqa: BLE001
            p = None
        if p is None:
            msg, sev = "dry-run failed (target unreachable?)", "warning"
        elif p["total"] == 0:
            msg, sev = "in sync — nothing pending", "information"
        else:
            msg = (f"↑ push {p['tu'] + p['td']} (del {p['td']})  ·  "
                   f"↓ pull {p['iu'] + p['id']} (del {p['id']})")
            sev = "information"
        self.call_from_thread(self.notify, f"{name} — {msg}", severity=sev, timeout=6)

    def _apply(self, name, data, auto=None):
        self.data[name] = data
        if auto is not None:
            self.auto[name] = auto
        self._render_card(name)

    @work(thread=True, group="running", exclusive=True)
    def _poll_running(self):
        """Cheap lock-file poll so the ↑push/↓pull spinner catches short syncs
        the 6 s full refresh would miss. No ssh, no dry-run."""
        upd = {}
        for name, cfg, _ in self.conns:
            sd = os.path.expanduser(cfg.get("INITIATOR_SYNC_DIR", ""))
            try:
                upd[name] = core.sync_running(sd)
            except Exception:  # noqa: BLE001
                pass
        if upd:
            self.call_from_thread(self._apply_running, upd)

    def _apply_running(self, upd):
        for name, running in upd.items():
            d = self.data.get(name)
            if not d or d[0].get("running") == running:
                continue
            was = d[0].get("running")
            d[0]["running"] = running
            self._render_card(name)
            if was and not running:
                # a sync just finished — re-probe so the live counts settle
                self.refresh_one(*self._triple(name))

    # actions -------------------------------------------------------------
    def _focused_name(self):
        f = self.focused
        if isinstance(f, ConnectionCard):
            return f.conn_name
        return self.conns[0][0] if self.conns else None

    def action_refresh(self):
        for name, cfg, tgt in self.conns:
            self.refresh_one(name, cfg, tgt)
        self._refresh_incoming()

    # mesh (incoming pushes from peers) -----------------------------------
    @work(thread=True, group="advertise", exclusive=True)
    def _advertise(self):
        try:
            core.advertise_all()
        except Exception:  # noqa: BLE001
            pass

    @work(thread=True, group="incoming", exclusive=True)
    def _refresh_incoming(self):
        try:
            data = [core.probe_incoming(b) for b in core.incoming_beacons()]
        except Exception:  # noqa: BLE001
            data = []
        self.call_from_thread(self._apply_incoming, data)

    def _apply_incoming(self, data):
        self.incoming = data
        box = self.query_one("#cards", VerticalScroll)
        for w in list(box.query(IncomingCard)) + list(box.query("#meshdiv")):
            w.remove()
        if not data:
            return
        for w in list(box.query("#empty")):  # a receive-only node isn't "empty"
            w.remove()
        box.mount(Static("── incoming pushes (peers → this node) ──", id="meshdiv"))
        for inc in data:
            card = IncomingCard(f"{inc['from_node']}-{inc['connection']}",
                                inc["from_node"], inc["connection"])
            box.mount(card)
            card.update(incoming_body(inc))

    def action_check(self):
        name = self._focused_name()
        if name:
            self.check_pending(name)

    def action_sync(self):
        name = self._focused_name()
        cfg = next((c for n, c, _ in self.conns if n == name), None)
        if not cfg:
            return
        guard = int(core.parse_settings().get("delete_guard", 0) or 0)
        if guard <= 0:
            self._run_sync(name, cfg)
            return
        # deletion guard: dry-run first, confirm if it would delete a lot.
        self.notify("checking changes before sync…", timeout=2)
        self._guarded_manual_sync(name, cfg, guard)

    @work(thread=True, group="syncguard", exclusive=False)
    def _guarded_manual_sync(self, name, cfg, guard):
        try:
            p = core.compute_pending(cfg["_configfile"])
        except Exception:  # noqa: BLE001
            p = None
        dels = core.pending_deletions(p)
        if dels > guard:
            self.call_from_thread(self._confirm_big_delete, name, cfg, dels, guard)
        else:
            self.call_from_thread(self._run_sync, name, cfg)

    def _confirm_big_delete(self, name, cfg, dels, guard):
        body = (f"This sync would propagate [b]{dels} deletions[/b] (guard is {guard}).\n"
                f"Deletions apply to both replicas. Continue?")
        self.push_screen(
            ConfirmModal("⚠ large deletion", body, ok_label="Sync anyway"),
            lambda ok: self._run_sync(name, cfg) if ok else None)

    def _run_sync(self, name, cfg):
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
        order = ["send", "receive", "bidir"]
        cur = core.direction_of(cfg)
        new = order[(order.index(cur) + 1) % 3] if cur in order else "send"
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
            AutoSyncModal(name, cfg.get("_auto", "off"), cfg.get("_interval", "15m"),
                          cfg.get("_at", "")),
            lambda res: self._apply_auto(name, res))

    def _apply_auto(self, name, res):
        if res is None:
            return
        mode, interval, calendar = res
        self.notify("configuring auto-sync…", timeout=2)
        self._do_set_auto(name, mode, interval, calendar)

    @work(thread=True, group="auto", exclusive=False)
    def _do_set_auto(self, name, mode, interval, calendar):
        _, msg = core.set_auto(name, mode, interval, calendar)
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

    def action_edit_host(self):
        name = self._focused_name()
        if not name:
            return
        _, conns = core.parse_compose()
        conn = next((c for c in conns if c.get("name") == name), None)
        if conn:
            self.push_screen(AddHost(core.ts_devices(), edit=conn), self._added)

    def _added(self, result):
        if not result:
            return
        action, name = result
        self._load_compose()
        self._build_cards()
        if action == "edit":
            # regenerate any running auto units against the edited config
            _, conns = core.parse_compose()
            conn = next((c for c in conns if c.get("name") == name), None)
            if conn and core.auto_mode(conn) != "off":
                self._do_set_auto(name, core.auto_mode(conn), conn.get("interval"), conn.get("at"))
            self.notify(f"updated '{name}'", severity="information")
        else:
            self.notify(f"created connection '{name}'", severity="information")
        self._advertise()  # (re)publish this node's push beacons to peers

    def action_delete_host(self):
        name = self._focused_name()
        if not name:
            return
        body = (f"Delete connection [b]{name}[/b]?\n\nThis removes it from the compose "
                f"file and stops any auto-sync unit. Your synced files are left untouched.")
        self.push_screen(ConfirmModal("🗑 delete connection", body, ok_label="Delete"),
                         lambda ok: self._delete(name) if ok else None)

    @work(thread=True, group="delete", exclusive=False)
    def _delete(self, name):
        try:
            defaults, conns = core.parse_compose()
            conn = next((c for c in conns if c.get("name") == name), None)
            if conn:
                core.unadvertise(conn, defaults)  # retract the beacon from the peer
            core.remove_connection(name)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.notify, f"delete failed: {e}", severity="error")
            return
        self.call_from_thread(self._after_delete, name)

    def _after_delete(self, name):
        for d in (self.data, self.auto):
            d.pop(name, None)
        self._load_compose()
        self._build_cards()
        self.notify(f"deleted '{name}'", severity="information")

    def action_settings(self):
        self.push_screen(SettingsModal(core.parse_settings()), self._saved_settings)

    def _saved_settings(self, updates):
        if updates is None:
            return
        core.save_settings(updates)
        self.notify("settings saved", severity="information")

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
