> **Revision 3** — amends decision 5 so `latest` is prerelease-aware, enabling
> release-candidate tags. See "Revision 3: release candidates".
>
> **Revision 2** — incorporates review notes from `REVIEW-1-PLAN-container-publishing.md`.
> See "Review response" for the mapping.

# Phase 7 — Container publishing

## Context

`ROADMAP.md` phase 7 has stood unchecked since the roadmap was written — "GitHub Actions
workflow: build and publish the Docker image to GHCR on `main` merges" — with a note recording
that it "was scoped, then skipped" and was deliberately left out of order rather than
renumbered, because "hiding that would only make it harder to notice."

Two things make now the moment to close it. `v0.1.0` was tagged on 2026-08-21 (main @
`11ca1b9`) as the first alpha, cut for read-only tester access; its release notes state
plainly that container publishing "isn't done — deploy from source". And **nothing in CI
builds the `Dockerfile` at all** today, so a change that breaks the image is invisible until
a deploy is attempted.

There is also a latent exposure that publishing forces into the open. `.gitignore` excludes
`prod/` — the four MPS Audio Network Standards CSVs (IP addressing, Dante devices, switch
ports) are real production network data and are **not** in the public repo. But
`.dockerignore` does not exclude `prod/`, and the `Dockerfile` ends with `COPY . .`. So a
**local** `docker compose up --build` bakes that production data into the image today. The
same gap covers secrets: `.dockerignore` lists `.env` but not `.env.deployment` or
`docker-compose.env`, both gitignored precisely because they hold secrets.

Moving the build into CI *improves* this — Actions checks out only tracked files, so `prod/`
and the `.env*` secrets simply do not exist in a CI-built image. But the local foot-gun
survives unless `.dockerignore` is tightened, and "someone builds locally and pushes" is
exactly the accident a public registry makes expensive. That tightening rides along with this
phase.

## Decisions settled during grilling

Resolved with Mike, 2026-08-21. Out of scope to relitigate.

1. **Validate everywhere, push on tags.** The image builds on pull requests and `main` merges
   to prove it still builds, but is pushed to GHCR only on a `v*` tag. This deliberately
   departs from phase 7's recorded scope ("on `main` merges"), which would publish moving
   images nobody pulls; `ROADMAP.md` is rewritten to match rather than left contradicting the
   build.
2. **Multi-arch on native runners.** `linux/amd64` on `ubuntu-latest` and `linux/arm64` on an
   ARM runner, each building natively and then joined into one manifest. Not QEMU: the
   builder stage compiles `mysqlclient` from source, and running that C build under emulation
   is the slowest option available. Mike's dev machine is arm64 and the on-prem target's
   architecture is recorded nowhere, so publishing both removes the guess.
3. **Public package.** GHCR packages default to private even for a public repo, so this is a
   deliberate one-time flip. The source is already public and — once decision 6 lands — the
   image carries no production data and no secrets, so the marginal exposure is near zero,
   while the on-prem host gains a credential-free `docker pull`.
4. **Compose consumes via an override, and ADR 0009 stands.** `docker-compose.yml` keeps
   `build: context: .` as its documented default; a new `docker-compose.release.yml` overrides
   the `app` service to a pinned image. This adds *where the image comes from* without
   changing the deployment shape ADR 0009 decided, so ADR 0009 gets a postscript rather than
   an amendment to its decision, and no new ADR is created.
5. **Tags are exact + `latest` + `sha`.** A `v0.2.0` push produces `0.2.0`, `latest` and
   `sha-<short>`. No floating `0.2`, and emphatically no floating `0`: under semver a `0.x`
   line permits breaking changes in a *minor* bump, so a `0` tag would actively mislead. The
   release override pins the exact version, so the floating tags are human convenience, not
   load-bearing. **Amended by revision 3** — `latest` moves only for a non-prerelease tag.
6. **`.dockerignore` is tightened in this phase** — `prod/`, `.env.deployment`,
   `docker-compose.env`.
7. **`workflow_dispatch` with a tag input**, so the publish path can be exercised on demand
   without cutting a version. `v0.1.0` is **not** retro-published: its release notes say
   "deploy from source", and that stays true.
8. **Two workflow files.** Build-validation is a job in the existing `ci.yml`, reusing its
   `changes` job so documentation-only pull requests skip it; publishing lives in a new
   `publish.yml`. Duplicating the docs-skip logic — which `ci.yml` reasons about carefully,
   because `main`'s ruleset requires `lint` and `test` *by name* — would invite the two copies
   to drift.
