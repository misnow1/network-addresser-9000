---
name: plan-cycle
description: Drive an approved plan through the full chain — independent codex review of the plan, fold the notes in, implement on a Sonnet subagent, independent codex review of the code, fold those notes in — with no interaction between stages. Use immediately after plan approval / ExitPlanMode, when Mike says to go build a PLAN-*.md, or when invoked as /plan-cycle.
---

# Plan cycle

Runs this project's plan-review ritual end to end without stopping between stages:

```
approved plan
  → 1. codex reviews the plan
  → 2. you fold the notes in, commit rev N            [Opus]
  → 3. Sonnet subagent implements, runs the suite     [Sonnet]
  → 4. codex reviews the code
  → 5. same subagent fixes or rebuts each finding     [Sonnet]
  → 6. report to Mike, stop
```

## Before you start

**You must be Opus-class.** Stages 2 and 6 are planning and judgement work. If you are
not, say so and tell Mike `/model opus` before running this — do not fold review notes
into a plan on a weaker model. The implementer is a Sonnet *subagent*, spawned with
`Agent(model: "sonnet")`; Mike never switches models himself, and this session stays on
Opus throughout.

**Do not interact between stages.** The whole point is that Mike approves a plan and
reads one report. The only permitted stops are the escalation gate in stage 2 and the
hard failures called out in each stage. Never ask "shall I continue?".

## Stage 0 — Set up

1. **Resolve the plan.** It must be `docs/plans/PLAN-<topic>.md` — that is the
   convention (`docs/plans/PLAN-adr-0019.md`, `docs/plans/PLAN-prod-import.md`). Plans
   live alongside `docs/adr/` rather than in the repo root, and they *are* committed:
   the plan is the durable record of what was decided and why, which is why only the
   `REVIEW-*.md` notes are gitignored. If the approved plan only lives in
   `~/.claude/plans/`, copy it to `docs/plans/` under a descriptive `PLAN-<topic>.md`
   name first. `<topic>` is reused for the review filenames.
2. **Check the branch.** `git branch --show-current` must return something that is not
   `main`. An **empty** result means detached HEAD — treat it exactly like `main`, or the
   chain's commits end up orphaned. In either case create a feature branch named for the
   topic first. This chain never commits to `main`, never pushes, and never force-pushes.
3. **Scratch dir.** `TMP=$(mktemp -d)` for diffs and prompt files. Review notes go to the
   repo root as `REVIEW-<n>-PLAN-<topic>.md` — gitignored, and deleted in stage 6.
4. **Preflight codex.** `codex --version`. If it fails, **abort the whole chain** and tell
   Mike. Never silently skip a review and build anyway — the independent review is the
   point, not a nicety.

### Extract the untouchables

Before writing any review prompt, read the plan and pull out every decision that is
already settled. Look for sections named:

- `Departure from ADR …` / `Deliberate departure`
- `Consequences accepted, not solved`
- `Decisions` — especially lines reading `resolved with Mike, <date>`
- `Carried as assumptions`

These go into **both** review prompts as stated context that is out of scope to
relitigate. This matters: `PLAN-adr-0015-0016.md` had a section titled *"Departure from
ADR 0016: there must be a `clean()`-time pre-flight"*, and ADR 0016 was implemented with
two deliberate departures from its own text. A reviewer not told will flag them as
defects, and a naive fold-in would silently revert a decision Mike made on purpose.

(That plan was deleted in 93c7d6b rather than kept — the reason plans now live in
`docs/plans/` and stay there. The departures survive only because they were amended into
ADR 0016 itself.)

## Stage 1 — Independent plan review

Write the prompt to `$TMP/plan-review.md`, then:

```bash
codex exec --sandbox read-only \
  -c model_reasoning_effort="high" \
  -o "REVIEW-1-PLAN-<topic>.md" \
  - < "$TMP/plan-review.md"
```

`-o` captures just the final message, so the file is the review and nothing else.
Progress chatter goes to stdout/stderr and can be ignored.

The prompt must:

