# osync-dash

An interactive terminal dashboard for [osync](https://github.com/deajan/osync)
two-way sync jobs, built with [Textual](https://github.com/Textualize/textual).
**One compose file defines many connections** (docker-compose style), and each
gets its own always-expanded card: merged health + sync-state, both machines
(local + remote over SSH, with hostnames and Tailscale identity), live
↑push/↓pull activity, paths, and the soft-delete/backup safety net.

```
┌ ubuntuserver  ⇄ ──────────────────────────────────────────────────────────┐
│ ● HEALTHY   last sync 3m ago                                               │
│ ↑ push idle          ↓ pull idle                                          │
│ result synced · remote synced · resume 0 clean                            │
│                                                                            │
│ ▎ local   my-laptop    ● online   rsync ✓   ↳ my-laptop · 100.x.y.z       │
│     350 files · 235M    disk ███████──────── 41%  300G free               │
│ ▎ remote  my-server    ● online   rsync ✓   ↳ my-server · 100.a.b.c       │
│     350 files · 235M    disk █████─────────── 28%  534G free              │
│                                                                            │
│ local  ~/docs    remote  ubuntu@my-server:/srv/docs   via Tailscale …     │
└────────────────────────────────────────────────────────────────────────────┘
┌ laptop  → ────────────────────────────────────────────────────────────────┐
│ ● RUNNING  ⠹ running   ↑ push ⠹ transferring…   ↓ pull idle  …            │
└────────────────────────────────────────────────────────────────────────────┘
```

When a sync is running, the ↑push / ↓pull legs animate a spinner (for the
direction that's actually moving data); otherwise they show the queued counts
from the last dry-run. Cards are focusable — actions apply to the focused one.

## Architecture

- **`osync_core.py`** — data layer + one-shot renderer. **Standard library only**,
  runs on the system Python. Powers `--print` and all the probing.
- **`osync_tui.py`** — the interactive [Textual](https://textual.textualize.io)
  app. The one third-party dependency, kept in a project virtualenv.
- **`osync-dash`** — thin launcher: interactive → Textual (venv); otherwise →
  the stdlib one-shot renderer.

So the TUI is a proper app (mouse, resize, background refresh, key bindings),
while `--print` stays dependency-free for scripts, cron, and non-TTY pipes.

## Requirements

- Python 3.11+ (uses stdlib `tomllib`)
- `osync` (`osync.sh` on `PATH` or `/usr/local/bin/osync.sh`)
- `rsync` + `ssh` on both ends (already required by osync)
- The TUI needs `textual` — `install.sh` puts it in a local `.venv`
- Optional: `tailscale` (device names + import dropdown)

## Install

```sh
git clone https://github.com/bk-bf/osync-dash.git
cd osync-dash
./install.sh            # creates .venv, installs Textual, symlinks to ~/.local/bin
```

`./install.sh [BINDIR]` to link somewhere else. The `--print` path works even
without the venv (system Python, stdlib only).

## Usage

```sh
osync-dash              # interactive Textual TUI — every connection, all at once
```

All connections live in a single compose file,
**`~/.config/osync/osync-dash.toml`** — no picker, no prompt. On first run, any
existing `~/.config/osync/*.conf` jobs are folded into it automatically (the old
files are left untouched).

```toml
# ~/.config/osync/osync-dash.toml
[defaults]
user = "ubuntu"
key  = "~/.ssh/id_ed25519"

[[connection]]
name = "ubuntuserver"
local = "~/docs"
remote = "/srv/docs"
direction = "bidir"          # bidir | send | receive
mode = "both"                # ts | ssh | both
ts_host = "ubuntuserver.tailXXXX.ts.net"
ssh_host = "192.168.1.50"

[[connection]]
name = "laptop"
local = "~/notes"
remote = "/home/kirill/notes"
direction = "send"
mode = "ssh"
ssh_host = "laptop.local"
```

Each `[[connection]]` is a two-way osync job between **this machine** and a host
(so a mesh like `server ⇄ desktop ⇄ laptop` is just two connections on the
desktop). osync-dash materialises a real osync `.conf` per connection into
`~/.cache/osync/generated/` when it needs to run osync — the compose file stays
the single source of truth.

In the TUI, actions apply to the **focused card** (move focus with ↑/↓ or `j`/`k`):

| key | action |
|-----|--------|
| `↑`/`↓`, `j`/`k` | move focus between connection cards |
| `r` | refresh all connections |
| `c` | pending-changes dry-run (focused card) |
| `s` | run the sync (suspends to stream osync, then returns) |
| `t` | cycle the endpoint mode (Tailscale / SSH / both) |
| `d` | cycle the sync direction (bidirectional / send → / receive ←) |
| `A` | **auto-sync** picker (off · on-change · periodic — interval or calendar) |
| `a` | add a connection (floating form, appends to the compose file) |
| `e` | edit the focused connection (same form, name fixed) |
| `x` | delete the focused connection (confirms; synced files untouched) |
| `,` | settings (notifications, deletion guard) |
| `l` | page the osync log |
| `q` | quit |

Status refreshes on background threads, so ssh probes never freeze the UI.
Resize and mouse work; it's fine over SSH. The theme follows btop's **ayu**
palette, with gradient disk meters per machine.

## Mesh: add hosts, browse dirs, pick a direction

Press `a` for a setup form that turns osync-dash into a little sync-mesh
controller:

- **Import a device** — a dropdown of your Tailscale peers (online/OS shown), or
  "manual entry". Picking one fills in its host.
- **Endpoint mode** — Tailscale / Plain SSH / **Both**. The form is dynamic: it
  only asks for the hosts the chosen mode needs. In **both** mode osync-dash
  prefers Tailscale and **falls back to plain SSH** automatically when Tailscale
  is unreachable (it repoints `TARGET_SYNC_DIR` to whichever answers).
- **Directory autocomplete** — type in the remote-dir field and it lists real
  subdirectories on that device over ssh (cached, drill in by selecting); the
  local-dir field does the same against your filesystem. No exact typing.
- **Direction** — Bidirectional ⇄, Send → (local→remote), or Receive ←
  (remote→local), mapped to osync's native `SYNC_TYPE`.

Submitting the form appends a `[[connection]]` to the compose file and the new
card appears immediately. On any card, `t` cycles the endpoint mode and `d` the
direction live (both rewrite that connection's entry in the compose file).

## Auto-sync (off · on-change · periodic)

osync is a batch tool — one run, one reconciliation, then it exits. By default
nothing syncs until you press `s`. Press **`A`** on a card to open the auto-sync
picker; osync-dash writes and manages a **systemd `--user`** unit for it:

- **on file change** — runs `osync.sh <conf> --on-changes`, osync's inotify
  monitor, as a long-lived service. Syncs shortly after changes settle.
  (Needs `inotify-tools` / `inotifywait`.)
- **periodic** — a oneshot sync fired by a `.timer`, either:
  - **every** a fixed interval: presets **1 minute → 2 weeks** or a custom
    `90m` / `6h` / `2d` / `1w` (stored as `interval = "6h"`), or
  - **at** a calendar time: presets (daily 03:00, weekdays 09:00, weekly…) or a
    custom systemd `OnCalendar` like `Mon..Fri 18:00` (stored as `at = "…"`).
- **off** — manual only.

Each card's **auto-sync** line shows the live unit state — `next in 4m · last
ok`, or a red `⚠ last run failed` you can jump into with `l`. Units are
`osync-dash-<name>.{service,timer}` under `~/.config/systemd/user/`, and survive
logout/reboot when user lingering is on (`loginctl enable-linger $USER`).

**Scheduled and continuous syncs run through the deletion guard** (below), so an
unattended sync can't quietly mirror a mass-delete.

## Settings & safety net

Press `,` for global settings, stored in `[settings]` of the compose file so
they travel with your config and stay portable across machines:

- **Desktop notifications** — on/off, plus the **command** to use. Defaults to
  `notify-send`; point it at `dunstify`, a wrapper script, or anything taking
  `<cmd> TITLE BODY`. You get a notification when a sync fails or is blocked.
- **Deletion guard** — `delete_guard = N`: before any sync (manual *or*
  automatic) osync-dash checks the dry-run, and if it would propagate more than
  `N` deletions it **blocks and notifies** instead of running. A manual `s`
  turns this into an "are you sure?" you can override; an automatic run just
  skips and flags the card. Set `0` to disable. Guards against the classic
  disaster where an empty or unmounted replica deletes everything on the other
  side.

### Non-interactive

```sh
osync-dash --print          # one-shot render to stdout (automatic when piped)
osync-dash --print --fast    # skip the pending dry-run
osync-dash --sync           # run the sync, print the result, exit
osync-dash --log            # page the osync log, exit
osync-dash --local-only     # offline: skip the remote probe
```

## How it reads status

| Field    | Source |
|----------|--------|
| health   | `*-last-action-<instance>` + `resume-count-<instance>` state files, plus live reachability |
| last run | mtime of the initiator `last-action` state file |
| devices  | system hostname (local `gethostname`, remote `hostname`) + Tailscale device name/IP from `tailscale status` |
| files/size | live walk locally; one combined `ssh` probe remotely |
| free space | `statvfs` locally, `df` remotely |
| safety net | file counts under `.osync_workdir/{deleted,backup}` on both sides |
| pending  | `osync … --dry --summary`, parsed for update/deletion counts |

## License

MIT — see [LICENSE](LICENSE).