9. **The `docker` job is not added to the branch ruleset.** `main`'s ruleset currently requires
   exactly `lint` and `test`; making the image build block merges is a separate decision, and
   the ruleset is fiddly enough (PUT-not-PATCH, `rules` replaces wholesale) to deserve its own
   change.

## Review response

| Note | Resolution | Section |
|---|---|---|
| 1 (P1) — release override does not remove `build` | **Accepted, verified by running it.** `docker compose -f docker-compose.yml -f <override> config` was confirmed to emit **both** `build:` (with `context:` and `dockerfile:`) and `image:` for the `app` service, so Compose could still build locally. Also confirmed the fix: `build: !reset null` — the tag on the *value*, not the key — removes `build` entirely, leaving only `image`. (`!reset build: null` does **not** work; it silently dropped only `dockerfile:`.) Minimum Compose version documented. | `docker-compose.release.yml` |
| 2 (P1) — no registry authentication | **Accepted.** `packages: write` authorises the token but does not log Docker in. `docker/login-action` added to both matrix build jobs and the merge job — every job that touches the registry. | `publish.yml` |
| 3 (P1) — job permissions replace, not widen | **Accepted; the plan's wording was wrong.** Job-level `permissions` replaces the top-level block wholesale, so declaring only `packages: write` sets `contents` to `none` and breaks checkout. Registry jobs now declare **both** `contents: read` and `packages: write` explicitly. | `publish.yml` |
| 4 (P1) — dispatch can overwrite release tags | **Accepted.** The free-text input accepted `latest` or `0.2.0`. Dispatch tags are now constrained to a `test-` namespace and validated before any push, and `type=sha` is gated to tag events so a dispatch cannot emit an unclean-up-able `sha-*` tag. | `publish.yml` |
| 5 (P1) — malformed `v*` tag can still move `latest` | **Accepted.** `type=semver` yields no version tag for `vfoo`, but the `startsWith(refs/tags/v)` gate would still publish `latest` and `sha-*`. A validation step now rejects any tag that is not valid semver before any digest is pushed. | `publish.yml` |
| 6 (P1) — verification can pass while the feature is unusable | **Accepted, and it is the most important finding.** The original Verification leant on `docker compose config`, which happily resolves a tag that does not exist, and never ran the image. Verification now requires an anonymous pull of the literal published tag and running that image through Compose against MariaDB with an HTTP check. This is precisely the failure mode the plan's own Context warns about. | Verification |
| 7 (P2) — `type=gha` cache setup incomplete | **Accepted.** `docker/setup-buildx-action` is now named and SHA-pinned (the GHA cache backend does not work on the default driver), cache scope is per-platform so the two matrix jobs stop overwriting each other, and `mode=max` retains the builder stage's compiled wheels — the expensive part. | `publish.yml` |
| 8 (P2) — the smoke test cannot run before merge | **Accepted; a real sequencing error.** `workflow_dispatch` is only offered for workflows present on the default branch, so the plan required a pre-merge step that is impossible. The Definition of Done is now split into pre-merge and post-merge gates, with the smoke test and the visibility flip in the latter, plus explicit rollback criteria. | Definition of done |
| 9 (P2) — supply-chain verification missing | **Split.** Accepted: a guard refusing to overwrite an existing version tag, recording the manifest digest in the run log and the release, and build provenance attestation (~5 lines, and the documented default for public images). **Rejected: vulnerability scanning.** It is a new, ongoing deliverable whose failure mode is release builds going red on third-party CVEs in a base image this phase does not otherwise touch, and it belongs with a decision about *policy* (what severity blocks a release) that nobody has taken. Noted in ROADMAP as a follow-up instead of silently absorbed here. | `publish.yml`, ROADMAP |

## Revision 3: release candidates

Raised by Mike after the review chain completed, while the branch was still unpushed: should
image builds be enabled outside `main`, so release-candidate branches can be tagged and
workflows like this one rehearsed?

**Two thirds of that already work, and needed nothing.** `ci.yml`'s `docker` job already builds
on every code PR and every push to `main`, so branch builds exist — they simply do not push.
And **git tags are not branch-scoped**: `push: tags: ['v*']` fires for a tag on any commit,
whatever branch it sits on, so tagging a release-candidate branch already publishes. The
SemVer grammar adopted in revision 2 accepts prerelease identifiers — verified against
`v0.2.0-rc.1`, `v0.2.0-alpha`, `v0.2.0-rc.1.2` and `v1.0.0-beta.3`, all accepted, with `vfoo`
still rejected.

