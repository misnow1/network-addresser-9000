> **Revision 2** — incorporates review notes from an independent plan review.
> See "Review response" for the mapping. Two findings hit the escalation gate and were settled with
> Mike: uniqueness is now **rename-only** (creation is exempt, because the importer creates
> duplicates by design), and the four Amphenol `rdj…` slugs are a typo to be corrected to `rjd…`.
>
> The reviewer was a **same-model-family** agent, not `codex` — no OpenAI budget. Correlated blind
> spots are possible in a way the usual chain avoids.

# Implement ROADMAP phase 18 — hostname computation

## Context

Phase 17 shipped the five components as fields and computed nothing. This phase makes them do
something: assemble a hostname, refuse a hand-typed rename into a duplicate, resolve collisions, and
surface divergence.

`docs/adr/0023-hostname-scheme.md` settles the design and needs no successor. It carries **six
amendments**, all made while planning this phase and all found by measuring the **live database**
rather than the CSVs the ADR was written from. Read them first; they override the decisions they sit
under.

**Measure before implementing. Do not trust the table below.** These figures have moved three times
during planning — once because the database had drifted from the CSVs, once because a duplicate
hostname was fixed by hand mid-plan, and once because 24 Types were given slugs by hand *after*
revision 1 was written.

| | live |
|---|---|
| equipment rows with a non-blank hostname | 83 |
| rows that change under lowercasing | 40 |
| rows longer than 63 chars | 0 (longest is 22) |
| duplicated hostnames | 5, covering 32 rows — all bare model names |
| equipment rows with an `owner` | **9 of 84** |
| Types carrying a `hostname_slug` | **24 of 33** — all 8 switch Types and `NA2-DLINE` blank |

The last two rows drive two decisions. Owner blocks assembly, so almost nothing computes yet and
`hostname_diverges` will fire on ~6 rows, not the estate. And seeding is now mostly *already done*
by hand, which changes what PR 4 has left to do.

## Decisions this plan settles (ADR 0023 left them to the build)

1. **Four PRs**, ordered so the riskiest lands alone and first:

   | PR | Contents | Why separate |
   |---|---|---|
   | 1 | `hostname` → `CharField(63)`, strip+lowercase, **backfill migration**, and the case-sensitivity fallout | The only PR that rewrites live data; must be revertible on its own |
   | 2 | `inventory/hostnames.py` — `assemble_hostname()`, `choose_sequence()`, `hostname_is_taken()` | Pure functions, no call sites; reviewable in isolation |
   | 3 | Rename-only uniqueness, add-form assembly, the recompute action, the advisories, audit | The write paths, all of which depend on PR 2 |
   | 4 | `hostname_diverges` + UI surfacing, and seeding the 9 remaining `hostname_slug`s | Smallest, and the only PR that is now largely already done by hand |

2. **Two functions, not one** (review note 4). `assemble_hostname(obj)` is a pure join over stored
   components — no collision queries — and `choose_sequence(stem, *, exclude)` is the numbering rule
   plus the free-check loop. `hostname_diverges` uses **only** `assemble_hostname()`, so rendering it
   costs no queries beyond the three relation reads. Merging them would put a three-table collision
   query behind a property rendered once per row, which is the objection ADR 0023's rejected
   alternatives already raise against assembly in `save()`.

3. **Everything that computes excludes the object being computed**, by pk and by model — both the
   sibling scan and `hostname_is_taken()`. Without this the recompute action renames a device on
   every run: a device stored as `…-2` sees itself as a numbered sibling and goes to `-3`, then `-4`.
   Recompute must be **idempotent**.

4. **An explicitly-set `hostname_sequence` is honoured**, not overridden. Assembly joins it as-is and
   only bumps if `hostname_is_taken()` says that exact name is occupied. The starting-value table
   applies only when the field is null. Otherwise the first advisory is self-defeating: it tells the
   operator to assign `1` to the bare twin, and the next recompute would ignore it.

5. **The chosen sequence is stored back** on the object alongside the name. It is in all four forms'
   `Meta.fields`, so it can be — and if it is not, every newly-created device diverges the moment
   PR 4 lands.

6. **The backfill migration refuses rather than truncates**, via a pre-check that runs *before* the
   `AlterField` — MySQL cannot roll back DDL, so a failure afterwards would leave the schema changed.

