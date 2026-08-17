# Implement ROADMAP phase 18 — hostname computation

## Context

Phase 17 shipped the five components as fields and computed nothing. This phase makes them do
something: assemble a hostname, refuse a hand-typed duplicate, resolve collisions, and surface
divergence.

`docs/adr/0023-hostname-scheme.md` settles the design and needs no successor. It carries **four
amendments** made while planning this phase, each found by grilling the ADR against the **live
database** rather than the CSVs it was written from — decisions 6, 7, 8 and 10. Read the amendments
first; three of them change what this phase builds.

**The CSVs are stale as a source of truth.** 49 hostnames have been renamed by hand into scheme
shape since the import, and every prose value is gone. Numbers below are measured against the
deployment database (`network-addresser-9000-db-1`), not `prod/*.csv`.

| | live |
|---|---|
| equipment rows with a non-blank hostname | 83 |
| rows that change under lowercasing | 40 |
| rows longer than 63 chars | 0 (longest is 22) |
| duplicated hostnames | 5, covering 32 rows — all bare model names |
| Types carrying a `hostname_slug` | **0 of 32** |

Re-measure before implementing rather than trusting these: they moved twice during planning, once
because the database had drifted from the CSVs and once because a duplicate was fixed by hand
mid-plan.

That last row is why decision 10 was amended: without seeding, this phase ships inert.

## Decisions this plan settles (ADR 0023 left them to the build)

1. **Four PRs**, ordered so the riskiest lands alone and first:

   | PR | Contents | Why separate |
   |---|---|---|
   | 1 | `hostname` → `CharField(63)`, strip+lowercase, **backfill migration** | The only PR that rewrites live data; must be revertible on its own |
   | 2 | `inventory/hostnames.py` — `compute_hostname()`, `hostname_is_taken()`, the numbering rule | Pure functions, no call sites; reviewable in isolation |
   | 3 | Uniqueness in `full_clean()`, add-form assembly, the recompute action, the advisories | The write paths, all of which depend on PR 2 |
   | 4 | `hostname_diverges` + read-only UI marker + admin filter, and the `hostname_slug` seeding | Seeding lands last so the indicator has something to compare against |

   PR 2 is inert by construction — nothing calls it — which is what makes PR 3's diff readable.

2. **`hostname_slug` seeding rides in PR 4, not PR 3.** Seeding is what makes `hostname_diverges`
   fire across the estate, so the indicator and its trigger should land together and be revertible
   together. Landing seeding earlier would make PR 3's recompute action start renaming equipment
   before anyone can see what diverged.

3. **The backfill migration refuses rather than truncates.** A pre-check raises with the offending
   hostnames listed if any exceeds 63. None does today; the guard exists because MySQL's own error
   at `ALTER` time names a column and a row number rather than the problem, and because silent
   truncation of a hostname is data loss.

4. **`hostname_is_taken()` relies on the collation, not `__iexact`.** The database's utf8mb4
   collation is case-insensitive, which is what `Department` already leans on
   (`PLAN-hostname-ingredients.md` decision 7). `__iexact` would force a `LOWER()` and lose the
   index.

5. **The advisories fire from the admin layer only.** They are `messages.info()` calls needing a
   request, which is why ADR 0023 decision 5 puts assembly on the add-form and action paths.
   `compute_hostname()` itself returns the name and the advisory *reasons*; the caller decides
   whether it has a request to show them on.

6. **The recompute action is available on both hierarchies** and to anyone holding the model's
   change permission — `@admin.action(permissions=["change"])`, matching `pull_cards`
   (`inventory/admin.py:1315`). An Editor can already rename a device by hand; doing it by action
   is not a greater power.

## PR 1 — normalise and cap `hostname`

### Model — `inventory/models.py`

`NetworkSwitch.hostname` (`:2427`) and `NetworkDevice.hostname` (`:3337`) drop from
`CharField(max_length=255, blank=True)` to `max_length=63`, and gain strip-and-lowercase in
`clean_fields()`, `clean()` and `save()` — the three-place pattern phase 17 established for every
component field, and for the same reason: `save()` never calls `clean()`, and `clean_fields()` runs
before field validators.

**No `validate_dns_label`.** See ADR 0023 decision 8 as amended: the importer commits every row to
`construct → full_clean() → save()` and writes `hostname = row.description`, which is still prose in
the CSVs, so a validator would break a rebuild — and a switch's hostname is the only human-readable
label it has.