**The third part was a real defect.** `type=raw,value=latest` was gated on
`github.event_name == 'push'` alone, with no notion of a prerelease. So a `v0.2.0-rc.1` tag
would republish `latest` pointing at an unreleased candidate, built from a commit that need
not be on `main` — and an ordinary `docker pull` with no tag would serve it.

This is decision 5's caveat coming due rather than a new discovery. The original grilling
noted that `latest` would track prereleases "while every release is alpha" and accepted it.
That reasoning held precisely *because* prereleases were the releases; it stops holding the
moment `v0.2.0-rc.1` and `v0.2.0` can both exist, which is exactly what release-candidate
tagging introduces.

**Decision: make `latest` prerelease-aware; do not broaden the triggers.** `latest` moves only
for a tag with no prerelease identifier. Everything else about decision 5 stands.

- `v0.2.0-rc.1` → publishes `0.2.0-rc.1` and `sha-<short>`; `latest` untouched.
- `v0.2.0` → publishes `0.2.0` and `sha-<short>`; `latest` moves.

Because revision 2 rejects build metadata outright, a `-` after the patch component is an
unambiguous prerelease marker — the grammar admits `-` nowhere else — so the test is provable
from the regex rather than heuristic.

**Branch-triggered publishing is declined**, and recorded here so it is not re-proposed.
Auto-publishing from `rc/*` branches reintroduces exactly what decision 1 rejected — moving
images nobody pulls — while an RC tag gives the same capability as a deliberate act rather than
a side effect of merging. It is also how the release-shaped path gets rehearsed at all: the
`test-` dispatch namespace cannot exercise `type=semver`, `latest` or `sha-*`, so an RC tag is
the only way to test that path short of cutting a real release. That argument only works if RC
tags are safe, which is what this revision makes them.

## The build

One PR. The pieces are interdependent — the docs describe the workflow, the override pins the
image the workflow produces — so a docs-first split would document something that does not yet
exist.

### `.github/workflows/publish.yml` — new

Triggers: `push` on `tags: ['v*']`, plus `workflow_dispatch` with a required `image_tag`
input.

Top-level `permissions: contents: read`. Every job that touches the registry declares **both**
`contents: read` and `packages: write` — job-level permissions *replace* the top-level block
rather than adding to it, so declaring `packages: write` alone would set `contents` to `none`
and break `actions/checkout` (review note 3).

Every action pinned by **full commit SHA with a `# vN` comment**, matching the convention every
`uses:` line in `ci.yml` already follows. Reuse the SHAs already pinned there for
`actions/checkout` rather than introducing a second pin for the same action.

**`validate` job** — runs first, and everything else `needs` it.

- On a tag push: assert the ref is valid semver (`v<major>.<minor>.<patch>` with optional
  prerelease/build metadata). `vfoo` matches the `v*` trigger but yields no `type=semver` tag,
  and without this gate would still publish `latest` and `sha-*` (review note 5). Fail the run.
- On `workflow_dispatch`: assert `image_tag` matches `^test-[a-z0-9][a-z0-9-]*$`. This stops a
  dispatch from moving `latest`, a released version, or an existing `sha-*` (review note 4).
- On a tag push: query the registry and **fail if the exact version tag already exists**. GHCR
  tags are mutable; a re-run of a tag build should not silently replace what consumers already
  pulled (review note 9).

**`build` job** — a matrix over `{platform: linux/amd64, runner: ubuntu-latest}` and
`{platform: linux/arm64, runner: <ARM runner label>}`, so each architecture builds natively.

- `docker/setup-buildx-action` (SHA-pinned) — required, because the `type=gha` cache backend is
  not supported on the default Docker driver (review note 7).
- `docker/login-action` against `ghcr.io` with `github.actor` and `secrets.GITHUB_TOKEN`
  (review note 2).
- `docker/build-push-action` building and pushing **by digest**, not by tag
  (`outputs: type=image,push-by-digest=true,name-canonical=true,push=true`).
- Cache `type=gha` with a **per-platform `scope`** and `mode=max`. A shared default scope lets
  the two concurrent matrix jobs overwrite each other's cache, and `mode=min` drops the builder
  stage's compiled `mysqlclient` wheels — the only expensive part of this build (review note 7).
- Write each digest to a file and upload it as an artifact.