7. **`hostname_is_taken()` relies on the collation**, confirmed as `utf8mb4_uca1400_ai_ci` on both
   hostname columns. Note that this argument does **not** extend to the derived-port-name branch: an
   annotation over `CONCAT_WS` cannot use an index at all. Accepted — the table is small.

8. **The recompute action is available on both hierarchies**, to anyone holding the model's change
   permission — `@admin.action(permissions=["change"])`, matching `pull_cards`
   (`inventory/admin.py:1315`). Its name must be **appended to both admins' explicit `actions`
   lists** (`:1207`, `:1283`) or it never renders, however it is decorated.

## PR 1 — normalise and cap `hostname`

### Model — `inventory/models.py`

`NetworkSwitch.hostname` (`:2427`) and `NetworkDevice.hostname` (`:3337`) drop from
`CharField(max_length=255, blank=True)` to `max_length=63`, and gain strip-and-lowercase in
`clean_fields()`, `clean()` and `save()` — the three-place pattern phase 17 established, for the same
reason: `save()` never calls `clean()`, and `clean_fields()` runs before field validators.

**No `validate_dns_label`** — see ADR 0023 decision 8 as amended.

### Migration `0017_hostname_normalise`

1. `RunPython` pre-check raising with any hostname longer than 63 characters listed, **before** the
   `AlterField`.
2. `AlterField` × 2 for `max_length`.
3. `RunPython` backfill stripping and lowercasing every non-blank hostname on both models —
   **40 of 83 live rows change**. Reverse is a no-op, commented: the original casing is
   unrecoverable and re-uppercasing would be a guess.

`apps.get_model()`, not the real model, so the migration does not re-run `save()`'s normalisation.
Deliberately **not** audited — it is a historical-model data fix, not an operator action.

### The case-sensitivity fallout — the part revision 1 missed entirely

Lowercasing changes stored values that existing code compares exactly.

- **`verify_prod_import.py:624` and `:760`** compare `hostname != description` against the raw CSV.
  Left alone, the verifier fails for nearly every row from this PR onward. Make both comparisons
  case-insensitive (`.strip().lower()` both sides). This does not breach its independence contract —
  it imports nothing from the importer.
- **Eight case-sensitive assertions** must be updated: `test_prod_import.py:455`, `:539`, `:541`,
  `:553`, `:560` (this one *is* the port property ADR 0022 believed it had protected), and
  `test_ui.py:1048`, `:1098`, `:1119`.

### Tests

- `DM7C-1` normalises to `dm7c-1` through `objects.create()`, `full_clean()`, and the admin form.
- `NetworkDevicePort.hostname` yields `dm7c-1-device-control` for a device whose stored hostname was
  `DM7C-1` **before** the migration — this is what proves the backfill rather than the on-write path.
- The length guard raises on a 64-character hostname and names it.
- A migration test: insert via `bulk_create` to bypass `save()`, migrate, assert lowercase.
- `test_prod_import.py` passes unchanged apart from the listed assertion updates.

## PR 2 — `inventory/hostnames.py`

Pure functions. Nothing imports them yet, which is what keeps PR 3's diff readable.

### `assemble_hostname(components) -> str | None`

Returns `None` when a **blocking** component is missing — owner or the Type's `hostname_slug`.
Otherwise joins the non-blank components with `-`:

```
owner.slug · rack.location_slug · type.hostname_slug · hostname_purpose · hostname_sequence
```

**Takes components, not an object** (review note 7): on an add form, `self.instance` is still empty
because `ModelForm._post_clean()` runs `construct_instance()` *after* `clean()`, so the submitted
values live in `cleaned_data`. A small adapter builds the component tuple from either source, the
same way `_fill_rack_derived_owner_default()` takes `cleaned_data` explicitly.

No queries beyond the three relation reads. Location is read through the rack and is simply absent
for spare-pool equipment. Never reads through to `rack.owner` — that fallback belongs to the
recompute action alone, so the value is stored rather than inherited.

### `choose_sequence(stem, *, exclude_switch_pk=None, exclude_device_pk=None) -> int | None`

| State of the stem | Start at |
|---|---|
| nothing exists | `None` — take the bare name |
| bare name exists, no numbered siblings | **2**, leaving `1` for the advisory |
| any numbered sibling exists | **highest + 1** |