### Migration `0017_hostname_normalise`

1. A `RunPython` pre-check that raises `RuntimeError` listing any hostname longer than 63
   characters, before anything is altered.
2. `AlterField` × 2 for the `max_length` change.
3. A `RunPython` backfill stripping and lowercasing every non-blank hostname on both models —
   **40 of 83 live rows change**. Reverse is a no-op with a comment saying why: the original casing
   is unrecoverable, and re-uppercasing would be a guess.

Use `apps.get_model()`, not the real model, so the migration does not run `save()`'s normalisation
and is a pure data operation.

### Tests

- A row stored as `DM7C-1` normalises to `dm7c-1` through `objects.create()`, `full_clean()`, and
  the admin form — all three paths, since settled decision 6 of phase 17 exists precisely because
  they differ.
- `NetworkDevicePort.hostname` yields `dm7c-1-device-control` for a device whose stored hostname was
  `DM7C-1` before the migration — the concrete divergence `models.py:4303`'s docstring defers here.
  This is the test that proves the backfill, not merely the on-write path.
- The migration's length guard raises on a 64-character hostname and names it.
- A migration test asserting the backfill actually ran (build a row with `bulk_create` to bypass
  `save()`, run the migration, assert lowercase).

## PR 2 — `inventory/hostnames.py`

Pure functions. Nothing imports them yet.

### `compute_hostname(obj) -> HostnameResult | None`

Returns `None` when a **blocking** component is missing — `obj.owner` or
`obj.<type>.hostname_slug` (ADR 0023 decision 1). Otherwise joins the non-blank components with
`-`:

```
owner.slug  ·  obj.rack.location_slug  ·  type.hostname_slug  ·  hostname_purpose  ·  hostname_sequence
```

Location is read through the rack (`obj.rack.location_slug`) and is simply absent for spare-pool
equipment, which has no rack. `compute_hostname()` **never** reads through to `rack.owner` — that
fallback belongs to the recompute action alone (ADR 0023 decision 5), so the value is stored rather
than inherited.

The return carries the assembled name, the chosen sequence, and a list of advisory reasons; the
caller decides whether it has a request to render them on.

### `hostname_is_taken(name, *, exclude_switch_pk=None, exclude_device_pk=None) -> bool`

Three tables, per ADR 0023 decision 6:

```python
NetworkSwitch.objects.filter(hostname=name)
NetworkDevice.objects.filter(hostname=name)
NetworkDevicePort.objects.filter(source_type_port__hostname_suffix__gt="")
    .annotate(derived=Concat("device__hostname", Value("-"), "source_type_port__hostname_suffix"))
    .filter(derived=name)
```

Plain `=`, not `__iexact` (settled decision 4). Blank names are never taken — blank is exempt
throughout.

### The numbering rule

Per ADR 0023 decision 7 as amended. Choose a starting sequence, *then* increment until free:

| State of the stem | Start at |
|---|---|
| nothing exists | no sequence — take the bare name |
| bare name exists, no numbered siblings | **2**, leaving `1` for the advisory |
| any numbered sibling exists | **highest + 1** |

Highest + 1, never lowest-free, so a gap left by a deleted device is never reused — a retired
hostname is referenced by DNS, switch configs and physical labels this system cannot see.

Sibling detection is a query for stored hostnames matching the stem exactly or `stem-<digits>`,
regardless of how they were created; a hand-typed `mps-avio-aes-9` occupies the namespace as surely
as a computed one.

### Tests

Table-driven over the numbering rule, one case per row above plus: a stem with a gap
(`-1`, `-3` present → `-4`, not `-2`); a hand-typed sibling counted; a device colliding with a
*derived port* name; each blocking component absent yielding `None`; every optional component
absent yielding a bare `owner-typeslug`.

## PR 3 — the write paths

### Uniqueness — `full_clean()`

