> **Revision 2** — incorporates review notes from `REVIEW-1-PLAN-ci-and-branch-protection.md`.
> See "Review response" for the mapping.

# Roadmap item 6 — Process hardening (CI + branch protection)

## Context

`ROADMAP.md` phase 6 has three items. Pre-commit hooks shipped; **GitHub Actions CI** and
**branch protection on `main`** are still open, and they are the last two unticked boxes
before phase 7 (container publishing).

Two facts found while exploring change the shape of the work:

1. **There is no `.github/` directory at all.** The only workflow this repo ever had —
   `codex-review.yml` — was deleted in `78b4bea` because `/plan-cycle` replaced it. So CI is
   a from-scratch build, but the deleted file establishes the house conventions to follow:
   actions pinned to a full commit SHA with a `# vN` trailing comment, explicit
   `permissions:` blocks, and a `concurrency:` group with `cancel-in-progress`.

2. **Branch protection is not missing — it is stale.** An *active* repository ruleset
   (`id 19629514`, created 2026-07-23) already covers `~DEFAULT_BRANCH` with `deletion`,
   `non_fast_forward`, `update` (blocks direct pushes), a `pull_request` rule, and a
   `required_status_checks` rule demanding a check named **`codex`** — supplied by the
   workflow deleted in `78b4bea`. That check can never report again, and the rule also
   requires 1 approving review, which GitHub will not let a solo maintainer self-supply.
   PRs #45, #46 and #47 merged only via the repository-admin `bypass_mode: always` actor.
   So the ruleset currently describes a gate that is bypassed on literally every merge.

Intended outcome: a CI workflow that actually runs the lint and test suites on every PR,
and a ruleset that gates on *those* checks — so a green merge means something, and the
admin bypass goes back to being an escape hatch rather than the normal path.

**Decisions taken (confirmed with Mike, 2026-08-05):** approvals drop to **0** (PRs still
mandatory); the **admin bypass stays**; the ruleset is updated **by me via `gh api`, after**
the CI workflow is merged and has reported at least once.

## The one real gotcha

**`mysqlclient==2.2.8` publishes no Linux wheels** — PyPI has Windows wheels and an sdist,
nothing else. Every job that installs it (which is *both* jobs, because the mypy pre-commit
hook lists `mysqlclient==2.2.8` in `additional_dependencies`) must build it from source and
therefore needs `pkg-config` + `default-libmysqlclient-dev` + `build-essential` installed
via apt first. Omitting this fails the run at install time with a `pkg-config`/`mysql.h`
error, and it is not obvious from the symptom.

*Confirmed by the stage-1 review against PyPI: 2.2.8 ships `cp310`–`cp314` Windows wheels
and an sdist only, and the named Ubuntu packages are the right set.*

## Work

### 1. `.github/workflows/ci.yml` (new file)

Top level:

- `on: pull_request` (default types) **and** `push: branches: [main]`.
- `permissions: contents: read` at workflow level.
- `concurrency: group: ci-${{ github.ref }}`, `cancel-in-progress: ${{ github.event_name == 'pull_request' }}` — cancel superseded PR runs, never cancel a `main` run.
- `timeout-minutes` on both jobs (20 is generous).

The two jobs must have the **job ids** `lint` and `test`, because the job id is what becomes
the status-check context name the ruleset will require. Do not set a `name:` that differs
from the id without updating §3 to match.

Pinned actions (SHAs resolved, current as of this plan):

| Action | Pin |
| --- | --- |
| `actions/checkout` | `fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5` (same pin the deleted workflow used) |
| `actions/setup-python` | `ece7cb06caefa5fff74198d8649806c4678c61a1 # v6` |
| `actions/cache` | `0057852bfaa89a56745cba8c7296529d2fc39830 # v4` |

Both jobs use `actions/setup-python` with **`python-version-file: .python-version`** (reuse
the existing `3.12.13` pin rather than hardcoding a version in the YAML) and `cache: pip`.

**Job `lint`** — no database, no secrets:

1. apt step (see gotcha above).
2. `pip install -r requirements-dev.txt`.
3. `actions/cache` on `~/.cache/pre-commit`, keyed on `hashFiles('.pre-commit-config.yaml')`.
4. `pre-commit run --all-files --show-diff-on-failure`.

Running `pre-commit` rather than re-invoking ruff/mypy directly is deliberate: the hook
config in `.pre-commit-config.yaml` is already the single source of truth for ruff-check,
ruff-format, mypy and the file-hygiene hooks, and duplicating those invocations in YAML
creates two places to drift. The mypy hook needs no environment setup — `pyproject.toml`
points django-stubs at `config/settings_typecheck.py`, which sets `DJANGO_DEBUG=true`
itself precisely so tooling never needs a real `SECRET_KEY`.