Then increment until `hostname_is_taken()` says free. Highest + 1, never lowest-free, so a gap left
by a deleted device is not reused. Sibling detection matches stored hostnames equal to the stem or
`stem-<digits>`, whatever their origin — **excluding the object being computed**.

Skipped entirely when `hostname_sequence` is already set (settled decision 4).

### `hostname_is_taken(name, *, exclude_switch_pk=None, exclude_device_pk=None) -> bool`

```python
NetworkSwitch.objects.filter(hostname=name).exclude(pk=exclude_switch_pk)
NetworkDevice.objects.filter(hostname=name).exclude(pk=exclude_device_pk)
NetworkDevicePort.objects
    .filter(source_type_port__hostname_suffix__gt="")
    .exclude(device__hostname="")            # Concat would yield "-suffix"; the property returns None
    .exclude(device_id=exclude_device_pk)    # a rename must not be blocked by its own ports
    .annotate(derived=Concat("device__hostname", Value("-"), "source_type_port__hostname_suffix"))
    .filter(derived=name)
```

Plain `=`, not `__iexact`. Blank names are never taken.

### Tests

Table-driven over the numbering rule, one case per row, plus: a gap (`-1`, `-3` present → `-4`); a
hand-typed sibling counted; a device colliding with a derived *port* name; each blocking component
absent → `None`; every optional component absent → bare `owner-typeslug`; **self-exclusion**
(computing for an object that already holds a name in its own stem returns that same name).

## PR 3 — the write paths

### Uniqueness — rename-only

