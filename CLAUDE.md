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

## Running tests

**Nothing auto-loads `.env`** — not `manage.py`, not `config/settings.py`, not the
`Dockerfile`, and not `direnv` (its hook lives in `~/.zshrc`, which the Bash tool's
non-interactive shell never sources). A bare `python manage.py test inventory` dies
with `ImproperlyConfigured: SECRET_KEY environment variable must be set`. Always:

```bash
set -a; source .env; set +a
python manage.py test inventory
```

That is the whole command in a worktree too — **do not** add a `DB_NAME=` override.
Worktrees get `.env` as a *symlink* to the main checkout's copy (via
`worktree.symlinkDirectories`), so they all resolve to the same `DB_NAME`, which
would let two parallel test runs fight over one `test_<DB_NAME>` database and
destroy each other's data under `--noinput`. `config/settings.py` already handles
this: when `BASE_DIR` is under `.claude/worktrees/`, it sets `TEST.NAME` to
`test_na9k_wt_<worktree>`. Nothing to remember at the command line.

That needs a one-time grant on the dev MariaDB, because `na9000` holds only
`CREATE, DROP` globally plus per-database grants — enough to *create* a test
database but not to run migrations in it:

```sql
GRANT ALL PRIVILEGES ON `test_na9k_%`.* TO 'na9000'@'%'; FLUSH PRIVILEGES;
```

Run it against the **`na9000-mariadb-dev`** container — the one publishing
`0.0.0.0:3306`, which is what `.env` points at. `network-addresser-9000-db-1` is
the compose deployment database and is *not* what the test suite uses. Do not
write the pattern as `` `test\_na9k\_%` ``: inside backticks the backslash is
stored literally and the grant silently never matches.

**Never put environment variables inline in front of a command.** Not
`DJANGO_DEBUG=true python manage.py test …`, not `DB_NAME=… python …`. Two reasons:

- Everything those prefixes used to supply now comes from `.env`, so they are dead
  weight.
- Permission rules match from the *start* of the command string, so each new prefix
  combination is a distinct un-allowlisted command that prompts. Approving them one
  by one is what previously grew `.claude/settings.local.json` to ~150 entries, most
  of them single-use fossils. Plain `python manage.py test …` matches the existing
  `Bash(python *)` rule and never prompts.

Same trap applies to `$(...)`: the permission matcher refuses to match any command
containing command substitution, so a shell-derived value always prompts. Derive
values in Python, not in the command line.

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