**`merge` job** — `needs: build`. Logs in again (a different runner), downloads the digest
artifacts, and joins them into a single multi-arch manifest with `docker buildx imagetools
create`, tagged from `docker/metadata-action`:

- `type=semver,pattern={{version}}` — `v0.2.0` → `0.2.0`. Fires only on tag events.
- `type=raw,value=latest`, enabled only on a `v*` tag event.
- `type=sha,prefix=sha-`, **enabled only on a tag event** — an unconditional `type=sha` means a
  `test-1` dispatch also publishes a `sha-*` tag that the documented cleanup would miss
  (review note 4).
- `type=raw,value=${{ inputs.image_tag }}`, enabled only on `workflow_dispatch`.

Set `flavor: latest=false` and add `latest` through the gated `type=raw` above —
metadata-action's automatic `latest` handling does not express "tags only, never dispatch",
and a manual smoke-test publish must not move `latest`.

Finish with `imagetools inspect`, and **record the resulting manifest digest** in the job
summary so there is a durable record of what was published under each tag (review note 9).

**Provenance attestation** — `actions/attest-build-provenance` on the merge job, so a public
consumer can verify the image was built by this workflow from this repository. Requires
`id-token: write` and `attestations: write` on that job, in addition to `contents: read` and
`packages: write`.

### `.github/workflows/ci.yml` — build validation

A new `docker` job, `needs: changes`, `if: needs.changes.outputs.code == 'true'` — the same
gate `lint` and `test` use, so a Markdown-only pull request skips the image build exactly as
it skips the suite.

It builds **without pushing** (`push: false`), for `linux/amd64` only. Validating one
architecture is the deliberate trade: the point is catching a broken `Dockerfile`, which is
architecture-independent in every realistic case, and building both on every pull request
would double the cost of the project's most common CI event to re-prove something the release
path proves anyway.

No registry login, no `packages:` permission — this job never touches GHCR.

### `docker-compose.release.yml` — new

```yaml
services:
  app:
    build: !reset null
    image: ghcr.io/misnow1/network-addresser-9000:<version>
```

**`build: !reset null` is load-bearing, and its exact form matters.** Compose merges mappings,
so an override that sets only `image:` inherits the base file's `build:` — verified by running
`docker compose config`, which emitted both keys, leaving Compose free to build locally instead
of pulling. The `!reset` tag must be applied to the *value* (`build: !reset null`); written as
`!reset build: null` it does not work, and was observed to drop only the nested `dockerfile:`
key while leaving `build.context` in place (review note 1).

`!reset` requires **Compose ≥ 2.24**; state this in the README next to the invocation, since a
too-old Compose fails in the worst possible way — by quietly building from source and appearing
to work.

Everything else — `db`, the healthcheck, the env plumbing, the port binding — is inherited from
`docker-compose.yml`. The version is pinned literally, not floated to `latest`, so a deploy is
reproducible and bumping it is a visible edit:

```
docker compose -f docker-compose.yml -f docker-compose.release.yml up -d
```

### `.dockerignore` — tightened

Add `prod/`, `.env.deployment`, `docker-compose.env`.

**This has one real behavioural consequence** worth stating in the docs rather than
discovering later: `import_prod_data` defaults to `--data-dir prod`, so once `prod/` is
excluded, running the importer *inside a container* requires mounting the CSVs and passing
`--data-dir` explicitly. That is the correct trade — production network data has no business
in a public image — but it changes a working invocation and should be documented, not left as
a surprise.

### Docs

**`docs/adr/0009-docker-deployment-shape.md`** — a postscript recording that the image is now
built and published to GHCR on release tags, that the shape the ADR decided is unchanged, and
that a deploy may consume a prebuilt image via `docker-compose.release.yml` instead of
building from source. Explicitly note that no new ADR was created because no new architectural
decision was taken.

**`README.md`** — document the pull-based deploy alongside the existing build-from-source
instructions: the two-file compose invocation, the **Compose ≥ 2.24** requirement and why it
matters, and the `--data-dir` consequence above.

**`ROADMAP.md`** — check phase 7's box and rewrite its wording. The recorded scope is "on
`main` merges", which decision 1 changed; leaving it would put the roadmap in contradiction
with the workflow. Preserve the existing note about the phase having been scoped-then-skipped
and deliberately left out of order — that history stays true and is worth keeping. Add
**image vulnerability scanning as a follow-up item**, per the rejected half of review note 9,
so it is recorded rather than forgotten.

## Risks and unknowns

