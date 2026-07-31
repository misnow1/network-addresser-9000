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

The planning phase should use the /grilling skill to thoroughly question the plan
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
- **The Sonnet half applies only to hand implementation.** When `/plan-cycle`
  drives the build (below), it spawns the implementer as a Sonnet subagent and
  the orchestrating session should *stay* on Opus — folding review notes into a
  plan is planning work. Don't fire the reminder in that case.

## Plan-cycle automation

`/plan-cycle` (`.claude/skills/plan-cycle/`) runs the whole ritual hands-off after
plan approval: an independent `codex` review of the plan → fold the notes into
`PLAN-<topic>.md` as a revision with a `## Review response` table → implement on a
Sonnet subagent → an independent `codex` review of the code → the same subagent
fixes or argues back against each finding → commits on the branch, then stops.
Mike opens the PR.

It comes back to Mike mid-chain in two situations. It **escalates** when a review
finding contradicts a committed ADR (or needs a new one), changes scope, or attacks
a decision Mike made deliberately — everything else is folded in without asking and
summarised in the final report. It **aborts** on operational failure: codex missing,
or an implementer that can't get the suite green.

That ritual — plan reviewed independently before *and* after implementation,
findings folded in rather than defended — is this project's convention whether or
not the skill is driving it.
