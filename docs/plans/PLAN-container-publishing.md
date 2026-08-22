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
   load-bearing.
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

## The build

One PR. The pieces are interdependent — the docs describe the workflow, the override pins the
image the workflow produces — so a docs-first split would document something that does not yet
exist.

### `.github/workflows/publish.yml` — new

Triggers: `push` on `tags: ['v*']`, plus `workflow_dispatch` with a required `image_tag`
input.

Top-level `permissions: contents: read`. Only the jobs that touch the registry widen this to
`packages: write` — least privilege, matching `ci.yml`'s existing top-level `contents: read`.

Every action pinned by **full commit SHA with a `# vN` comment**, matching the convention every
`uses:` line in `ci.yml` already follows. Reuse the SHAs already pinned there for
`actions/checkout` rather than introducing a second pin for the same action.

**`build` job** — a matrix over `{platform: linux/amd64, runner: ubuntu-latest}` and
`{platform: linux/arm64, runner: <ARM runner label>}`, so each architecture builds natively.
Per the distribute-across-runners pattern: build and push **by digest** rather than by tag
(`outputs: type=image,push-by-digest=true,name-canonical=true,push=true`), write each digest
to a file, and upload it as an artifact. Layer caching via `type=gha` so repeat builds do not
recompile `mysqlclient` from scratch.

**`merge` job** — `needs: build`. Downloads the digest artifacts and joins them into a single
multi-arch manifest with `docker buildx imagetools create`, tagged from
`docker/metadata-action`:

- `type=semver,pattern={{version}}` — `v0.2.0` → `0.2.0`. Fires only on tag events.
- `type=raw,value=latest`, enabled only when the ref is a `v*` tag.
- `type=sha,prefix=sha-`.
- `type=raw,value=${{ inputs.image_tag }}`, enabled only on `workflow_dispatch`.

Set `flavor: latest=false` and add `latest` explicitly through the gated `type=raw` above —
metadata-action's automatic `latest` handling does not express "tags only, never dispatch",
and a manual smoke-test publish must not move `latest`. Finish with an
`imagetools inspect` so the run's log records what was actually published.

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

Overrides the `app` service to `image: ghcr.io/misnow1/network-addresser-9000:<version>`, with
no `build:` key. Everything else — `db`, the healthcheck, the env plumbing, the port binding —
is inherited from `docker-compose.yml`.

The version is pinned literally, not floated to `latest`, so a deploy is reproducible and
bumping it is a visible edit. Document the two-file invocation:

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
instructions, including the two-file compose invocation and the `--data-dir` consequence
above.

**`ROADMAP.md`** — check phase 7's box and rewrite its wording. The recorded scope is "on
`main` merges", which decision 1 changed; leaving it would put the roadmap in contradiction
with the workflow. Preserve the existing note about the phase having been scoped-then-skipped
and deliberately left out of order — that history stays true and is worth keeping.

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
- **Cache growth.** `type=gha` caching is capped per repository, and two architectures share
  that budget. Not a launch concern; a thing to notice if build times regress.

## Verification

- A pull request touching only `*.md` skips the `docker` job, and `lint`/`test` still report,
  so `main`'s by-name required checks are unaffected.
- A pull request touching code runs the `docker` job and it builds green.
- `workflow_dispatch` with `image_tag: test-1` publishes
  `ghcr.io/misnow1/network-addresser-9000:test-1`, and `docker buildx imagetools inspect`
  shows **both** `linux/amd64` and `linux/arm64` in one manifest. `latest` must **not** move.
- The published image contains no `prod/` directory and no `.env.deployment` /
  `docker-compose.env` — verify by inspecting the image filesystem, not by assuming.
- `docker compose -f docker-compose.yml -f docker-compose.release.yml config` resolves to the
  pinned image with no `build:` key.
- Delete the `test-1` tag from GHCR afterwards.
- The full suite still passes: `set -a; source .env; set +a; python manage.py test inventory`.

## Definition of done

- `publish.yml` and the `ci.yml` `docker` job exist, with every action SHA-pinned.
- `docker-compose.release.yml` pins an explicit version.
- `.dockerignore` excludes `prod/`, `.env.deployment`, `docker-compose.env`.
- ADR 0009 carries the postscript; README documents the pull path and the `--data-dir`
  consequence; ROADMAP phase 7 is checked and its wording matches decision 1.
- A `workflow_dispatch` smoke test has produced a real multi-arch manifest, been inspected,
  and been cleaned up.
- The package is public, and `docker pull` works with no credentials.