`_validate_hostname_unique()` on both models, shaped like `_validate_static_address()`
(`inventory/models.py:463`) including its `exclude_*_pk` parameters, inheriting its known race (#5).

**Enforced only when `pk is not None` and `hostname` differs from the stored value** — ADR 0023
decision 6 as twice amended. Creation is exempt because the importer creates duplicates by design
(`import_prod_data.py:1226` calls `full_clean()` with a CSV hostname; the CSV repeats `IK42`
eighteen times), so enforcing there would break every rebuild. Blank exempt.

Docstring must record that **hostnames are not unique in the database and no code may assume it.**

### Add-form assembly — `inventory/admin.py`

In `NetworkDeviceAddForm.clean()` and `NetworkSwitchAddForm.clean()`, **immediately after the
`_fill_rack_derived_owner_default()` call at `:585` / `:705` and before the device form's
`rack is None` early return at `:587`** — otherwise every spare-pool device silently skips assembly,
which is exactly the case ADR 0023 says must still work. The switch form has no such early return;
that asymmetry is the same class of trap phase 17 documented for `Meta.fields`.

Blank hostname only. A typed value is never overwritten. The change form neither assembles nor
re-derives.

### The recompute action

Appended to `NetworkDeviceAdmin.actions` and `NetworkSwitchAdmin.actions` (settled decision 8):

1. If `owner` is blank and the object has a rack, set `owner = rack.owner` and store it.
2. `assemble_hostname()`. If blocked, skip and report which component was missing.
3. `choose_sequence()` if the sequence is null; store it back.
4. Store the name, overwriting whatever was there.
5. Report per object: renamed, **unchanged**, or skipped with a reason.

Saved per row, like `pull_cards` (`:1330-1339`), so each rename is audited. Objects are processed
sequentially, each saved before the next is computed, so selecting 17 identical amps yields 17
distinct names.

### The advisories

`ModelForm.clean()` has **no request**, so the messages cannot be emitted there (review note 6).
`clean()` stashes them on the form (`self._hostname_advisories`); `ModelAdmin.save_model()` emits
them, having both. The action emits its own directly.

- Where the bump started at 2 because the colliding twin has no sequence: name that twin, recommend
  assigning it `1`.
- Where the rack has a `location_slug` and a sequence was assigned: note that a purpose reads better
  than a number.

### Audit — `config/settings.py`

`"hostname"` is in **neither** `include_fields` whitelist. As it stands an Editor could rename any
selection of equipment with no trace. Add it to both, and add an audit test in the shape of
`DepartmentAuditTests`.

### Tests

A hand-typed hostname survives creation; a blank one is filled; **a spare-pool device with no rack
still assembles**; the change form does not assemble; `objects.create()` does not; the action fills a
blank owner from the rack and stores it; **recompute twice yields the same name** and reports
"unchanged" the second time; an explicitly-set sequence is honoured, so the advisory's own remedy
works (twin assigned `1` by hand → recompute yields `…-1`, not `…-3`); recompute over 17 identical
devices yields 17 distinct names; renaming into a duplicate is refused; **creating** a duplicate is
allowed; editing an unrelated field on one of the 32 duplicated rows saves cleanly; the advisories
fire through the admin **add view**, not the bare form; **`test_prod_import.py` passes unchanged**.

## PR 4 — divergence and the remaining seeding

### `hostname_diverges`

Read-only property on both models: `assemble_hostname()` returns a name, a stored hostname exists,
and they differ. No new field. Uses `assemble_hostname()` only, so it runs no collision query.

Reconcile with ADR 0023 decision 9, whose wording is "every component present" — that is stricter
than intended and would make divergence unreachable, since no live device has both a purpose and a
sequence. The operative definition is the one above.

**Surfacing, with the query budget in mind** (review note 8). The read-only UI asserts an *identical*
query count for a 2-row and a 50-row page (`test_ui.py:1777-1791`), and `FieldSpec`'s docstring
forbids an accessor that queries per row. So:

- `rack_detail.html` and `device_detail.html` get the marker, and **their own querysets** gain
  `select_related("owner", "rack", "device_type")` — the registry hints never reach these two views.
- `networkswitch`'s registry spec gets it as a `FieldSpec`, since `NetworkSwitch` has no canonical
  view and would otherwise be the one model with the property and no way to see it. Decide
  explicitly whether `spare_pool.html` carries it.
- The admin filter is a **`SimpleListFilter`** — a property cannot be a `list_filter` entry, and this
  repo has no existing one to copy. Its `queryset()` scans in Python with `select_related`; 84 rows
  today.
- Flat query budgets are an acceptance criterion, not an afterthought.

### Seeding the remaining `hostname_slug`s

**Mostly already done by hand.** 24 of 25 device Types carry a slug; what remains is the 8 switch
Types and `Neutrik NA2-DLINE`.

`HOSTNAME_SLUGS: dict[tuple[str, str], str]` is **derived from the live values**, not invented —
several are not what a naive constant would hold (`plm20q`, `avioao2`, `rio3224d3`, `dantx`/`danrx`).
Getting this wrong means a rebuild silently produces a different estate from the one running.

The four Amphenol entries were entered as `rdj1212` / `rdj2203` / `rdj32a3` / `rdj32u1` against models
`RJD1212-0050` etc. **Settled with Mike: a typo.** The constant carries `rjd…`, and PR 4 includes a
one-off correction of the four live rows.

Applied by the importer (rebuilds) and a data migration matching on `(manufacturer, model)` that
**does not overwrite a non-blank slug** — except for the four Amphenol corrections, which are
explicit. Keyed on the model, not the profile, so both `IK-42` profiles get `ik42`. Not `slugify()`.

`verify_prod_import.py` re-derives its own copy rather than importing the constant.

### Tests

`hostname_diverges` is `False` when components are missing, `False` when the stored name matches,
`True` when it differs; the marker renders through the canonical redirect for Rack and Device and
through the registry for Switch; **query budgets stay flat**; the `SimpleListFilter` selects the right
rows; **every `(manufacturer, model)` the importer creates has a `HOSTNAME_SLUGS` entry** (set
equality, the shape `test_ui.py:1777` uses); a Type with an operator-set slug is not overwritten; the
four Amphenol rows are corrected; the data migration is idempotent.

## Verification

```bash
set -a; source .env; set +a
python manage.py test inventory
```

Record the baseline first. PR 4 rebuilds from the CSVs and runs `verify_prod_import.py`.

**`hostname_diverges` will be near-silent at first — about 6 rows, not the estate.** Owner blocks
assembly and only 9 of 84 equipment rows have one. The estate becomes visible to the indicator as
operators run recompute, which sets the owner *and* the name in one step, so a recomputed row does
not diverge either. Revision 1 of this plan predicted a noisy rollout; that was wrong.

## Review response

| Note | Resolution | Section |
|---|---|---|
| 1 (P0) | **Escalated and settled with Mike.** Verified `import_prod_data.py:1226` calls `full_clean()` with a CSV hostname and that the CSV repeats `IK42` ×18 — enforcing uniqueness on creation would break every rebuild and the whole `test_prod_import` module. Uniqueness is now rename-only (`pk is not None`). ADR 0023 decision 6 amended a second time. | PR 3 "Uniqueness"; ADR 0023 decision 6 |
| 2 (P0) | Accepted — verified `verify_prod_import.py:624`/`:760` compare against the raw CSV case-sensitively, and that `test_prod_import.py:560` asserts the very port-property string ADR 0022 believed it had protected. PR 1 now owns the case-sensitivity fallout, and ADR 0023's "those tests need no change" bullet is corrected. | PR 1 "case-sensitivity fallout"; ADR 0023 Consequences |
| 3 (P1) | Accepted. Self-exclusion is now a settled decision, and idempotence is a named test — without it recompute renamed a device on every run, which no listed test would have caught. | Settled decision 3; PR 2/3 Tests |
| 4 (P1) | Accepted. Split into `assemble_hostname()` and `choose_sequence()`; `hostname_diverges` uses only the former, so it runs no collision query. PR 4's definition reconciled against ADR 0023 decision 9, whose "every component present" wording is unreachable in practice. | Settled decision 2; PR 2; PR 4 |
| 5 (P1) | Accepted — verified both admins declare explicit `actions` lists (`:1207`, `:1283`), so a decorated-but-unlisted method never renders. Registration is now a settled decision and is tested through the admin URL. | Settled decision 8; PR 3 |
| 6 (P1) | Accepted — no admin here passes a request into its forms, so `messages.info()` cannot fire in `clean()`. The `save_model()` hand-off is now named explicitly. | PR 3 "The advisories" |
| 7 (P1) | Accepted, both halves. `assemble_hostname()` takes components rather than an object, because `construct_instance()` runs after `clean()`; and assembly is placed before the device form's `rack is None` early return at `:587`, which would otherwise skip every spare-pool device. | PR 2; PR 3 "Add-form assembly" |
| 8 (P1) | Accepted. Named the querysets that gain `select_related`, and made flat query budgets an acceptance criterion. This is a second reason to keep the numbering rule out of the property. | PR 4 "Surfacing" |
| 9 (P1) | Accepted — verified against the live database: 24 of 25 device Types now carry hand-set slugs, entered after revision 1 was written; 8 switch Types and `NA2-DLINE` remain; 33 Types, not 32. The constant is now derived from live values rather than invented, and settled decision 2's rationale is rewritten. The `rdj`/`RJD` transposition was escalated; Mike settled it as a typo. | Context; PR 4 "Seeding" |
| 10 (P2) | Accepted — measured: 9 of 84 rows have an owner, so 6 diverge, not 49. The "noisy rollout" warning was wrong and is replaced with the measured figure and its cause. | Verification; ADR 0023 decision 10 |
| 11 (P2) | Accepted. An explicitly-set sequence is honoured; the starting table applies only when null. Without this the first advisory's own remedy was unreachable, which is a good catch. | Settled decision 4; PR 3 Tests |
| 12 (P2) | Accepted — verified `"hostname"` is in neither `include_fields` whitelist, so a mass rename would leave no audit trail. Added in PR 3; the PR 1 backfill stays deliberately unaudited. | PR 3 "Audit" |
| 13 (P2) | Accepted — a property cannot be a `list_filter` entry and this repo has no `SimpleListFilter` to copy. Named explicitly, with its cost. | PR 4 "Surfacing" |
| 14 (P2) | Accepted, both halves, plus the observation that settled decision 7's index argument does not extend to a `CONCAT_WS` annotation — recorded as accepted rather than quietly implied. | Settled decision 7; PR 2 |
| 15 (P2) | Accepted. `NetworkSwitch` has no canonical view, so it gets the marker through its registry spec; `spare_pool.html` is called out as an explicit decision. | PR 4 "Surfacing" |
| 16 (P2) | Accepted, both. A set-equality test that the seeding constant covers every Type the importer creates, and a batch-recompute test asserting N distinct names. | PR 4 Tests; PR 3 Tests |
| 17 (P3) | Accepted. All counts reconciled to the live measurements (83, 32, 33, 9 of 84), in this plan, ADR 0023 and `ROADMAP.md`. | Throughout |