The cache key is `.pre-commit-config.yaml` alone, which is correct here: that file pins both
the hook `rev`s *and* the mypy hook's `additional_dependencies`, so any change to what gets
installed changes the hash.

**Job `test`** — MariaDB service container:

```yaml
services:
  db:
    image: mariadb:11.4.5          # same tag as docker-compose.yml
    env:
      MARIADB_ROOT_PASSWORD: na9000dev
    ports: ["3306:3306"]
    options: >-
      --health-cmd="mariadb-admin ping -h 127.0.0.1 -u root -pna9000dev --silent"
      --health-interval=5s --health-timeout=5s --health-retries=20 --health-start-period=30s
```

Job-level `env:` block (this is the CI equivalent of `set -a; source .env; set +a`):

```yaml
env:
  DJANGO_DEBUG: "true"
  DB_NAME: network_addresser_9000
  DB_USER: root
  DB_PASSWORD: na9000dev
  DB_HOST: 127.0.0.1
  DB_PORT: "3306"
```

`SECRET_KEY` is deliberately absent: with `DJANGO_DEBUG=true`, `config/settings.py` falls
back to its documented dev-only key, so **this workflow needs no repository secrets at
all** — which also means it works unchanged on fork PRs.

`DB_USER: root` avoids reproducing the dev box's grant dance. CLAUDE.md documents a
one-time `GRANT ALL PRIVILEGES ON \`test_na9k_%\`` on the dev MariaDB because `na9000` there
holds only `CREATE, DROP` globally; the CI container is ephemeral and connecting as root
removes that entire failure mode. `BASE_DIR` is not under `.claude/worktrees/` on a runner,
so the worktree `TEST.NAME` branch in `config/settings.py` does not fire and Django uses its
default `test_network_addresser_9000`.

Steps: apt → `pip install -r requirements-dev.txt` →
`python manage.py makemigrations --check --dry-run` → `python manage.py test inventory`.

The `makemigrations --check` gate is a genuine addition, not ceremony: this project has had
heavy model churn across ADRs 0010–0018, and a model edit landing without its migration is
exactly the failure a solo repo won't otherwise catch until deploy. *The stage-1 review ran
it on this branch: "No changes detected" — the repo passes today, so the gate does not land
red. It also does not require a reachable database (it warns and proceeds), but it runs
after the service is healthy anyway.*

> **Note for the implementer:** the CLAUDE.md rule "never put environment variables inline
> in front of a command" is about the Claude Code Bash permission matcher on *this machine*.
> A workflow `env:` block is not an inline prefix and is the correct mechanism here. Keep
> obeying the rule for any local commands you run while building this.

### 2. Docs

- `ROADMAP.md`: tick the two phase-6 boxes; update the "Current phase" line at the top
  (currently `12 — done`, which already ignores that phase 6 was open — state phase 6 as
  done alongside it rather than leaving the header stale).
