# osync-dash

A dependency-free terminal dashboard for [osync](https://github.com/deajan/osync)
two-way sync jobs. One command shows the whole picture: health, both replicas
(local + remote over SSH), paths, the soft-delete/backup safety net, and a live
pending-changes dry-run.

```
╭─ osync · documents_remote ───────────────────────────────╮
│ ● HEALTHY    last sync 2m   mode bidirectional           │
╰──────────────────────────────────────────────────────────╯
╭─ devices ────────────────────────────────────────────────╮
│   device            up  rsync  files  size   free         │
│ ▸ initiator (local) ●   ✓      350    235M   300G         │
│ ▸ target host       ●   ✓      350    235M   534G         │
╰──────────────────────────────────────────────────────────╯
  + sync state · paths · safety net · pending (dry-run)
```

## Why

osync scatters its status across state files in `.osync_workdir/state/`, a log
file, and whatever the last run printed. `osync-dash` reads all of it and the
config, probes both ends, and renders a single glanceable view.

## Requirements

- Python 3 (standard library only — no `pip install`)
- `osync` (`osync.sh` on `PATH` or at `/usr/local/bin/osync.sh`)
- `rsync` + `ssh` on both ends (already required by osync)
- Optional: `fzf` (config picker when you have multiple jobs)

## Install

```sh
git clone https://github.com/bk-bf/osync-dash.git
ln -s "$PWD/osync-dash/osync-dash" ~/.local/bin/osync-dash   # anywhere on PATH
```

## Usage

Just run it — the default view includes everything, pending dry-run included:

```sh
osync-dash                 # full dashboard for the sole ~/.config/osync/*.conf
osync-dash -c path.conf    # a specific job
osync-dash --fast          # skip the pending dry-run (status only, no ssh rsync)
osync-dash --watch         # live refresh (pending skipped each cycle)
osync-dash --sync          # run the sync, then show the dashboard
osync-dash --log           # page the osync log
osync-dash --local-only    # offline: skip the remote probe
```

Configs are auto-discovered in `~/.config/osync/*.conf`; with more than one it
fzf-picks (or falls back to the first). See `osync-dash --help` for all flags.

## How it reads status

| Field        | Source |
|--------------|--------|
| health       | `*-last-action-<instance>` + `resume-count-<instance>` state files, plus live reachability |
| last run     | mtime of the initiator `last-action` state file |
| files/size   | live walk locally; one combined `ssh` probe remotely |
| free space   | `statvfs` locally, `df` remotely |
| safety net   | file counts under `.osync_workdir/{deleted,backup}` on both sides |
| pending      | `osync … --dry --summary`, parsed for update/deletion counts |

## License

MIT — see [LICENSE](LICENSE).
