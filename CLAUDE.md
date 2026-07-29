# CLAUDE.md

Project docs: `CONTEXT.md` (domain language + roles), `DESIGN.md` (data model),
`ROADMAP.md` (phases), `docs/adr/` (decision records).

## Model workflow

This project's convention: **plan on Opus (or better), implement on Sonnet.**
Mike wants to be reminded at both transitions — he will not always remember, so
the reminder is your job, not his.

**When planning work starts** — entering plan mode, being asked to design /
architect / scope something, writing or revising a `PLAN-*.md`, or kicking off a
review-council pass — check which model you are (your system prompt states it):

- **Not Opus-class?** Say so before you start planning, and tell Mike:
  _"Switch to Opus for planning: `/model opus`"_. Don't quietly produce a plan on
  a weaker model.
- **Already Opus-class?** Say nothing. Just plan.

The planning phase should use the /grill-me skill to thoroughly question the plan
and make sure all ambiguous items are resolved.

**When planning ends and implementation begins** — right after plan approval /
`ExitPlanMode`, or when Mike says to go build it:

- **Still on Opus?** Remind him before you write code: _"Planning's done —
  switch back to Sonnet for implementation: `/model sonnet`"_.
- **Already on Sonnet?** Say nothing. Just build.

Guard rails so this stays useful instead of becoming noise:

- Remind **once per transition**. Never mid-plan or mid-implementation.
- It's advice, not a gate. If Mike says keep going, keep going — and drop it.
- Re-planning mid-implementation (scope change, plan revision, review-council)
  is a fresh planning transition. Remind again.
- `/model opus` and `/model sonnet` set the model directly; bare `/model` opens
  the picker. `/fast` is an Opus **speed** toggle, not a downgrade — it does not
  satisfy "switch to Sonnet".