A `_validate_hostname_unique()` on both models, shaped like `_validate_static_address()`
(`inventory/models.py:463`) including its `exclude_*_pk` parameters, and inheriting its known race
(#5) rather than introducing a stricter mechanism for names.

**Enforced only when `hostname` is being set or changed** — ADR 0023 decision 6 as amended. Compare
against the stored value; if unchanged, skip. This grandfathers the 32 already-duplicated rows,
which would otherwise be unsaveable with no way out, while still refusing a rename *into* a
duplicate or a new duplicate. Blank exempt.

Record in the docstring that **hostnames are not unique in the database and no code may assume they
are.**

### Add-form assembly — `inventory/admin.py`

`NetworkDeviceAddForm.clean()` and `NetworkSwitchAddForm.clean()`, beside
`_fill_rack_derived_owner_default()` (`:376`, called at `:585` and `:705`): if `hostname` is blank,
assemble and fill it. A typed value is never overwritten. Add-only — the change form neither
assembles nor re-derives.

### The recompute action

`@admin.action(permissions=["change"], description="Recompute hostname")` on both admins, following
`pull_cards` (`:1315`):

1. If `owner` is blank and the object has a rack, set `owner = rack.owner` and store it.
2. Assemble. If blocked, skip and report which component was missing.
3. Bump per the numbering rule until free.
4. Store, overwriting whatever was there — that is the point of an explicit action, unlike the
   creation path.
5. Report per object: renamed, unchanged, or skipped with a reason.

### The advisories

Both are `messages.info()`, both about already-saved rows, neither an action:

- Where the bump started at 2 because the colliding twin has no sequence: name that twin and
  recommend assigning it `1`.
- Where the rack has a `location_slug` and a sequence was assigned: note that a purpose reads
  better than a number.

### Tests

A hand-typed hostname survives creation; a blank one is filled; the change form does not assemble;
`objects.create()` does not; the recompute action fills a blank owner from the rack and stores it;
a device with no rack and no owner is skipped with a reason; recompute overwrites a hand-typed
name; renaming into a duplicate is refused; **editing an unrelated field on one of the 34
duplicated rows saves cleanly** (the regression the amendment exists to prevent); the advisories
fire on the right conditions and not otherwise.

## PR 4 — divergence and seeding

### `hostname_diverges`

A read-only property on both models: `compute_hostname()` returns a name, a stored hostname exists,
and they differ. No `hostname_is_computed` field — state that can itself go stale is what the
indicator exists to catch (ADR 0023 decision 9).

Surfaced as a marker on `rack_detail.html` and `device_detail.html` (which `model_detail` redirects
to, so the registry `detail_fields` never render — the trap phase 17 documented), and as an admin
list filter on both equipment admins.

### `hostname_slug` seeding

A `HOSTNAME_SLUGS: dict[tuple[str, str], str]` constant keyed on `(manufacturer, model)` — not on
the profile, so `IK-42 — with Dante Card` and `— without Dante Card` both get `ik42`. Applied by:

- `import_prod_data.py`, for rebuilds
- a data migration matching on `(manufacturer, model)`, for the database that already exists and
  will not be rebuilt to pick these up

Not `slugify()`: it is wrong for `IK-42` → `ik-42`, `SQ-5`, `DM7-EX`, `NA2-DLINE`, which is the trap
ADR 0023 already documents. A Type whose `(manufacturer, model)` is absent from the constant is left
blank, not guessed at.

`verify_prod_import.py` re-derives its own copy rather than importing the constant, per its
independence contract.

### Tests

`hostname_diverges` is `False` when components are missing (the common case today), `False` when the
stored name matches, `True` when it differs; the marker renders through the canonical redirect, not
the registry view; the admin filter selects the right rows; seeding sets `ik42` on both IK-42
profiles; an unlisted Type is left blank; the data migration is idempotent and does not overwrite an
operator-set slug.

## Verification

```bash
set -a; source .env; set +a
python manage.py test inventory
```

Record the baseline before touching code. PR 4 additionally rebuilds from the CSVs and runs
`verify_prod_import.py`.

**Expect `hostname_diverges` to fire widely once PR 4 lands.** 49 hostnames were renamed by hand
into scheme shape, and any that do not match what computation produces will be flagged. That is the
indicator working, and it is the reconciliation surface between the manual renaming and the scheme —
but it is not a quiet rollout, and the PR description should say so.

Found while measuring this phase and **already fixed by hand**: two switches in WPM1SR slots 1 and 2
were both named `mps-wpm1sr-sg350-1`. A data error rather than an import artifact, and correcting it
dropped the grandfathered set from 34 rows to 32. All 32 that remain are equipment still carrying
the bare model name the importer gave it, which is exactly what PR 3's recompute action is for.
