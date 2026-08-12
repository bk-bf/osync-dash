# osd

A Textual TUI over [osync](https://github.com/deajan/osync) two-way sync jobs.
One compose file defines many connections; each gets a card showing merged
health, both machines, live push/pull activity and the soft-delete safety net.

- `osd_core.py` — sync logic, compose/settings parsing, the guards
- `osd_tui.py` — the interface
- `osd` — the entry point the systemd units call

## The deletion guard — read this before touching it

`guarded_sync()` in `osd_core.py` dry-runs first, then refuses if the sync would
propagate more deletions than `delete_guard` (default 25, in
`~/.config/osync/osd.toml`). It exits **3** when it blocks. The systemd units
call this, so the guard protects automatic syncs, not just manual ones.

When a unit fails with exit 3, **raising the guard is not the fix, and neither
is bypassing it.** Both destroy the thing the guard exists for. Run
`osd --deletions NAME` instead: it splits the pending deletions into the two
kinds that matter and shows the paths that would really go.

**There are three cases, not two.** A legitimate cleanup, a broken replica, and:

> **A move.** Nothing was deleted — a directory was renamed and the files are
> under a different path. osync counts them as deletions because it compares
> paths, not content.

This bites `claude-sessions-laptop` hardest because Claude Code keys session
storage by **absolute project path**: `~/.claude/projects/<slug-of-abs-path>/`.
Move any project directory and its whole history re-slugs and the old path
appears to empty out — on a replica where that history is the only copy.

`analyse_deletions()` is what keeps a move from spending the guard's budget. It
indexes both trees and pairs each departing path against surviving content by
`(basename, size)`; what pairs is a relocation and costs nothing, what does not
is a real deletion, and only that count faces `delete_guard`. Three properties
hold it together, and a change that breaks any of them reopens the hole:

- **Each surviving copy vouches for exactly one departure.** Without the cap,
  one leftover empty file would excuse deleting a hundred of its namesakes.
- **osync's own count stays authoritative.** Moves are subtracted from it, per
  side and capped by that side's reported deletions, so anything osd cannot
  positively pair still counts against the guard.
- **An unreadable tree proves nothing.** If either index is missing, no move is
  credited and the guard is as strict as it ever was.

A file that was moved *and* edited has a new size, so it counts as a real
deletion. That is the intended direction to be wrong in.

Worked instance, 2026-08-09 → resolved 2026-08-12:
`~/Documents/Projects/Fantasia4x` moved to `~/Documents/local/Projects/Fantasia4x`;
222 reported deletions against a guard of 25, the unit failing every minute for
three days. 204 were the move (180 files + 24 emptied directories), 18 were
real, and the sync went through untouched once the two were told apart.

## Rules

- Exit codes are an interface: `0` ok or idle, `3` blocked by the guard,
  anything else is osync's own code passed through. Units and monitors read
  these; do not repurpose them.
- Never make a sync destructive to make a test pass. The safety net is the
  product.
- **Soft delete only covers `direction = "sync"`.** osync routes one-way jobs
  (`send`/`receive` → `SYNC_TYPE=initiator2target`) through rsync `--delete`
  instead of its deletion-list machinery, so `SOFT_DELETE=true` never fires and
  the files are gone for good. Measured 2026-08-12: `memory-universal` (bidir)
  holds 138 files in `.osync_workdir/deleted` with live state files, while both
  `send` connections have empty delete dirs and deletion lists untouched since
  22 Jul. `delete_guard` is the *only* thing standing in front of a one-way
  replica — which is why it is worth making it precise rather than loose.
- The dry-run before every sync also exists so a periodic timer does not spin up
  a full osync pass for nothing. Keep that short-circuit.

## Conventions

Comments explain why, not what. Match the density already in `osd_core.py`.
