# Noctalia plugin — osync

A [Noctalia](https://noctalia.dev) bar widget that runs `osync-dash --json` and
renders the result. It reimplements nothing — no probing, no health rules, no
log parsing. The same `gather()` / `health()` code that powers the TUI and
`--print` produces the JSON this plugin reads.

```
┌──────────────────────────────────────────┐
│  ● ↑2 ↓0                  ← the bar pill │
└──────────────────────────────────────────┘
   ↑ green = healthy · spins while syncing
     · red = a replica is unreachable
        click ↓
┌──────────────────────────────────────────┐
│ ✓ osync      2 connections · updated 8s ⟳↗│
│ ──────────────────────────────────────── │
│ ▎documents-remote  ⇄            healthy  │
│  last sync 3m ago                ↑2  ↓0  │
│  ● local   my-laptop  350 files · 235M 41%│
│  ● remote  my-server  350 files · 235M 28%│
│  ~/docs  →  ubuntu@my-server:/srv/docs   │
│                                          │
│ ▎claude-sessions   →             stale   │
│  last sync 2d ago                ↑7  ↓0  │
└──────────────────────────────────────────┘
```

## Dependency

**This plugin requires osync-dash itself** — install the parent project first
(repo root `install.sh`), which puts the launcher at `~/.local/bin/osync-dash`.
The plugin runs:

```sh
osync-dash --json [--local-only]
```

`--json` is a stable, additive contract in `osync_core.py`: stdlib only, no ANSI,
nothing but JSON on stdout. It runs the fast path — the probe, but never the
`--check` dry-run — so polling it is cheap relative to a sync.

If the launcher is missing or errors, the widget shows `osync ?` and the panel
prints the failing command and stderr.

## Install

```sh
./install.sh
```

Symlinks the directory into `~/.config/noctalia/plugins/osync-dash`, so the
plugin lives in this repo and updates with it. Then:

1. Noctalia → **Settings → Plugins → Installed** → enable **osync**.
   (Local plugins are discovered by a folder scan and start *disabled*.)
   Enabling automatically adds the pill to your bar.
2. Open its **settings** (gear) if your launcher is somewhere unusual.

Remove with `./install.sh --uninstall`.

## What it shows

**Bar pill** — a status dot on the left, counts to its right. The dot is the
signal: **green** when healthy, **green and spinning** while a sync is actually
running, **red** when a replica is unreachable, amber for a stale/errored job.
The label is configurable:

| Mode | Shows |
|---|---|
| `auto` (default) | Pending `↑push ↓pull`, or `syncing` / `offline` when either applies |
| `health` | Worst health across all connections |
| `counts` | `healthy / total` |
| `changes` | Pending `↑push ↓pull` summed across connections |

While a sync is in flight the widget polls every 3s instead of the usual
interval, so the pill tracks the run and clears promptly — the liveness check is
just a lock-file stat, so this stays cheap.

**Panel** — one card per connection, mirroring the TUI: health stripe and label;
**what the last run actually moved** (`↑2 ↓0`, with deletions as `(−N)` since
osync folds them into each direction's total); `synced … · checked …`; the
mtime-based pending `↑push ↓pull`; both replicas with reachability dot /
hostname / file count / size / disk percent; soft-delete and backup counts when
non-zero; and the paths. While a sync runs, a live "syncing now" banner replaces
the static rows.

**Read-only by design.** Nothing in the bar or panel can start a sync — a stray
click must never move files. Syncing stays an explicit action in the TUI, which
the panel's ↗ button opens.

## Cost

Every probe walks the local tree and ssh's to the remote, so the default
interval is 60s (minimum 15s) rather than the TUI's 6s. If that is still too
much — a laptop on battery, a remote that is often asleep — turn on **Local
only**: it skips the ssh probe entirely, at the cost of pull counts and remote
replica state going unknown.

## Files

| File | Role |
|---|---|
| `Main.qml` | Runs `osync-dash --json` on a timer, parses it, derives every display string. Bar and panel share it via `pluginApi.mainInstance`. |
| `BarWidget.qml` | The pill. Pure presentation. |
| `Panel.qml` | Per-connection cards. |
| `Settings.qml` | Binary path, interval, local-only, bar metric, terminal. |
| `manifest.json` | Entry points + `defaultSettings`. |

## Notes

- Strings are plain English; there is no `i18n/` yet.
- Noctalia's hot reload follows symlinks, so with debug mode on you can edit
  these files in the repo and see changes without restarting the shell.
