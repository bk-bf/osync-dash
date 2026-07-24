# osync-dash

An interactive terminal dashboard for [osync](https://github.com/deajan/osync)
two-way sync jobs, built with [Textual](https://github.com/Textualize/textual).
**One compose file defines many connections** (docker-compose style), and each
gets its own always-expanded card: merged health + sync-state, both machines
(local + remote over SSH, with hostnames and Tailscale identity), live
↑push/↓pull activity, paths, and the soft-delete/backup safety net. Ships with
[`yeet`](#bonus-yeet--one-shot-drops-across-the-mesh) for one-shot file drops
between the same machines.

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
  runs on the system Python. Powers `--print`, `--json`, all the probing, and the
  log parsing.
- **`osync_tui.py`** — the interactive [Textual](https://textual.textualize.io)
  app. The one third-party dependency, kept in a project virtualenv.
- **`osync-dash`** — thin launcher: interactive → Textual (venv); otherwise →
  the stdlib one-shot renderer.
- **`yeet`** — one-shot file drops across the tailnet (see below). Standard
  library only, and it borrows `osync_core`'s tailnet/ssh/compose plumbing.
- **`versions.env`** — pinned dependency versions (osync ref + Textual), read by
  `install.sh`. The single place to bump versions.
- **`noctalia-plugin/`** — optional [Noctalia](https://noctalia.dev) bar widget
  (see below). Depends on this project; ships with it rather than as its own repo.

So the TUI is a proper app (mouse, resize, background refresh, key bindings),
while `--print` stays dependency-free for scripts, cron, and non-TTY pipes, and
`--json` feeds the desktop widget — three front-ends over one data layer.

## Machine-readable output (`--json`)

```sh
osync-dash --json [--local-only]
```

Emits every connection as one JSON object on stdout and exits — stdlib only, no
ANSI, nothing else printed. It runs the fast path (the probe, never the `--check`
dry-run), so it is cheap enough to poll.

```jsonc
{
  "schema": 1,
  "generated_at": 1784773925,
  "host": "cachyos-x8664",
  "summary": { "total": 2, "running": 0, "healthy": 2,
               "problems": 0, "worst": "HEALTHY", "worst_color": "green" },
  "connections": [
    {
      "name": "documents-remote",
      "direction": "bidir",            // bidir | send | receive
      "health": "HEALTHY",             // same labels the TUI shows
      "color": "green",
      "running": false,
      "last_sync_ts": 1784689897.0,
      "last_sync_age": 84028,          // seconds since data last moved
      "last_check_ts": 1784776515.0,
      "last_check_age": 272,           // since in-sync-ness was last confirmed
      "push_changes": 0,               // live ↑ count since the last sync
      "pull_changes": 0,               // live ↓ count
      "paths":  { "local": "~/docs", "remote": "ssh://user@host:22//srv/docs" },
      "local":  { "reach": true, "host": "...", "files": 350, "size": 245020870,
                  "disk_total": 511793483776, "disk_used": 0, "free": 0,
                  "deleted": 0, "backup": 0, "ts": { "name": "...", "ip": "..." } },
      "remote": { "reach": true, "host": "...", "files": 350, "...": "..." },
      "state":  { "init_action": "synced", "tgt_action": "synced",
                  "resume": "0", "last_run": { "pushed": 2, "pulled": 0 } }
    }
  ]
}
```

`summary.worst` is the most-alarming health across all connections, so a status
indicator can colour itself without re-deriving severity. **The schema is
additive** — fields get added, never renamed or dropped.

### Synced vs. checked

These are deliberately two different timestamps. A periodic sync runs
`--guarded-sync`, which dry-runs first and **skips the real osync pass when
nothing needs moving** — so `last_sync_*` stops advancing while a connection is
healthy but idle, and only the dry-run state files get touched.

Health is therefore judged on `last_check_*` ("when did we last confirm both
replicas match"), not `last_sync_*` ("when did data last move"). Judging it on
the latter would flip every quiet connection to `STALE` after 24 h even though it
was verified in sync moments earlier.

## Noctalia plugin (optional)

`noctalia-plugin/` is a desktop widget for the [Noctalia](https://noctalia.dev)
shell that runs `osync-dash --json` and renders sync health in your bar, with a
click-through panel showing every connection. It reimplements nothing — same
probing, same health rules.

```sh
cd noctalia-plugin && ./install.sh
```

Entirely optional — nothing else in this project depends on it. See
[`noctalia-plugin/README.md`](noctalia-plugin/README.md).

## Requirements

- Python 3.11+ (uses stdlib `tomllib`)
- `git`, `rsync`, `ssh` (git only for install; rsync/ssh are osync's own deps)
- Optional: `tailscale` (device names + import dropdown; not required by
  [`yeet`](#transport)), `inotify-tools` (for on-change auto-sync)

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
4. symlinks `osync-dash` and [`yeet`](#bonus-yeet--one-shot-drops-across-the-mesh)
   into `~/.local/bin`.

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

In the TUI, actions apply to the **focused card** (move focus with ↑/↓ or `j`/`k`).
Press **`?`** in the app for this list (the footer stays minimal — just `?` and `q`):

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

**Netmap-style POV.** Run osd on every node and each shows the mesh from *its*
side: **outgoing** cards (the pushes in this node's compose) and **incoming**
cards (peers pushing *to* this node). Incoming is discovered with no central
registry — when a node sets up a push, it drops a tiny beacon
(`~/.config/osync/incoming/<node>--<conn>.toml`) on the target over ssh; the
target reads those and shows `← from <node>` cards with the received dir's file
count and freshness. So from a receive-only box like a server you'll see
`← cachyos : documents → /srv/docs (350 files, newest 2d ago)` even though it has
no connections of its own.

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

## Bonus: `yeet` — one-shot drops across the mesh

Connections are for folders that should *stay* the same. `yeet` is for the other
half: moving one file to whichever machine you happen to be at next. One verb —
with paths it sends, without them it receives. It installs with everything else,
so every node that has osync-dash has it already.

```sh
yeet report.pdf          # on the laptop
yeet                     # on any other node; it lands in the current directory
```

No target host, no passphrase, no code to type. The file is rsynced to a spool
on a **hub** — an always-on node in the tailnet — and the receiving side pulls it
down and clears it. Because the hub holds the file, **the sender can go offline
the moment the upload finishes**; nothing needs both ends awake at once, and no
node has to accept inbound connections.

```
[laptop]              [hub]                 [desktop]
yeet report.pdf  ─────▶  spool/
                         spool/  ─────▶  yeet  →  ./report.pdf
```

Directories work (`yeet ~/notes`), so do several paths at once, and rsync brings
its own delta transfer, `--partial` resume and progress meter.

| flag | |
|---|---|
| `-l`, `--list` | show what is waiting, transfer nothing |
| `-a`, `--all` | receive every waiting drop, not just the newest |
| `-n NAME` | receive the drop whose name contains `NAME` |
| `-k`, `--keep` | receive without clearing it — fan the same drop out to several machines |
| `-f`, `--force` | overwrite existing files in the target directory |
| `--rm [NAME]` | delete a waiting drop without receiving it (`--all` for every) |
| `--hub HOST` | set the hub host |

The hub is stored as `yeet_hub` in `[settings]` of the compose file, and is
guessed on first use from the host your existing connections already talk to.
On the hub itself the spool is a plain local directory — no ssh round trip.

### Transport

`yeet` is rsync over ssh, so **it needs no Tailscale**. Anything that gets you an
ssh connection to the hub works the same: plain SSH on a LAN, a hand-rolled
WireGuard tunnel, a jump host, an `~/.ssh/config` alias, whatever. Tailscale is
used for exactly two conveniences when it happens to be there — labelling drops
with the node's tailnet name instead of its hostname, and recognising that the
hub *is* this machine so the spool can be used locally. Both degrade to the
plain-ssh behaviour when `tailscale` is absent.

The one requirement is that every node can **reach the hub outbound over ssh**.
Nothing ever connects to a laptop, so no node needs an open inbound port.

### Interrupted transfers resume

Nothing is thrown away when a transfer dies. **Uploads** are announced before the
payload moves and stay marked unfinished until it is all there, so a drop in
flight is visible but cannot be picked up:

```
$ yeet -l
  linux.iso  4.4G · 1 file  ← laptop  2m ago   ⋯ uploading 38%
```

Sending the same paths again **resumes that drop** rather than starting a second
one — rsync only moves what is missing. Abandoned uploads are binned after a
week, or immediately with `yeet --rm`.

**Downloads** resume the same way. In-flight chunks are parked in a `.yeet-part`
directory instead of landing in the destination truncated, so an interrupted
`yeet` never leaves something that looks like a finished file, and re-running it
picks up where it stopped. The drop stays on the hub until the transfer actually
completes.

A receive also **refuses** rather than overwriting a file that already exists in
the target directory — `-f` overrides.

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
