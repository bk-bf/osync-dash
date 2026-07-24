# Noctalia plugin — osync

A [Noctalia](https://noctalia.dev) bar widget that runs `osd --json` and
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
┌────────────────────────────────────────────┐
│ ● osync     2 connections · updated 8s ⟳ ↗ │
│ ┌────────────────────────────────────────┐ │
│ │ documents_remote  ⇄                    │ │
│ │ ● HEALTHY    last sync 3m ago          │ │
│ │ ↑ push in sync      ↓ pull in sync     │ │
│ │ local synced · remote synced · resume  │ │
│ │   0 clean · moved ↑2 ↓0                │ │
│ │                                        │ │
│ │ ▎ local   my-laptop  ● online  rsync ✓ │ │
│ │     350 files · 235M  disk ▓▓▓░ 41%    │ │
│ │ ▎ remote  my-server  ● online  rsync ✓ │ │
│ │     350 files · 235M  disk ▓▓░░ 28%    │ │
│ │                                        │ │
│ │ local      ~/docs                      │ │
│ │ remote     ubuntu@my-server:/srv/docs  │ │
│ │ via        Tailscale                   │ │
│ │ auto-sync  every 1min                  │ │
│ │ safety     soft-delete on 0/0 30d …    │ │
│ │ log        ~/.cache/osync/…            │ │
│ └────────────────────────────────────────┘ │
└────────────────────────────────────────────┘
```

## Dependency

**This plugin requires osd itself** — install the parent project first
(repo root `install.sh`), which puts the launcher at `~/.local/bin/osd`.
The plugin runs:

```sh
osd --json [--local-only]
```

`--json` is a stable, additive contract in `osd_core.py`: stdlib only, no ANSI,
nothing but JSON on stdout. It runs the fast path — the probe, but never the
`--check` dry-run — so polling it is cheap relative to a sync.

If the launcher is missing or errors, the widget shows `osync ?` and the panel
prints the failing command and stderr.

## Install

```sh
./install.sh
```

Symlinks the directory into `~/.config/noctalia/plugins/osd`, so the
plugin lives in this repo and updates with it. Then:

1. Noctalia → **Settings → Plugins → Installed** → enable **osync**.
   (Local plugins are discovered by a folder scan and start *disabled*.)
   Enabling automatically adds the pill to your bar.
2. Open its **settings** (gear) if your launcher is somewhere unusual.

Remove with `./install.sh --uninstall`.

## What it shows

**Bar pill** — a status dot on the left, counts to its right. The dot carries
every state change: **green** when healthy, **green and spinning** while a sync
is actually running, **red** when a replica is unreachable, amber for a
stale/errored job. The label deliberately never changes shape, because a pill
that resizes drags every widget next to it along the bar. The label is
configurable:

| Mode | Shows |
|---|---|
| `auto` (default) | Pending `↑push ↓pull`, always — the label never swaps to a word, so the pill keeps a constant width and nothing beside it shifts |
| `health` | Worst health across all connections |
| `counts` | `healthy / total` |
| `changes` | Pending `↑push ↓pull` summed across connections |

**Panel** — one card per connection, and it is a one-to-one port of the TUI's
card (`card_body()` in `osd_tui.py`): same rows, same order, same wording,
same semantic colours. Health badge and last-sync age; the `↑ push / ↓ pull`
legs with the TUI's exact logic (`off` for the idle leg of a one-way sync,
`N files` when hot, `in sync` at zero, braille spinner and `transferring…`
while running); `local … · remote … · resume … · moved ↑x ↓y`; both replicas
with online dot, rsync, Tailscale identity, file count, size and disk bar; then
the `local / remote / via / auto-sync / safety / log` rows.

Nothing here is invented — if it is on the card it is in the TUI.

**Read-only by design.** Nothing in the bar or panel can start a sync — a stray
click must never move files. Syncing stays an explicit action in the TUI, which
the panel's ↗ button opens.

## Cost and cadence

Three independent loops, so liveness never waits on the expensive probe:

| Loop | Every | Cost |
|---|---|---|
| Lock-file stat (is a sync running?) | 300ms | ~2ms (0.7% of a core) |
| Full probe (counts, disk, reachability) | 20s | ~550ms (ssh + tree walk) |
| Relative-age tick | 1s | none |
| Spinner animation | 120ms | none |

osync's lock windows are only ~1.5s wide, so a slow poll misses most syncs
entirely — a 1-minute sync routine would finish between two probes. The fast
loop stats the `lock_file` path the core reports (`osd --json`), which
costs ~2ms, and **a start/stop transition immediately triggers a full probe** so
the counts are right the moment a sync ends rather than up to an interval later.
`osd --status` does the same job for scripts, but pays ~90ms of Python
startup per call.

Relative ages are computed from `last_sync_ts` against the 1s tick, not from the
payload's `*_age` fields — those are snapshots taken when the probe ran, so
rendering them makes the label freeze between probes and then jump.

If even 20s is too much — a laptop on battery, a remote that is often asleep —
turn on **Local only**: it skips the ssh probe entirely, at the cost of pull
counts and remote replica state going unknown. Liveness keeps working, since
the lock file is local.

## Files

| File | Role |
|---|---|
| `Main.qml` | Runs `osd --json` on a timer, parses it, derives every display string. Bar and panel share it via `pluginApi.mainInstance`. |
| `BarWidget.qml` | The pill. Pure presentation. |
| `Panel.qml` | Per-connection cards. |
| `Settings.qml` | Binary path, interval, local-only, bar metric, terminal. |
| `manifest.json` | Entry points + `defaultSettings`. |

## Notes

- Strings are plain English; there is no `i18n/` yet.
- Noctalia's hot reload follows symlinks, so with debug mode on you can edit
  these files in the repo and see changes without restarting the shell.
