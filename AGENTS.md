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

When a unit fails with exit 3, **raising the guard is almost never the fix.**
The guard is doing its job; the question is why the deletions exist. A
legitimate cleanup and a broken replica look identical from the guard's side,
which is the entire reason it refuses instead of deciding. Establish which one
it is — on both ends — before changing any threshold.

For `claude-sessions-laptop` specifically, the source is `~/.claude/projects`,
where session files are pruned by retention. A rising deletion count there is
usually retention working, but "usually" is not a diagnosis, and the count has
climbed 63 → 73 → 140, which is a trend rather than a one-off cleanup.

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