- Give the plan's path and tell codex to read it, every ADR it cites, `CONTEXT.md`,
  `DESIGN.md`, and the source files the plan names. Codex has read-only shell access to
  the repo — let it look rather than pasting context in.
- Include the untouchables block from stage 0, labelled as settled and out of scope.
- Ask for **numbered** findings, each with a severity and a `file:line` or plan-section
  citation.
- Ask the three questions that actually matter here: *does the sequencing hold; are the
  named files, lines and symbols real; does the verification section actually prove the
  change works?* The repo has been burned by the third one before — ADR 0015's Follow-up
  records that a five-failing-test prediction made by reading the diff was simply wrong.

## Stage 2 — Fold the notes in (you, Opus)

**Verify before accepting.** Open the cited code for every finding. Reviewers are wrong
sometimes, and a previous cycle recorded a review finding that *corrected a wrong
conclusion the grilling pass had reached* — the verification is what makes the fold-in
trustworthy in both directions.

If any ambiguity remains, **stop the chain** and escalate to Mike. Do not fold in a
finding that is not understood, and do not argue with the reviewer — the point of this
stage is to fold in the reviewer's knowledge, not to defend the plan.

Then rewrite `docs/plans/PLAN-<topic>.md` in the shape the repo already uses:

- A revision blockquote at the very top:

  ```
  > **Revision 2** — incorporates review notes from `REVIEW-1-PLAN-<topic>.md`.
  > See "Review response" for the mapping.
  ```

  Bump the number if the plan is already at a revision.

- A `## Review response` section with one row per numbered finding:

  ```
  | Note | Resolution | Section |
  |---|---|---|
  ```

  Every finding gets a row, **including rejected ones, with the argument for rejecting
  them**. The rule is fold findings in rather than defend the original; rejection is
  allowed, but it has to be reasoned, not asserted.

Commit the revised plan on the branch: `Plan <topic> (rev N)`. This matches how #35 was
built — the plan commit immediately precedes the implementation commit in the same PR.

### The escalation gate

Stop and ask Mike **only** when a finding:

- contradicts a committed ADR, or would require writing or amending one;
- changes scope — adds or removes a deliverable, or moves work across a PR split the
  plan defines;
- attacks one of the untouchables from stage 0.

Everything else is folded in without asking and summarised in stage 6: missing tests,
wrong `file:line`, unhandled edge cases *within* the ADR's stated decision, sequencing
detail, verification gaps, naming.

When the gate fires, stop the chain. Do not build against a plan whose design is in
question, and do not resolve it by quietly siding with the reviewer.

## Stage 3 — Implement

```
Agent(subagent_type: "general-purpose", model: "sonnet", run_in_background: false, …)
```

`run_in_background: false` is what makes this a chain — you block on the build and go
straight to stage 4 when it returns. Keep the agent's id; stage 5 needs it.

The prompt must carry the operational knowledge that is otherwise tribal:

- **Export `.env` before running anything Django:** `set -a; source .env; set +a`.
  Nothing auto-loads it, and a bare `source .env` is not enough — `.env.example` uses
  plain `KEY=value` lines, so sourcing creates shell variables that the child `python`
  process never sees, and Django then dies on the missing `SECRET_KEY`. `.env` must
  hold the *local-dev* shape from `.env.example` (`DJANGO_DEBUG=true`,
  `DB_PASSWORD=na9000dev`, `DB_HOST=127.0.0.1`), not the deployment shape from
  `.env.compose.example`. With the deployment shape every test errors on `Access denied
  for user 'na9000'`, and 9 admin tests additionally error on `Missing staticfiles
  manifest entry` because `DJANGO_DEBUG=false` selects `ManifestStaticFilesStorage`.
- **Baseline first.** Run `python manage.py test` and record the count *before* touching
  code, so "tests fail" can be told apart from "tests already failed".
- Implement the plan, following its own Definition of Done — including ticking the
  `ROADMAP.md` checkboxes it names, and correcting an ADR's `## Follow-up` if
  implementation contradicted a prediction it made. Correct it visibly rather than
  silently: ADR 0015 keeps its wrong prediction on the page on purpose, *"so the gap
  between 'reading a diff' and 'running the suite' stays visible"*.
