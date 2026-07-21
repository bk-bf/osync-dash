# osync-dash

An interactive terminal dashboard for [osync](https://github.com/deajan/osync)
two-way sync jobs, built with [Textual](https://github.com/Textualize/textual).
One view shows the whole picture: health, both replicas (local + remote over
SSH, with hostnames and Tailscale identity), paths, the soft-delete/backup
safety net, and a live pending-changes dry-run.

```
╭─ health ───────────────────────────────────────────────────────────────────╮
│ ● HEALTHY      last sync 3m      mode bidirectional                         │
╰────────────────────────────────────────────────────────────────────────────╯
╭─ devices ──────────────────────────────────────────────────────────────────╮
│ device      host                              up   rsync   files  size  free│
│ initiator   my-laptop                         ●     ✓       350   235M  300G│
│             ↳ my-laptop  ·  100.x.y.z                                       │
│ target      my-server                         ●     ✓       350   235M  534G│
│             ↳ my-server  ·  100.a.b.c                                       │
╰────────────────────────────────────────────────────────────────────────────╯
  + sync state · paths · safety net · pending — all live, keyboard-driven
```

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

- Python 3.8+
- `osync` (`osync.sh` on `PATH` or `/usr/local/bin/osync.sh`)
- `rsync` + `ssh` on both ends (already required by osync)
- The TUI needs `textual` — `install.sh` puts it in a local `.venv`
- Optional: `fzf` (config picker), `tailscale` (device names in the table)

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
osync-dash              # interactive Textual TUI (auto-discovers ~/.config/osync/*.conf)
osync-dash -c job.conf  # a specific job
```

In the TUI:

| key | action |
|-----|--------|
| `r` | refresh now |
| `c` | run the pending-changes dry-run |
| `s` | run the sync (suspends to stream osync, then returns) |
| `l` | page the osync log |
| `q` | quit |

Status refreshes on a background thread, so ssh probes never freeze the UI.
Resize and mouse work; it's fine over SSH.

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
