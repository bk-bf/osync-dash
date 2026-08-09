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
is bypassing it.** Both destroy the thing the guard exists for, and they are
the only two escapes it currently offers — which is itself the defect.

**There are three cases, not two.** The code comment in `guarded_sync()` names
only a legitimate cleanup and a broken replica. The third is the one that keeps
happening and the one agents keep missing:

> **A move.** Nothing was deleted — a directory was renamed and the files are
> under a different path. osync counts them as deletions because it compares
> paths, not content.

This bites `claude-sessions-laptop` hardest because Claude Code keys session
storage by **absolute project path**: `~/.claude/projects/<slug-of-abs-path>/`.
Move any project directory and its whole history re-slugs, the old path appears
to empty out, and the guard blocks propagating that to the replica — where the
history is the only remaining copy.

Confirmed instance, 2026-08-09: `~/Documents/Projects/Fantasia4x` moved to
`~/Documents/local/Projects/Fantasia4x`; old-path session dir 33 files,
new-path 99, 140+ reported deletions, the unit failing every minute since.

**Before concluding anything about an exit 3, check whether the missing paths
exist elsewhere in the tree.** If they do, it is a move, and "the guard is
working as designed" is a description of the bug rather than a diagnosis of it.
The real defect is that osd has no concept of a move, so anything hooked to it
can never be relocated.

This is deliberately unfixed — it is the live test case for the `mon`
monitoring system. Diagnose it correctly; do not repair it unless asked.

## Rules

- Exit codes are an interface: `0` ok or idle, `3` blocked by the guard,
  anything else is osync's own code passed through. Units and monitors read
  these; do not repurpose them.
- Never make a sync destructive to make a test pass. The safety net is the
  product.
- The dry-run before every sync also exists so a periodic timer does not spin up
  a full osync pass for nothing. Keep that short-circuit.

## Conventions

Comments explain why, not what. Match the density already in `osd_core.py`.