- `pre-commit run --all-files` (ruff check/format, mypy over `config inventory
  manage.py`) must pass before committing.
- **Do not revert a deliberate departure named in the plan.** Pass the untouchables
  block through.
- Commit style: a long body explaining *why*, what was measured versus predicted, and
  which issues it opens or closes. Trailer
  `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- Report back: baseline and final test counts, files touched, and anything in the plan it
  could not do, with the reason.

If it reports test failures it could not resolve, **stop the chain** and escalate with
its report. Do not send a broken build to code review.

## Stage 4 — Independent code review

`codex review`'s `--base` / `--uncommitted` / `--commit` flags are mutually exclusive
with a prompt, so intent has to be carried in prompt mode with the diff inlined:

```bash
git diff main...HEAD > "$TMP/changes.diff"
{
  cat "$TMP/intent.md"
  printf '\n```diff\n'; cat "$TMP/changes.diff"; printf '\n```\n'
} | codex review --title "<commit subject>" \
      -c model_reasoning_effort="high" - \
  > "REVIEW-2-PLAN-<topic>.md"
```

`$TMP/intent.md` states what the plan set out to do, which ADRs it implements, the
untouchables block again, and what is deliberately out of scope. Ask for `[P0]`–`[P3]`
findings with `file:line` citations.

If the diff is too large to inline, fall back to flag mode:

```bash
codex review --base main -c model_reasoning_effort="high" > "REVIEW-2-PLAN-<topic>.md"
```

and say in the final report that the review ran without the intent briefing — its
findings against deliberate departures are then expected noise, not signal.

## Stage 5 — Fold the code review in

`SendMessage` to the stage-3 subagent. Its build context is intact, which beats a cold
agent re-deriving the change. Spawn a fresh `Agent(model: "sonnet")` only if that session
is gone.

Every finding ends in one of exactly two recorded states:

- **Fixed** — with a test, where the finding is behavioural.
- **Rebutted** — with a written reason. Required, not optional, when a finding
  contradicts the plan or one of the untouchables. The agent argues; it does not comply
  by default.

Fixes land as one commit — `Fix Codex review findings: <short list>` — matching the
existing `Fix Codex review findings on SwitchPortVlanProfile` and `Fix review-council
findings: …` commits. Re-run the suite and pre-commit before committing.

**One review round by default.** Run a second only if P0/P1 findings were fixed, and
never a third. That cap is what terminates the loop.

## Stage 6 — Report and stop

`rm -f REVIEW-*-PLAN-<topic>.md`. The durable record is the plan's `## Review response`
table and the commit history, which is how this repo already works.

Report to Mike:

- what each review found, what was folded in, and what was rebutted — with the reasons;
- test counts, baseline → final;
- the commits produced, in order;
- anything escalated, skipped, or left undone.

Then stop. Mike runs `gh pr create` and merges.

**There is no CI review behind this chain.** `.github/workflows/codex-review.yml` was
removed when this skill landed: it had been disabled since 2026-07-25 because reviews
were already being run locally, and stage 4 now does that job automatically on every
run. So the stage-4 review is the only independent look the code gets — never skip it
on the assumption that CI will catch things, and never report a green PR as a second
opinion. If CI review is ever wanted back, it was added in `1ad76aa` and can be
restored from there.

## Notes

- **Never call `/codex:review` from this chain.** Its command file sets
  `disable-model-invocation: true`, so a model cannot invoke it. Shell out to the `codex`
  CLI directly, as above — that also avoids the plugin's version-pinned path, which
  changes on every plugin upgrade.
- **`review-council` is deliberately not used.** Two of its seven streams (`agy`,
  `gemini`) are not installed here, and it stops for user approval by design, which is
  the opposite of what this chain is for. Use it by hand when a change warrants the
  heavier pass.
- Model selection for codex is left to its own config default. To pin one, add
  `-c model="<name>"` to both invocations.