- **The ARM runner label needs confirming.** GitHub-hosted arm64 runners for public repos are
  the premise of decision 2. Verify the exact label the account offers before relying on it;
  if unavailable, the fallback is QEMU for the arm64 half (slow, per decision 2) or amd64-only,
  and that fallback is a decision to escalate rather than take silently.
- **GHCR visibility cannot be set by the workflow.** The first push creates a **private**
  package. Flipping it to public is a one-time manual step in the package settings, and until
  it is done a credential-free `docker pull` will fail. This is a hand-off step for Mike, not
  a build task.
- **The first push also links the package to the repo.** Confirm the package lands under
  `misnow1/network-addresser-9000` and inherits repo permissions, rather than as a
  user-namespace package needing separate access management.
- **`latest` will track prereleases** while every release is alpha. Accepted for now, per
  decision 5; worth revisiting when a stable line exists.
- **Cache growth.** `type=gha` caching is capped per repository, and two architectures with
  `mode=max` and separate scopes will use more of that budget than one shared `mode=min` cache
  would. Not a launch concern; a thing to notice if build times regress.

## Verification

Split by what can actually be checked when — `workflow_dispatch` is only offered for workflows
already on the default branch, so no dispatch-based check is available pre-merge (review
note 8).

**Pre-merge, on the PR:**

- A pull request touching only `*.md` skips the `docker` job, and `lint`/`test` still report,
  so `main`'s by-name required checks are unaffected.
- A pull request touching code runs the `docker` job and it builds green.
- `docker compose -f docker-compose.yml -f docker-compose.release.yml config` shows the `app`
  service with `image:` and **no `build:` key at all**. Assert the absence explicitly — this is
  the exact defect review note 1 found, and it is invisible unless looked for.
- `pre-commit run --all-files` passes.
- The full suite passes: `set -a; source .env; set +a; python manage.py test inventory`.

**Post-merge, once the workflow is on `main`:**

- `workflow_dispatch` with `image_tag: test-1` publishes
  `ghcr.io/misnow1/network-addresser-9000:test-1`, and `docker buildx imagetools inspect`
  shows **both** `linux/amd64` and `linux/arm64` in one manifest.
- `latest` must **not** have moved, and no new `sha-*` tag exists. Check both explicitly.
- A dispatch with `image_tag: latest` is **rejected** by the validate job.
- **Release-candidate path (revision 3).** Push a `v0.2.0-rc.1` tag: it publishes
  `0.2.0-rc.1` and `sha-<short>`, and `latest` does **not** move. Then a `v0.2.0` tag
  publishes `0.2.0` and **does** move `latest`. Assert the non-movement explicitly — a
  `latest` that happens to already point at the right digest proves nothing.
- Flip the package to public, then `docker logout ghcr.io` and pull the literal published tag
  **anonymously**. A pull that only works while logged in means the visibility flip did not
  take.
- **Run the pulled image.** Point `docker-compose.release.yml` at `test-1`, bring it up against
  MariaDB, and confirm the entrypoint's `migrate`/`sync_roles` complete and an HTTP request to
  `/` returns a login redirect rather than a 500. `docker compose config` proves nothing about
  whether the image runs — it resolves tags that do not exist.
- Confirm the image contains **no `prod/` directory** and no `.env.deployment` /
  `docker-compose.env`, by inspecting the container filesystem rather than assuming.
- Delete the `test-1` tag from GHCR.

## Definition of done

**Pre-merge:**

- `publish.yml` and the `ci.yml` `docker` job exist, with every action SHA-pinned.
- `docker-compose.release.yml` pins an explicit version and uses `build: !reset null`.
- `.dockerignore` excludes `prod/`, `.env.deployment`, `docker-compose.env`.
- ADR 0009 carries the postscript; README documents the pull path, the Compose ≥ 2.24
  requirement and the `--data-dir` consequence; ROADMAP phase 7 is checked, its wording matches
  decision 1, and vulnerability scanning is recorded as a follow-up.
- Every pre-merge verification item above passes.

**Post-merge (Mike, or a follow-up session):**

- The smoke test has produced a real multi-arch manifest, been inspected, and been cleaned up.
- The package is public and pulls anonymously.
- The pulled image has been run and serves HTTP.

**Rollback criteria.** If the smoke test fails after merge, the exposure is limited: nothing
consumes the published image until `docker-compose.release.yml` is pointed at a real version,
and no release tag has been cut. Revert the merge commit, or delete the offending package
version and fix forward — there is no deployed state to unwind.
