# Implement ROADMAP phase 23 — a device model is an entity

> **Revision 2** — incorporates review notes from `REVIEW-1-PLAN-adr-0026.md`.
> See "Review response" for the mapping. Revision 1 shipped a backfill that would have
> failed on the real IK-42 data; that is fixed in settled decision 6.

## Context

`docs/adr/0026-device-model-entity.md` landed in f789bce (#85). It answers a small request from
`docs/Device Model Description.md` — a sentence saying what an `Amphenol RJD32A3-0050` *is* — with a
structural change, because ADR 0010 made `(manufacturer, model, name)` the whole identity of a
`NetworkDeviceType` and left nowhere model-level to put the text.

This phase builds it. The ADR's argument was that the codebase has **already been forced to simulate
the entity** as a `(manufacturer, model)`-keyed dictionary. The census below found that is true in
**three** independent places, not the two the ADR named — `import_prod_data.py:173`,
`verify_prod_import.py:113` (deliberately re-declared, so the verifier does not read the importer's
own constant), and `migrations/0018_seed_hostname_slugs.py:45`. This plan's job is to replace those
dictionaries with a table without breaking the lock, the importer, the verifier, or the read-only UI.

Delivery is split into two PRs, as the ADR's decision 2 requires:

| | scope |
|---|---|
| **PR 1** | Create `NetworkDeviceModel` with `description`; re-point `NetworkDeviceType` at it |
| **PR 2** | Move `hostname_slug` onto the model; retire the device half of `HOSTNAME_SLUGS` |

Nothing about addressing changes in either. No suggestion, materialization, offset or stored address
is touched.

**The canonical docs do need amendment, though** — revision 1 claimed otherwise and was wrong.
`DESIGN.md:117–119` lists `Manufacturer` and `Model` as fields *of* `Network Device Type`, and both
`CONTEXT.md:40` and `DESIGN.md:95` state a Type is identified by `(Manufacturer, Model, Name)`. After
PR 1 none of that is true of a device type. See "Canonical docs" below; the confusion in revision 1
was between *addressing behaviour*, which genuinely does not change, and the *data model*, which does.

### Roadmap placement

The work is currently an unnumbered bullet under **Later / not yet designed** (`ROADMAP.md:651`),
where it still describes the fork the ADR has since resolved. PR 1 promotes it to **phase 23** — the
next free number, after phase 22 (Dante device names) — and rewrites the bullet as a phase with a
checklist, matching how phases 17–22 are written. The stale "the fork is a `description` field on
the Type plus a convention, versus a real model row" sentence goes: that decision is made.

### The estate, measured

Numbers below are from the ADR, which measured them against both the importer catalog and the live
database. They are restated here because they size the migration, not to re-derive them.

| | value |
|---|---|
| device types | 23 |
| **distinct `(manufacturer, model)` pairs** | **22** — the row count migration 0021 creates |
| models carrying more than one profile | **1** — Martin Audio IK-42 (`with Dante Card`, `without Dante Card`) |
| duplicate models needing a merge | **0** |
| `HOSTNAME_SLUGS` entries | 26 — 22 device, 4 switch |
| descriptions known today | 7, from `docs/Device Model Description.md`; the other 15 ship blank |

Two consequences for the build:

**The collapse is nearly a no-op on real data.** 23 types become 22 model rows. Only IK-42 exercises
the many-to-one path at all, which means the migration's interesting behaviour is *not* reachable
from a production-shaped fixture — every test of multi-profile collapse has to construct its own
case. Same shape as phase 22's length limits.

**There is nothing to merge, so the merge action stays unbuilt.** Issue #79 tracks it. Decision 4
accepted that duplicates are creatable; this phase does not make them repairable beyond the fact
that decision 3 leaves the text editable.

### The blast radius, counted

A full census of every site touching `NetworkDeviceType.manufacturer` / `.model`:

| | sites |
|---|---|
| `inventory/models.py` | 13 |
| `inventory/admin.py` | 4 |
| `inventory/views.py` | 5 |
| `import_prod_data.py` | 6 (incl. the `DeviceTypeSpec` dataclass fields) |
| `verify_prod_import.py` | ~14, incl. two 3-tuple identity comparisons at `:388` and `:1253` |
| `config/settings.py` | 1 |
| **templates** | **0 direct** — everything renders through `__str__` |
| `tests.py` / `test_ui.py` / `test_prod_import.py` | **56 / 26 / 14 = 96** |

Two findings change the shape of the work:

**Templates never name the fields.** Five templates render `{{ device.device_type }}`
(`device_detail.html:15` and `:71`, `spare_pool.html:55`, `fit_card.html:35`) and all of them go
through `models.py:3542`'s `__str__`. That is the whole template surface, and settled decision 4
below keeps it byte-identical — so the template diff is one added line, not a sweep.

**A test factory exists, but it does not cover "everything else."** `tests.py:148–149` and
`test_ui.py:98–99` are the `_make_device_type` helper's `manufacturer`/`model` defaults, and 106 call
sites go through it. Extending the helper to create-or-get a `NetworkDeviceModel` handles those.

It does **not** handle the rest, and revision 1 was too optimistic here. Measured:

| | sites |
|---|---|
| `_make_device_type(...)` calls (factory covers these) | 106 — 84 `tests.py`, 22 `test_ui.py` |
| **direct `NetworkDeviceType.objects.create(...)`** | **67 — 45 `tests.py`, 22 `test_ui.py`** |

Those 67 pass `manufacturer=`/`model=` and each needs a decision: route through the factory, or
create a `NetworkDeviceModel` explicitly. Prefer routing through the factory where the test does not
care about the model row. On top of that there are direct field assertions to rewrite, e.g.
`test_prod_import.py:468` asserts `device_type.model == "IK-42"`.

**Migration tests are the exception**: `apps.get_model(...)`-based historical tests must keep using
the old fields, because at their point in the graph the fields still exist. Do not sweep those.

Still: extend the helper **first** and re-count before touching anything by hand.

`inventory/hostnames.py`, `suggestions.py`, `dante.py`, `validators.py`, `templatetags/`,
`seed_defaults.py` and `sync_roles.py` have **zero** hits.

## Decisions this plan settles (the ADR left them to the build)

### 1. The picker hook is a `ModelChoiceField` subclass, not `label_from_instance` at two call sites

ADR 0026 decision 6 says the description reaches the admin dropdown "via `label_from_instance` on
the `device_type` field (`admin.py:2442`, `2536`)". **Those two lines are the wrong hook.** Verified
against current main:

- `admin.py` contains **no** `label_from_instance` and **no** `formfield_for_foreignkey` anywhere.
- Lines 2442 and 2536 are both inside the *add-in-card fit* flow, and only reassign `.queryset` on
  an already-constructed field, filtered to `is_add_in_card=True`. They are not the ordinary picker.
- The ordinary picker is on `NetworkDeviceAddForm`, which `NetworkDeviceAdmin.get_form()` installs
  for **add only** (`admin.py:2150`); on change, `device_type` is read-only ("fixed at creation,
  ADR 0010"), so there is no second dropdown to fix.

So: declare a `NetworkDeviceTypeChoiceField(forms.ModelChoiceField)` overriding
`label_from_instance`, and use it for `device_type` on `NetworkDeviceAddForm` via
`Meta.field_classes`. One declaration covers all three flows — ordinary add, and both card-fit
sites, since reassigning `.queryset` preserves the field instance's class. The ADR's *intent* is
unchanged; only its named coordinates were wrong.

Label format: `Amphenol RJD32A3-0050 — Default (Dante Interface with AES3 I/O)` — i.e. `str(type)`
plus ` (description)`, and **plain `str(type)` when the description is blank**, so 15 of 22 models
render exactly as they do today. No empty parentheses.

The queryset needs `.select_related("device_model")`, or the dropdown becomes an N+1 across every
device type.

### 2. The registry slug is `networkdevicemodel`, and the "models/models" collision is accepted

The read-only UI serves every registry entry from `/models/<slug>/` (`urls.py:29`). A device model
therefore lives at `/models/networkdevicemodel/`, and its generic templates are already named
`model_list.html` / `model_detail.html` — where "model" means *Django model*, not *device model*.

Accepted, not fixed. Renaming the URL space is a read-only-UI change with no bearing on this ADR,
it would break every existing bookmark, and ADR 0020 governs that surface. The plan notes it so the
next person reading `model_detail.html` does not assume it is about device models.

### 3. `description` is `CharField(max_length=255, blank=True)`

The ADR says "matching its `NetworkDeviceTypePort` sibling". Confirmed: `models.py:3657` and the
other three `description` fields at 2818, 3351 and 4740 are all `CharField(255, blank=True)`.
`Department.description` (`models.py:668`) is a `TextField` and is the odd one out — do **not**
follow it. A device description is a one-line label for a dropdown; 255 is generous.

### 4. `__str__` on `NetworkDeviceModel` is `f"{manufacturer} {model}"`

No em-dash, no description. It has to compose inside `NetworkDeviceType.__str__`, which stays
`f"{self.device_model} — {self.name}"` and therefore produces byte-identical output to today for
every existing row.

That identity is load-bearing, and the census is what proves it cheap: all five template sites and
both operator-addressed error messages (`models.py:308`, `models.py:4261`) read through `__str__`,
so keeping it stable means **no template sweep, no audit churn, and no test churn on `str()`**. It
is also what makes decision 6's "the description does not go in `__str__`" a free promise rather
than a trade.

### 5. `_locked_snapshot()` keys on `"device_model"` and holds `self.device_model_id`

Not `"device_model_id"` as the key. `_check_locked_fields_unchanged`'s docstring (`models.py:191–196`)
is explicit: keys must be field names as `update_fields` would name them, while values hold **raw
ids** for FK fields. The existing precedent is `models.py:3016` —
`{"switch_type": self.switch_type_id}`. Follow it exactly.

Getting this backwards would not fail loudly; `QuerySet.values()` accepts either spelling, so the
check would keep passing while comparing the wrong things.

**Where it actually bypasses.** Review note 8 pinned this down more precisely than revision 1 did.
An ordinary `save()` or `clean()` *would* still catch a wrong `"device_model_id"` key, so a naive
lock test passes either way. The real hole is `save(update_fields=["device_model"])`: the docstring
at `models.py:198–207` says `update_fields` is **normalized to field names** before comparison, so a
snapshot keyed `"device_model_id"` intersects nothing, and the helper returns early as a deliberate
no-op. The lock is then silently gone on exactly the code path Django's own admin uses.

That dictates the test (see Tests): drive the lock through **both** `update_fields=["device_model"]`
and `update_fields=["device_model_id"]`, and assert the persisted FK is unchanged — not merely that
something raised.

### 6. The migration is two files, not one

`0020_networkdevicemodel.py` — schema: create the table, add `device_type.device_model` as
**nullable**, no constraint changes yet.
`0021_device_model_backfill.py` — data, then constraints, in this exact order:

1. `RunPython(collapse_and_repoint, reverse_code=noop)` — forward collapse (below).
2. `AlterField` `device_model` → `null=False`.
3. **`RemoveConstraint("unique_device_type")`** — must come *before* step 6.
4. `AlterModelOptions` — `ordering` off `manufacturer`/`model`.
5. **`RunPython(noop, reverse_code=repopulate_strings)`** — forward no-op; exists **only** so the
   reverse direction has somewhere to run. See "the reverse is not free" below.
6. `RemoveField` `manufacturer`, `RemoveField` `model`.
7. `AddConstraint` — `unique_device_type` on `["device_model", "name"]`.

Steps 3 and 4 are the forward footgun: the old constraint and the old `Meta.ordering` both name
columns that step 6 drops, and a `RemoveField` under a live constraint fails on MariaDB. `0004_…`
already sequences a `unique_device_type` reshape this way (`:81` remove, `:136` re-add) — follow it.
(Django's autodetector orders it the same way; only the constraint removal is a MariaDB DDL
requirement, `AlterModelOptions` is migration state only. Correct either way — just do not reorder
them.)

**The reverse is not free, and revision 1 got it wrong.** Reversing runs 7→1. Step 6 reversed
re-adds `manufacturer`/`model` as `NOT NULL` with Django's empty-string effective default, so every
row holds `("", "")`. Step 3 reversed then re-adds the old
`unique_device_type` on `(manufacturer, model, name)` — and with all manufacturers and models blank,
any two profiles sharing a name collide. The estate has 22 profiles named `Default`, so the reverse
fails immediately. Putting the repopulation in step 1's `reverse_code` does not help: step 1 reverses
**last**, long after the constraint is back.

Hence step 5. It is a forward no-op whose `reverse_code` repopulates `manufacturer`/`model` from the
FK, and it sits between steps 3 and 6 so that in reverse it runs *after* the columns exist and
*before* the constraint returns. Step 1's `reverse_code` stays `noop`.

The reverse remains **lossy** — every `description` is destroyed, because the text has no column to
go back to. Say so in a comment on the reverse function rather than pretending otherwise.

Split into two files because it makes the data step independently reversible and keeps the DDL
review separate from the data review. Revision 1 also claimed a `RunPython` "cannot reliably see a
column added in the same migration" — **that is false** and should not be repeated:
`0011_device_companions.py` adds `companion_type` at `:86` and reads it from `RunPython` at `:118`,
in one migration. Operations within a migration are sequential. The split is a readability and
reversibility choice, not a correctness one. Both ship in PR 1.

The backfill is deterministic and needs **no re-import** — it reads only the rows already present.
**`.order_by()` is load-bearing and must not be dropped:**

```python
for manufacturer, model in (NetworkDeviceType.objects
                            .order_by()                      # <-- see below; not optional
                            .values_list("manufacturer", "model").distinct()):
    device_model = NetworkDeviceModel.objects.create(
        manufacturer=manufacturer, model=model, description="")
    NetworkDeviceType.objects.filter(
        manufacturer=manufacturer, model=model).update(device_model=device_model)
```

**Why `.order_by()`.** Revision 1 omitted it and the migration would have crashed on the real data.
`Meta.ordering = ["manufacturer", "model", "name"]` (`models.py:3539`) survives into the historical
model, and Django appends ordering columns to a `SELECT DISTINCT`. Measured against this tree on
Django 6.0.7:

```sql
-- without .order_by()  -- note the third column
SELECT DISTINCT `manufacturer`, `model`, `name` FROM `inventory_networkdevicetype`
                          ORDER BY 1 ASC, 2 ASC, `name` ASC
-- with .order_by()
SELECT DISTINCT `manufacturer`, `model` FROM `inventory_networkdevicetype`
```

`name` in the DISTINCT means Martin Audio IK-42 — the one multi-profile model in the estate —
yields **two** rows, and the second `create()` violates `unique_device_model`. This is not a
hypothetical: IK-42 is real production data, so the forward migration fails on every real database
and on any fixture with a second profile. A test must cover it (see Tests).

`created_by` is left null on the created rows (the field is `null=True`, `SET_NULL`), as in every
other data migration here. `description` is blank for all 22 — the correct initial value, since it
is new metadata nothing can infer.

Note the `.distinct()` is doing real work under a **case-insensitive** collation: if the estate ever
held `DiGiCo` and `Digico` as separate type rows they would already have violated
`unique_device_type`, so they cannot — but the same collation means `.distinct()` returns one of the
two spellings arbitrarily where a future dataset differs only in case. Today's data has no such
pair; the migration does not need to handle it, and should not pretend to.

**The reverse migration is lossy and must say so.** Reversing re-populates `manufacturer`/`model`
from the FK and drops every `description`. That is unavoidable — the text has no home to go back to
— so the reverse function carries a comment stating it, rather than being written as
`migrations.RunPython.noop`.

`unique_device_type` **keeps its name** while changing to `["device_model", "name"]`. A rename is
churn for no gain, the name still describes the constraint, and the census confirms **no test
asserts on the constraint name**.

### 7. `verify_prod_import.py` looks up through the FK; the rename divergence is left as designed

Three sites, not the one the ADR named: `:562` filters
`NetworkDeviceType.objects.filter(manufacturer=…, model=…)`, and `:388` / `:1253` build
`(manufacturer, model, name)` identity tuples for equality comparison. All become
`device_model__manufacturer=…` / `device_type.device_model.manufacturer`.

The ADR's consequence stands unchanged: an operator who renames a model after import makes the
verifier stop matching, and that is correct behaviour — the verifier proves the import reproduced
the sheet. No `--ignore-renames` escape hatch is built.

Line 1086's switch equivalent is untouched (decision 7 of the ADR).

## PR 1 — the entity

### `inventory/models.py`

**New `NetworkDeviceModel(AuditedModel)`**, placed immediately *above* `NetworkDeviceType` (line
3499) so the FK target is defined first and the two read together:

- `manufacturer = CharField(max_length=100)`, `model = CharField(max_length=100)` — same widths as
  the fields they replace (`models.py:3506–3507`).
- `description = CharField(max_length=255, blank=True)`, with `help_text` saying what it is *for*
  ("What this hardware is, e.g. 'Dante Interface with AES3 I/O'") and distinguishing it from
  `NetworkDeviceTypePort.description`, per ADR 0026's naming note.
- `Meta`: `UniqueConstraint(["manufacturer", "model"], name="unique_device_model")`, a
  `CheckConstraint` for each of manufacturer/model non-blank (matching
  `networkdevicetype_name_not_blank`), `ordering = ["manufacturer", "model"]`.
- `__str__` per settled decision 4.
- **No `save()` override and no `_locked_snapshot()`** — decision 3 of the ADR makes this row
  editable. This is the one place the new model deliberately departs from its neighbours, and its
  docstring says why, citing ADR 0026 decision 3 and the ADR 0010 reasoning it supersedes.

**`NetworkDeviceType` changes** (all line numbers current):

- `3506–3507`: drop `manufacturer`, `model`. Add
  `device_model = ForeignKey(NetworkDeviceModel, on_delete=PROTECT, related_name="profiles")`.
- `3536`: `unique_device_type` becomes `fields=["device_model", "name"]`, name unchanged.
- `3539`: `Meta.ordering` → `["device_model__manufacturer", "device_model__model", "name"]`.
- `3542`: `__str__` → `f"{self.device_model} — {self.name}"`.
- `3604–3605`: `_locked_snapshot()` per settled decision 5.
- `3500–3504`: the docstring gains a sentence saying the hardware identity now lives on
  `NetworkDeviceModel` and this class is the profile — which is the ADR's title, and the thing a
  future reader most needs.

### `inventory/migrations/0020_networkdevicemodel.py`, `0021_device_model_backfill.py`

Per settled decision 6. Latest existing migration is `0019_dante_unit_id.py`.

### `config/settings.py`

Add `"inventory.NetworkDeviceModel"` to `AUDITLOG_INCLUDE_TRACKING_MODELS`, **bare** — following
`Department` and `Owner`, whose comment already states the rule for "a descriptive vocabulary table
with no per-field scoping needed". Place it adjacent to `"inventory.NetworkDeviceType"` (line 283)
with a one-line comment citing ADR 0026.

This is not tidiness. Two things depend on it:

- ADR 0004 requires removals always be logged, and a new model gets nothing until registered.
- `NetworkDeviceType` is registered bare *today*, which means `manufacturer` and `model` are
  currently tracked audit fields on it. Moving them to another model silently changes what the
  audit trail records for a type — registering the new model bare is what keeps the coverage whole
  rather than merely equivalent-looking.

### `inventory/admin.py`

- **New `NetworkDeviceModelAdmin`** — `list_display = ["manufacturer", "model", "description"]`,
  `search_fields = ["manufacturer", "model", "description"]`, `AuditedModelAdminMixin` +
  `AuditlogHistoryAdminMixin`, `show_auditlog_history_link = True`, matching its neighbours.
  **No `get_readonly_fields`** — decision 3 of the ADR.
- `2076` `list_display`: `"manufacturer"`, `"model"` → `"device_model"`. Fine — `list_display`
  renders through `__str__`.
- `2078` `search_fields`: **not** `"device_model"`. A bare FK name in `search_fields` becomes
  `device_model__icontains`, which raises at the moment an operator types in the search box.
  Verified against this tree: `FieldError: Unsupported lookup 'icontains' for ForeignKey or join on
  the field not permitted.` Use `["device_model__manufacturer", "device_model__model", "name"]`, and
  add a test that actually exercises the changelist with a `?q=` term — the existing tests assert the
  *contents* of `search_fields`, which is exactly the assertion that would have passed here.
- Add `list_select_related = ["device_model"]` to keep the changelist off an N+1.
- `2090` `get_readonly_fields`: returns `["device_model", "name", "port_count", "is_add_in_card"]`.
  Its comment at `2083` says "ADR 0010's lock on manufacturer/model/name/port_count" — rewrite to
  name `device_model` and cite ADR 0026 decision 3 for *why* one FK replaced two strings.
- **`NetworkDeviceTypeChoiceField`** per settled decision 1, wired through
  `NetworkDeviceAddForm.Meta.field_classes`.
- **Nested `select_related` for the device changelist and the card-fit lists.** `admin.py:2123` and
  `:2448` currently reach a device type one level down; once `NetworkDeviceType.__str__` dereferences
  `device_model`, each rendered row costs an extra query. They become `device_type__device_model`.

### `inventory/views.py`

- `networkdevicetype` spec (starts `1532`). The census confirms it sets **no**
  `canonical_detail_view` — only `rack` (`1380`) and `networkdevice` (`1637`) do — so unlike those
  two, its registry fields genuinely render and editing them is worth doing.
  - `1538–1539` (list) and `1546–1547` (detail): the `Manufacturer`/`Model` pairs collapse to a
    single `FieldSpec("Device model", "device_model", render="relation")`.
  - `detail_fields` **only** gains `FieldSpec("Description", "device_model.description")`. Dotted
    traversal is supported — `FieldSpec`'s docstring (`views.py:1116`) documents
    `"native_vlan.vlan_id"` and short-circuits on the first `None` hop. The changelist already
    carries six columns and the description is long, so it stays off the list page.
  - `1570` `ordering` → `("device_model__manufacturer", "device_model__model", "name")`.
  - `list_select_related` / `detail_select_related` gain `"device_model"`;
    `list_permissions` / `detail_permissions` gain `"inventory.view_networkdevicemodel"`.
- **Every other surface that renders a device type also needs the permission and the join.**
  Revision 1 added both only to the type registry, which is the same gap this file already records
  two Codex findings for (`views.py:900–906`, `:990–999`). The rule here is *declare every model the
  page reads*, and after this change reading a device type's display string reads a device model.
  - `device_detail` (`:898–920`): add `"inventory.view_networkdevicemodel"` — it renders the type
    string **and**, per the template change below, the description directly.
  - `spare_pool` (`:990–999`): add it; the type column is a device type string.
  - the `networkdevice` registry spec (`:1640–1644`): add it.
  - Joins to widen at the same time: `views.py:953` (`select_related("device_type", …)`) and `:965`
    (the `installed_cards` prefetch) become `device_type__device_model`; likewise `:1018` and
    `:1639`, and the verifier loop at `verify_prod_import.py:783`.
  - Each of the three surfaces needs a **partial-grant test** — a user holding every other view
    permission but not `view_networkdevicemodel` must get a 403, which is the shape the existing
    permission tests already use.
- **New `networkdevicemodel` spec** — `list_columns` manufacturer / model / description, matching
  `detail_fields`, plus an `InlineSpec` listing its profiles (accessor `profiles`, columns name /
  port count / add-in card, permission `inventory.view_networkdevicetype`). That inline is what
  makes the "two near-identical rows are visible" claim in ADR decision 4 actually true on screen.

### Canonical docs

Review note 6. These describe the schema and go stale the moment PR 1 lands, so they change in the
same PR:

- **`CONTEXT.md:40`** (Type Profile) — "Identified by `(Manufacturer, Model, Name)`" is now true only
  of a Network *Switch* Type. Reword to give the device side `(Device Model, Name)` and keep the
  switch side as-is, noting the two deliberately diverge until issue #78. The `_Avoid_` line below it
  ("treating `(Manufacturer, Model)` as a Type's whole identity") still stands and needs no edit.
- **`DESIGN.md:95`** — same identity sentence, same split.
- **`DESIGN.md:117–119`** — `Manufacturer` and `Model` are listed as fields of Network Device Type.
  They move to a new `Network Device Model` bullet, which also carries `Description` (and, after
  PR 2, `Hostname Slug`). Add a `Device Model` FK line to the Type in their place.
- **`ROADMAP.md:311–315`** — phase 17's text says the slug duplication "is accepted rather than
  inventing a bare hardware-model entity ADR 0010 deliberately doesn't have." That was true when
  written and is superseded by ADR 0026. **Do not delete it** — append a dated note pointing at
  phase 23, the way this repo keeps ADR 0015's wrong prediction visible on the page.
- **`docs/adr/0023-hostname-scheme.md`** — PR 2 only, already scheduled below.

### `inventory/templates/inventory/device_detail.html`

Line 15 is `<p class="tile__meta">{{ device.device_type }}</p>` — `NetworkDevice` sets
`canonical_detail_view`, so its registry `detail_fields` never render and this is a hand edit, as
ADR 0026 decision 6 says. Add the description as a second line, conditional on it being non-blank,
so the 15 blank models render as today. Per the "no ADR references in UI text" rule, the rationale
goes in a template comment, not on screen.

`device_detail.html:71` (fitted cards), `spare_pool.html:55` and `fit_card.html:35` are left alone —
those rows are already dense, and each device's own detail page carries the description. Settled
decision 4 means all three keep rendering exactly as they do now with no edit.

### `inventory/management/commands/import_prod_data.py`

- `DEVICE_MODELS_CSV_NAME = "MPS Audio Network Standards - Device Models.csv"`, alongside the three
  existing names at lines 75–77.
- A parser in `_prod_import_csv.py` — a `DeviceModelRow` dataclass plus `parse_device_models()`,
  matching that file's existing shape (it already holds `VlanRow`, `RackOffsetRow`, `AddressingRow`,
  `SwitchPortRow` and their parsers).
- **Optional, per ADR decision 5.** If the file is absent: warn, and proceed with every description
  blank. A missing file must not fail an import — blank is valid by design, and every existing test
  path would otherwise break.
- A new stage creating all 22 `NetworkDeviceModel` rows up front, before `_stage7_device_types`
  (`:1006`). Cleaner than `get_or_create` inside the type loop, and it gives the CSV a single place
  to be applied. `_stage7` then looks the model up rather than passing two strings (`:1008–1015`).
- `DeviceTypeSpec` keeps its `manufacturer`/`model` fields (`:284–285`) — the catalog is still
  authored per type, and the model rows are derived from it. Changing the spec's shape is a bigger
  edit for no benefit.
- PR 1 reads `Manufacturer`, `Model`, `Description` and **ignores** `Hostname Slug`. That column
  exists in the example CSV from the start so the sheet is authored once; PR 2 starts reading it.
  The parser must tolerate the column being present and unused without warning.

### `inventory/management/commands/verify_prod_import.py`

Per settled decision 7 — `:562`, `:388`, `:1253`. If the Device Models CSV is present, the verifier
also checks each model row's description against it, reading the CSV directly and never the
importer's helper, which is what that command's docstring requires.

### Tests

New coverage PR 1 must add:

- **The unique constraint** on `(manufacturer, model)`, and that it is **case-insensitive but
  punctuation-sensitive** — `DiGiCo`/`Digico` collide, `Lab.Gruppen`/`Lab Gruppen` do not. The ADR
  measured this against MariaDB 11.8 `utf8mb4_uca1400_ai_ci`; a test pins it, because it is
  collation-dependent behaviour that a database change would silently alter.
- **Two profiles, one model** — the IK-42 shape: both types resolve to one `NetworkDeviceModel`, and
  `(device_model, name)` still rejects a duplicate profile name. `tests.py:1798–1807` is the
  existing `unique_device_type` multi-profile test and is the right place to extend.
- **`PROTECT`** — deleting a model with profiles raises; deleting one without succeeds.
- **The lock**, driven through `update_fields` — not just a bare `save()`. Settled decision 5
  explains why: a bare `save()`/`clean()` catches a wrong snapshot key anyway, so a naive test is
  green under the bug. Required assertions, with instances present:
  - `save(update_fields=["device_model"])` re-pointing the FK raises;
  - `save(update_fields=["device_model_id"])` re-pointing the FK raises (Django accepts both
    spellings, and the helper normalizes them);
  - after each, **re-read from the database and assert the FK is unchanged** — "it raised" is not
    the same as "it did not write";
  - the plain `save()` and `clean()` paths raise too;
  - **without** instances, all of the above succeed.
- **Decision 3's payoff, as a test**: editing `manufacturer` on a model row whose profiles have
  instances succeeds, and every profile reads the new value. This is the behaviour ADR 0010 forbade
  and ADR 0026 deliberately allows — it needs a test asserting the new rule, not just the absence of
  the old one.
- **`__str__` is byte-identical** for a type before and after the change (settled decision 4), and
  the two operator-addressed error messages at `models.py:308` / `4261` still read as they did.
- **The migration — run the operations, do not just call the function.** The existing harness
  (`tests.py:6538–6550`) uses `MigrationExecutor` to reach a historical state and then calls the
  `RunPython` callable directly with a `_FakeSchemaEditor`. That validates data logic and **cannot**
  validate DDL ordering or reversal, which is where both migration defects in this review lived. So:
  - a data-level test in the existing shape: 3 types across 2 models collapse to 2 rows with FKs
    correctly re-pointed;
  - **plus** a `MigrationExecutor` test that migrates `0019 → 0021` and back to `0019` for real, with
    multi-profile data present. `tests.py:2783` and `:6423` are the precedent for driving the
    executor forward against real DDL. Forward proves steps 3/4/6 are ordered correctly; the reverse
    leg is the only thing that would have caught the constraint-before-data defect;
  - **the IK-42 regression specifically**: a fixture with two profiles of one model, asserting the
    backfill produces exactly one `NetworkDeviceModel`. Without `.order_by()` this fails with an
    `IntegrityError`, which is precisely the bug revision 1 shipped.
- **The picker label** — with and without a description, per settled decision 1.
- **Registry exhaustiveness** — the read-only UI enumerates every registered model; those tests fail
  until `networkdevicemodel` is added, exactly as in phase 17.
- **Importer**: with the CSV, descriptions land; without it, the import succeeds and warns.
- **Audit registration.** `config/settings.py` gaining an `AUDITLOG_INCLUDE_TRACKING_MODELS` entry is
  a string in a tuple; a typo or an omission passes every test above. Add create/update/delete audit
  coverage for `NetworkDeviceModel`, following the `Department` precedent at `tests.py:4905–4910`.
- **Admin search.** Exercise the changelist with a `?q=` term against a device model's manufacturer
  and model. Asserting the *contents* of `search_fields` is what would have let the FK defect through.
- **Partial-grant permission tests** for `device_detail`, `spare_pool` and the `networkdevice`
  registry surface, per the views section above.

Existing tests that must change regardless of the factory:

- `tests.py:3594–3595` and `:5116` assert `"manufacturer"`/`"model"` are in
  `NetworkDeviceTypeAdmin.get_readonly_fields()`. They become `"device_model"`.
- `test_prod_import.py:583`, `:586`, `:653` use `NetworkDeviceType.objects.get(manufacturer=…,
  model=…)`; `:680`, `:700` use `device_type__model="LM26"`; `:711`, `:1041`, `:1158` build
  `{(spec.manufacturer, spec.model) for spec in DEVICE_TYPES}`.

Everything else should fall out of extending `_make_device_type` (`tests.py:148`,
`test_ui.py:98`). Do that first and re-count.

## PR 2 — `hostname_slug` moves to the model

Deliberately separate: `hostname_slug` feeds live hostname computation, shipped in phase 18 and in
use since. PR 1 must not carry it.

- Move the field from `NetworkDeviceType` to `NetworkDeviceModel`, unchanged — same `max_length=63`,
  same `validate_dns_label`, same `blank=True`. Its `help_text` loses the "deliberately not unique:
  two profiles of one model both carry the same abbreviation" clause, because after this it *is*
  unique per model, structurally. That sentence was the comment standing in for this constraint.
- The normalization (`strip().lower()`) in `save()` and `clean_fields()` moves with it. Once it
  does, `NetworkDeviceType.clean_fields()` has no body left — delete the override rather than
  leaving it empty, and check what remains of `save()` beyond the lock check.
- Migration `0022`: add the column to `NetworkDeviceModel`, copy each model's value from its
  profiles, drop the column from `NetworkDeviceType`. **Assert rather than assume** that a model's
  profiles agree — they do today by construction, but nothing has ever enforced it, and that is
  precisely the drift the ADR says is unprevented. Fail loudly on disagreement.
- Readers to update: `hostnames.py`, `admin.py`, `models.py` (~18 sites), `views.py`, templates.
- `HOSTNAME_SLUGS` loses its 22 device entries, keeping 4 switch entries, in **both** live copies
  (`import_prod_data.py:173`, `verify_prod_import.py:113`). Migration 0018's frozen copy (`:45`) is
  left alone — it is history and must keep running against old databases.
- **`test_prod_import.py:1137–1198` (`HostnameSlugsConstantTests`) cross-checks all three copies**
  against each other, including the Amphenol case at `:1197–1199`. It will fail the moment the
  constants diverge and must be rewritten to expect the switch-only shape. This is the single test
  most likely to be missed.
- The importer starts reading the `Hostname Slug` column, with `HOSTNAME_SLUGS` as the fallback when
  the CSV is absent — otherwise a slugless import would silently stop computing hostnames.
- **ADR 0023 amendment**: its decision 1 table places `hostname_slug` "on both Type models". PR 2
  amends that text in place with a dated note pointing at ADR 0026 decision 2, rather than leaving a
  committed ADR describing a schema that no longer exists.

### PR 2 tests

Revision 1 specified none, which is how the assertion below would have gone unbuilt. Required:

- **Migration `0022` aborts when two profiles of one model disagree on `hostname_slug`.** This is the
  one that matters. Production data agrees by construction, so a fixture built from production would
  pass against a broken `.first()`-style implementation that silently picks a winner. The test must
  **construct disagreement deliberately** and assert the migration raises with a message naming the
  offending model.
- The agreeing case migrates cleanly and every profile's slug lands on its model.
- Hostname computation is unchanged end to end for a device whose slug moved — same computed
  hostname before and after, which is the whole point of PR 2 being separate.
- `HostnameSlugsConstantTests` (`test_prod_import.py:1137–1198`) rewritten for the switch-only shape,
  including the Amphenol case at `:1197–1199`.
- Migration 0018's frozen `HOSTNAME_SLUGS` copy still runs against an old database — the existing
  `0018` migration tests must stay green untouched.
- Importer: with the CSV's `Hostname Slug` column, slugs land from the sheet; without the CSV, the
  `HOSTNAME_SLUGS` fallback still produces hostnames.
- `NetworkDeviceType.clean_fields()` is deleted rather than left empty, and `save()` still enforces
  the lock — assert both, since deleting an override is easy to over-apply.

## Review response

`REVIEW-1-PLAN-adr-0026.md`, codex, high reasoning, read-only over the tree. Every finding was
checked against the code before being accepted. **All twelve were confirmed and folded in; none were
rejected.** Two were verified by running code rather than reading it, and both turned out to be real
defects that would have reached production.

| Note | Resolution | Section |
|---|---|---|
| 1 · P0 — backfill does not collapse profiles; `Meta.ordering` leaks `name` into `SELECT DISTINCT` | **Folded in.** Confirmed by printing the compiled SQL on Django 6.0.7 against this tree: without `.order_by()` Django emits `SELECT DISTINCT manufacturer, model, name`. IK-42 is real data, so the forward migration would have failed on every production database. `.order_by()` added, with the measured SQL recorded and a regression test required. | Decision 6 |
| 2 · P1 — the six-step order is not reversible; the old constraint returns before the data | **Folded in.** Confirmed by tracing the reverse order 6→1: `RemoveField` reversed restores `NOT NULL` columns with empty-string defaults, then `RemoveConstraint` reversed re-adds `unique_device_type` over 22 rows all named `Default`. A seventh operation — a forward no-op whose `reverse_code` repopulates — now sits between steps 3 and 6. | Decision 6 |
| 3 · P1 — `search_fields = ["device_model"]` raises `FieldError` | **Folded in.** Confirmed by running the lookup: `Unsupported lookup 'icontains' for ForeignKey`. Changed to `device_model__manufacturer` / `device_model__model`, plus a test that exercises the changelist with `?q=` instead of asserting the tuple's contents. | `admin.py` |
| 4 · P1 — nested-FK N+1s once `__str__` dereferences the model | **Folded in.** Confirmed at `views.py:953` and `:965`. All seven cited joins widened to `device_type__device_model`. Correctly framed by the reviewer as a consequence of settled decision 4, not an argument against it. | `admin.py`, `views.py` |
| 5 · P1 — `view_networkdevicemodel` missing from three surfaces | **Folded in.** This repo has an explicit "declare every model the page reads" rule, and `views.py:900–906` already carries comments recording two earlier Codex findings of this exact class. Added to `device_detail`, `spare_pool` and the `networkdevice` registry spec, each with a partial-grant test. | `views.py` |
| 6 · P1 — `CONTEXT.md` / `DESIGN.md` / `ROADMAP.md` go stale | **Folded in.** Revision 1's "`DESIGN.md` needs no amendment" was wrong: `DESIGN.md:117–119` lists Manufacturer and Model as fields of Network Device Type. New "Canonical docs" section. ROADMAP phase 17 gets a dated note rather than a deletion, matching how ADR 0015's wrong prediction is kept visible. | Context, Canonical docs |
| 7 · P1 — the factory does not cover "everything else" | **Folded in.** Measured: 106 `_make_device_type` calls but **67 direct `NetworkDeviceType.objects.create(...)`** sites (45 `tests.py`, 22 `test_ui.py`) that the factory cannot reach. Revision 1's estimate was too optimistic; the numbers are now in the plan, with historical migration tests explicitly excluded from the sweep. | Blast radius |
| 8 · P1 — the lock tests would not catch the silent error | **Folded in**, and sharpened. The bypass is specific to `update_fields`: the docstring at `models.py:198–207` normalizes `update_fields` to field names, so a snapshot keyed `"device_model_id"` intersects nothing and the helper returns early. Tests must drive both spellings and re-read the FK from the database. | Decision 5, Tests |
| 9 · P1 — migration testing must execute the operation list | **Folded in.** Confirmed the existing harness (`tests.py:6538–6550`) calls the `RunPython` callable directly with a `_FakeSchemaEditor`, so it cannot see DDL ordering — which is where findings 1 and 2 both lived. Added a `MigrationExecutor` test running `0019 → 0021 → 0019` for real. | Tests |
| 10 · P1 — PR 2 has no test plan | **Folded in.** New "PR 2 tests" section. The named risk — a `.first()` implementation passing against agreeing production data — is now an explicit requirement to construct disagreement deliberately. | PR 2 tests |
| 11 · P2 — audit registration directed but unproved | **Folded in.** A settings-tuple string typo passes every other test. Create/update/delete audit coverage added, on the `Department` precedent. | Tests |
| 12 · P2 — the stated reason for two migration files is false | **Folded in.** Confirmed: `0011_device_companions.py` adds `companion_type` at `:86` and reads it from `RunPython` at `:118` in one migration. The split survives on reversibility and review-separation grounds; the false justification is removed and flagged so it is not repeated. | Decision 6 |

The reviewer also independently confirmed the claims revision 1 rested on: the autodetector's
operation ordering and the `0004` precedent, the absence of any other live constraint/index/ordering
reference to the removed columns, `admin.py` having neither `label_from_instance` nor
`formfield_for_foreignkey`, no template reading the fields directly, no test asserting the constraint
name, and `sync_roles` enumerating models dynamically (plus running after migrate in
`docker/entrypoint.sh:32–34`). Those needed no change.

## Consequences accepted, not solved

- **Duplicate models are creatable.** ADR decision 4. The picker makes selection the default path;
  nothing structurally prevents `Lab Gruppen LM26` alongside `Lab.Gruppen LM26`. Issue #79 tracks
  the merge action, and there is nothing to merge today.
- **The two Type models diverge** until switches are aligned — a device type points at a model row,
  a switch type carries its own strings. Issue #78. Every argument in ADR 0026 was written to be
  cited rather than re-derived when that happens.
- **Word-mark spellings stay.** `Lab.Gruppen`, `DiGiCo` are left as they are in both catalog and
  database. ADR decision 3 makes normalizing them a one-row edit whenever that is wanted; doing it
  here would mean changing the importer catalog and the verifier in the same breath.
- **`/models/networkdevicemodel/`** reads oddly. Settled decision 2.
- **A post-import model rename breaks `verify_prod_import.py`.** Settled decision 7 — correct, but a
  behaviour change worth stating rather than discovering.

## Risks and what could still be wrong

- **`_check_locked_fields_unchanged` with an FK** is the subtlest change in PR 1, and settled
  decision 5 exists because getting it wrong fails *silently*. Re-read `models.py:191–196` and copy
  `models.py:3016` rather than reasoning from scratch.
- **PR 2's migration assumes profiles agree.** They do today (22 models, one of them multi-profile).
  Assert it; do not trust it — and test the assertion against *deliberately* disagreeing data, since
  a production-shaped fixture cannot distinguish a real check from a silent `.first()`. See PR 2 tests.
- **`sync_roles` must be re-run after migrating**, or the read-only UI hides Device Models from
  everyone — no role holds `view_networkdevicemodel` until then. `sync_roles` enumerates
  `get_app_config("inventory").get_models()` (`:31`), so the new model is picked up automatically;
  it just has to actually run. Same footgun ADR 0021 and ADR 0023 both record.
- **The test diff *is* large** — measured at 67 direct constructors the factory cannot reach, on top
  of the 106 it can. Revision 1 hoped otherwise. Extend the helper, re-count, then work the 67.
- **The importer's new stage changes stage ordering**, and `import_prod_data.py` is heavily
  stage-coupled. Creating model rows before `_stage7` is the obvious placement, but confirm nothing
  earlier already reads device types.

## After merge

- Re-run `sync_roles` on any deployed database.
- Fill in the remaining 15 descriptions in the real `prod/` sheet, using
  `docs/examples/MPS Audio Network Standards - Device Models.csv` as the shape. The example ships
  7 filled and 15 blank rather than inventing text.
- Close nothing automatically: #78 (switch parity), #79 (merge action) and #80 (Manufacturer entity)
  all survive this phase by design.