- `README.md`: the `## Status` section (line 7) says "phase 6 (process hardening) is still
  open" — that sentence needs rewriting. Add a CI status badge under the `# Network
  Addresser 9000` title.

No ADR. Phase 6's pre-commit item shipped without one, and CI wiring decides nothing about
the domain model.

### 3. Ruleset update — *after* the CI PR merges and reports green

**`PUT /repos/misnow1/network-addresser-9000/rulesets/19629514`** — the update endpoint is
`PUT`, not `PATCH`. Supplying `rules` replaces the whole array rather than merging by rule
type, so it must be sent complete; omitting `deletion` / `non_fast_forward` / `update` would
silently drop them.

Changes versus today, and nothing else:

- `required_approving_review_count`: `1 → 0`.
- `require_last_push_approval`: `true → false`. This one is load-bearing: GitHub defines it
  as "the most recent reviewable push must be approved by **someone other than** the person
  who pushed it", so leaving it `true` re-imposes a second-person requirement that a solo
  repo cannot satisfy — exactly the bypass-on-every-merge situation the 0-approval change is
  meant to end.
- The `codex` status check is replaced by `lint` and `test`, **retaining
  `integration_id: 15368`** on both. That id is GitHub Actions itself (`gh api
  apps/github-actions` → `{"id":15368,"slug":"github-actions"}`), not the OpenAI app — the
  old `codex` entry was an Actions job from the deleted `codex-review.yml`. Keeping it means
  only GitHub Actions can satisfy these contexts, rather than any status producer that can
  post a commit status with a matching name.

The bypass actor is sent unchanged; `enforcement`, `conditions` and `target` are not touched.

```json
{
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "update"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 0,
      "dismiss_stale_reviews_on_push": true,
      "required_reviewers": [],
      "require_code_owner_review": false,
      "require_last_push_approval": false,
      "required_review_thread_resolution": true,
      "allowed_merge_methods": ["merge", "squash", "rebase"]
    }},
    {"type": "required_status_checks", "parameters": {
      "strict_required_status_checks_policy": true,
      "do_not_enforce_on_create": true,
      "required_status_checks": [
        {"context": "lint", "integration_id": 15368},
        {"context": "test", "integration_id": 15368}
      ]
    }}
  ]
}
```

I'll capture `gh api .../rulesets/19629514` before and after and show you the diff.

## Verification

Locally, before pushing (CLAUDE.md's env-sourcing form — no inline prefixes, no `DB_NAME`
override):

```bash
set -a; source .env; set +a
python manage.py makemigrations --check --dry-run   # confirmed clean by the stage-1 review
python manage.py test inventory
pre-commit run --all-files
```

On the PR:

1. Both `lint` and `test` appear as checks and go green.
2. Deliberately confirm the DB path really ran — `test` must report the full
   `inventory/tests.py` + `inventory/test_prod_import.py` count. **481** is the exact figure
   (`Found 481 test(s)`, confirmed by the stage-1 review running Django's own discovery), so
   a run reporting fewer has silently skipped something rather than passed.

After merge:

3. `gh api repos/misnow1/network-addresser-9000/commits/main/check-runs` shows `lint` and
   `test` reporting on `main`.
4. Apply the ruleset `PUT`; then **re-read the ruleset and assert on the response**, rather
   than assuming the write did what was asked:
   - `deletion`, `non_fast_forward` and `update` are all still present;
   - `required_status_checks` contains exactly `lint` and `test`, and **no `codex` entry**
     (this also settles the replace-vs-merge question empirically);
   - `required_approving_review_count` is `0` and `require_last_push_approval` is `false`;
   - the bypass actor is still attached.
5. Open a throwaway PR and confirm the merge button is gated on `lint`/`test` and no longer
   asks for an approval — then close it without merging.

There is deliberately **no** "try pushing directly to `main`" step. As a bypass-enabled
admin that push would be *expected* to succeed, so it would demonstrate nothing about the
rule; step 4's assertion that the `update` rule is still present is the real check, and it
does not require pushing to `main` to find out.

## Review response

| Note | Resolution | Section |
|---|---|---|
| 1 (P1) — `require_last_push_approval: true` undermines `required_approving_review_count: 0`; a solo maintainer still can't merge without bypass | **Folded in.** Set to `false`. Verified the semantics in GitHub's ruleset docs — it requires approval by *someone other than* the last pusher, which is unsatisfiable here. Rev 1 said "every other `pull_request` parameter stays exactly as they are"; that sentence was wrong, because this parameter silently re-imposes the very requirement Mike's 0-approval decision removes. Serving that decision, not departing from it | §3 |
| 2 (P1) — the update endpoint is `PUT`, not `PATCH` | **Folded in.** Verified against GitHub's REST docs: `PUT /repos/{owner}/{repo}/rulesets/{ruleset_id}`. Docs do not state replace-vs-merge for `rules`, so rev 2 keeps sending the complete array (correct either way) and adds an explicit post-write assertion that no `codex` entry survives — settling it by observation instead of assumption | §3, Verification 4 |
| 3 (P3) — dropping `integration_id` works but the stated rationale is wrong, and it weakens source validation | **Folded in, and the rationale corrected.** Confirmed `gh api apps/github-actions` → id `15368`: that is GitHub Actions, so the old `codex` entry was an Actions job all along and rev 1's claim that an integration pin caused the staleness was simply false. The workflow's deletion caused it. Both new contexts now retain `integration_id: 15368` | §3 |
| 4 (P3) — verification step 6 (direct push to `main` is rejected) can't be performed with the maintainer's credentials | **Folded in.** The step is removed rather than reworded: with `bypass_mode: always` the push is *expected* to succeed, so the result is uninformative either way. Replaced by asserting the `update`/`deletion`/`non_fast_forward` rules are still present after the write | Verification |

Findings the reviewer confirmed sound and which therefore stand unchanged: the merge-CI-first
sequencing (no lockout, since the bypass exists), job ids becoming the check contexts, the
`settings.py` secret/DB fallbacks, the `settings_typecheck.py` shim, the worktree `TEST.NAME`
branch not firing on a runner, the mysqlclient wheel situation and apt package set, the
`.python-version` / setup-python pairing, the MariaDB `options:` health flags, `.github/` not
being gitignored, and the pre-commit cache key covering `additional_dependencies`.

## Out of scope

Phase 7's GHCR publishing workflow, and any Docker-image build step in CI — that build
belongs with the publishing job, not here.
