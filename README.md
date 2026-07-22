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
│ ↑ push in sync       ↓ pull in sync                                       │
│ result synced · remote synced · resume 0 clean · moved ↑2 ↓0              │
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

When a sync is running, the ↑push / ↓pull legs animate a spinner (detected live
from osync's lock file). Otherwise they show **live counts of files changed
since the last sync** — computed the git way: local changes (→ push) come from
comparing the local tree's mtimes against the last-sync baseline in the same
walk that already runs; remote changes (→ pull) from a fast `find -newermt` in
the same ssh probe. No dry-run, refreshed every cycle. These are an mtime-based
estimate (they don't see deletions or excludes) — press `c` for the exact osync
dry-run (updates + deletions per direction). Cards are focusable — actions apply
to the focused one.

## Architecture

- **`osync_core.py`** — data layer + one-shot renderer. **Standard library only**,
  runs on the system Python. Powers `--print`, all the probing, and the log parsing.
- **`osync_tui.py`** — the interactive [Textual](https://textual.textualize.io)
  app. The one third-party dependency, kept in a project virtualenv.
- **`osync-dash`** — thin launcher: interactive → Textual (venv); otherwise →
  the stdlib one-shot renderer.
- **`versions.env`** — pinned dependency versions (osync ref + Textual), read by
  `install.sh`. The single place to bump versions.

So the TUI is a proper app (mouse, resize, background refresh, key bindings),
while `--print` stays dependency-free for scripts, cron, and non-TTY pipes.

## Requirements

- Python 3.11+ (uses stdlib `tomllib`)
- `git`, `rsync`, `ssh` (git only for install; rsync/ssh are osync's own deps)
- Optional: `tailscale` (device names + import dropdown), `inotify-tools` (for
  on-change auto-sync)

You do **not** need to install osync yourself — the installer vendors a pinned
build (see below).

## Install

One line, no clone needed:

```sh
curl -fsSL https://raw.githubusercontent.com/bk-bf/osync-dash/main/install.sh | bash
```

The installer:

1. fetches osync-dash into `~/.local/share/osync-dash/`,
2. **vendors the pinned osync** (`versions.env`) into `…/osync/osync.sh`,
3. creates the Textual `.venv` with the pinned Textual,
4. symlinks `osync-dash` into `~/.local/bin`.

Re-run any time — it's idempotent. `install.sh [BINDIR]` links elsewhere. From a
clone, `./install.sh` works the same. `--print` still runs on the system Python
(stdlib only) even without the venv.

### Version pinning & replicas

Everything osync-dash runs uses the osync build pinned in `versions.env`, so
behaviour — and osync's **log format**, which the dashboard parses — is identical
on every machine. To put that same byte-identical osync on a replica (osync.sh
is a single self-contained script):

```sh
~/.local/share/osync-dash/src/install.sh --remote user@host
```

Bump a version by editing `versions.env` and re-running `install.sh` (and
`--remote` for each replica).

### Uninstall

```sh
~/.local/share/osync-dash/src/install.sh --uninstall
```

Prompts, then removes **everything on this machine** it created: the launcher
symlink, the install prefix (vendored osync + venv), all `osync-dash-*` systemd
units (stopped + disabled), the generated confs and logs, the compose file, and
`.osync_workdir` in each local synced dir. Add `--purge-remote` to also wipe
`.osync_workdir` on the replicas over ssh, or `--yes` to skip the prompt. **Your
actual synced files are never touched** — only osync's state/backup dirs are
(note that removes the soft-delete/conflict-backup safety net). Hand-written
legacy `~/.config/osync/*.conf` are also left alone.

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
| `d` | cycle the direction (push → / pull ← / bidirectional ⇄) |
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

## Mesh model: every node pushes its own changes

osync-dash is meant to run on **every device in the mesh** (Tailscale-style),
and the dashboard is titled with the node you're on (`osync-dash · <hostname>`)
— you see the mesh from *this node's* point of view.

The default topology is **push, not bidirectional**: each device is the sole
author of its own files and just **pushes them out** to peers. A two-way folder
is simply *two* pushes — one owned by each side — so there's no central
initiator and no shared lock to fight over. "Pulling" a peer's change is just
that peer pushing to you.

Push/pull connections carry rsync `--update`, which means **newest change wins**
without a merge step: a push never overwrites a file that's newer on the
receiver, so whichever version is newest ends up on both sides regardless of who
pushes last. (Bidirectional stays available for the rare shared-file folder that
needs osync's full conflict merge.) Ideal when each device owns distinct files
(e.g. per-device session logs); for the same file edited on two devices before
either pushes, the newer edit wins and the older is kept as a conflict backup.

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
- **Direction** — **Push →** (this node → peer; the mesh default), Pull ←, or
  Bidirectional ⇄, mapped to osync's native `SYNC_TYPE`.

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
| ↑push/↓pull (live) | files changed since the last sync — local mtime walk + remote `find -newermt` |
| moved (last run) | osync's own log, parsed for the last completed run's updates/deletions per direction (reliable thanks to the pinned osync) |
| pending (exact) | `osync … --dry --summary` on demand (`c`), for exact updates + deletions |

## License

MIT — see [LICENSE](LICENSE).
