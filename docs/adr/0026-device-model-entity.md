# A device model is an entity; a Type is a profile of one

`docs/Device Model Description.md` asks for something small — a sentence saying what an
`Amphenol RJD32A3-0050` *is*, because the part number says nothing to anyone who does not already
know. `ROADMAP.md` records the wall it hits: ADR 0010 makes `(manufacturer, model, name)` the whole
identity of a Type, with no bare hardware-model entity behind it, so there is nowhere model-level
to put the text.

The doc's load-bearing claim is that the description belongs to the **model**, not the profile: a
Lab.Gruppen LM26 is the same box whether its profile is Switched or Redundant, and hanging the text
on the profile invites two profiles of one model describing themselves differently — which the doc
names as the anti-pattern to prevent.

This ADR creates the missing entity rather than adding a field plus a convention. It covers devices
only; `NetworkSwitchType` keeps its current shape and is tracked by issue #78, but every argument
below is written to apply unchanged to switches when that work is done.

## The workaround is already in main

The strongest evidence is not an argument. It is that the codebase has **already been forced to
simulate this entity twice**, because the data is model-level and the schema is not.

`import_prod_data.py:145` declares `HOSTNAME_SLUGS`, a constant keyed on `(manufacturer, model)`,
and `0018_seed_hostname_slugs.py` keeps a second copy of it. That migration explains itself:

> Matches on (manufacturer, model), not the profile — a Type's `hostname_slug` depends only on its
> hardware, and this migration must give both IK-42 profiles (etc.) the same value, exactly as
> ADR 0023 decision 1 requires.

That is a `NetworkDeviceModel` table implemented as a dictionary. `NetworkDeviceType.hostname_slug`
carries the same admission in its own `help_text` — *"deliberately not unique: two profiles of one
model both carry the same abbreviation"* — which is a comment where a constraint belongs. Phase 17
saw the wall and accepted the duplication; this ADR removes it.

## What the production data actually shows

**The collapse is real but small.** The importer catalog and the live database agree exactly: 23
device types across **22 distinct `(manufacturer, model)` pairs**. Exactly one model carries more
than one profile — Martin Audio IK-42, `with Dante Card` and `without Dante Card`. Both are
hostname-computing (the without-card profile still has a Control port), so both need
`hostname_slug = "ik42"`, and today nothing enforces that.

**Case drift is already prevented; punctuation drift is not.** The database is MariaDB 11.8 on
`utf8mb4_uca1400_ai_ci`. Measured directly against it:

| Comparison | Equal? |
|---|---|
| `DiGiCo` = `Digico` | **yes** |
| `Lab.Gruppen` = `Lab Gruppen` | no |
| `Lab Gruppen` = `LabGruppen` | no |

So `unique_device_type` already treats the two casings of DiGiCo as one string, and the new
`(manufacturer, model)` constraint will too. Only punctuation and spacing can produce two rows for
one box. This matters because the catalog holds word-mark spellings (`DiGiCo`, `Lab.Gruppen`) while
operators are expected to type the plain brand (`Digico`, `Lab Gruppen`) — the repository is already
inconsistent about it, 59 `DiGiCo` against 14 `Digico`.

## Decision

### 1. `NetworkDeviceModel` owns the hardware identity and the description

A new `AuditedModel` with `manufacturer`, `model`, and `description`, unique on
`(manufacturer, model)`. `NetworkDeviceType` drops its two `CharField`s for a
`ForeignKey(NetworkDeviceModel, on_delete=PROTECT)`, and its unique constraint becomes
`(device_model, name)`.

`description` is `CharField(255, blank=True)`, matching its `NetworkDeviceTypePort` sibling. Blank
is a first-class state: the data migration produces it for all 22 rows, and nothing depends on it
being filled.

One description per model is therefore a structural fact, not a policed one. This is the whole
reason for preferring an entity: the alternative invariant — *every row sharing
`(manufacturer, model)` carries an equal description* — is not expressible as a `CHECK` or a
`UNIQUE` in MariaDB, and would need a trigger or Python-side enforcement.

### 2. `hostname_slug` belongs on the model too, and moves in a second PR

It is the same class of fact as the description, and its failure is worse: ADR 0023 decision 1 makes
`hostname_slug` **blocking**, so a divergent value between two profiles of one model does not merely
mislabel — it silently produces different computed hostnames for identical hardware. ADR 0023's own
rejected-alternatives section already worries about exactly this string, noting `slugify("IK-42")`
gives `ik-42` where the name in use is `ik42`.

Moving it retires `HOSTNAME_SLUGS`'s device half and both copies of the "keyed on the model, not
the profile" comment.

**Delivery is split.** PR 1 creates the entity with `description`. PR 2 moves `hostname_slug` onto
it. The design is settled here so nothing ships knowingly inconsistent, but no single PR carries the
whole radius — `hostname_slug` has ~18 sites in `models.py`, 61 in `tests.py`, and live readers in
`hostnames.py` and `admin.py`, all shipped and in use since phase 18.

This **amends ADR 0023 decision 1**, whose table places `hostname_slug` "on both Type models". After
PR 2 it is on `NetworkDeviceModel` and on `NetworkSwitchType`, until the switch work aligns them.

### 3. The lock moves to the foreign key; the model's own text is editable

`device_model_id` joins `NetworkDeviceType._locked_snapshot()` in place of `manufacturer` and
`model`. Re-pointing a type at a *different* model once instances exist stays forbidden, exactly as
today, with one field instead of two.

**`manufacturer` and `model` on the model row itself are not locked.** ADR 0010 locked them because
renaming one profile's copy "would leave existing instances holding a stale copy" — that hazard is
an artifact of denormalization. With one row, an edit updates every profile of that model
coherently, which is the correct behaviour rather than drift. This is the payoff the extraction is
being bought for: correcting `Lab.Gruppen` to `Lab Gruppen` becomes one admin edit that carries
LM26, LM44 and PLM20000Q with it, where today it is impossible on all three.

It is also the consistent choice. `description` and `hostname_slug` sit outside the lock under
ADR 0023's settled decision 3 — *a typo'd abbreviation must stay fixable without creating a new
named profile*. Locking a display-only manufacturer string while leaving the slug that feeds
computed hostnames editable would be backwards.

### 4. No structural guard against punctuation-variant duplicate models

`Lab Gruppen LM26` and `Lab.Gruppen LM26` can coexist as two rows, each with its own description —
the doc's anti-pattern relocated one level up. This ADR accepts that, and does not add a normalized
uniqueness key.

The picker is where it is won: creating a Type means *selecting* a model, so an operator sees
`Lab.Gruppen LM26` in the list and picks it. Typing a fresh manufacturer string becomes the
deliberate path rather than the default one. A normalized key (strip non-alphanumerics, unique on
that) is real machinery for a case the picker mostly prevents, and it is unforgiving — it would
permanently refuse two genuinely distinct models whose names differ only by punctuation.

To be precise rather than to overclaim: **extraction does not eliminate this failure mode, it makes
it visible and repairable.** Two near-identical rows sit adjacent in one list, and decision 3 means
the text is editable. Under today's denormalized scheme the same mistake is invisible and, because
the fields are locked, permanent.

Merging two model rows — re-pointing every type under A to B, then deleting A — has no admin
affordance and is not built here; issue #79 tracks it. There is nothing to merge: the live estate
has zero duplicates. The action is prophylactic, and it has a real conflict case deserving its own
design (if both models have a profile named `Default`, the merge collides on `(device_model, name)`
and must make the operator rename one first).

### 5. The importer reads an optional Device Models CSV

A fourth source file, `MPS Audio Network Standards - Device Models.csv`, in the same `--data-dir`,
with columns `Manufacturer`, `Model`, `Description`, `Hostname Slug`.

**Optional.** If absent, the importer warns and leaves descriptions blank. Blank is valid by design,
so a missing file must not fail an import, and requiring it would break every existing test path.

A CSV rather than another hardcoded constant, even though `DEVICE_TYPES` is a Python tuple and this
is the first CSV to feed the device catalog. Descriptions are prose that will be reworded; a
spreadsheet is the right editor. More importantly it gives `verify_prod_import.py` an independent
source — its docstring forbids sharing the importer's helper on the grounds that a check reading the
importer's own constant proves nothing, which is precisely why migration 0018 had to keep a
duplicate copy of `HOSTNAME_SLUGS`.

PR 1 reads the first three columns; PR 2 starts reading `Hostname Slug` and retires the device half
of `HOSTNAME_SLUGS`. The fourth column exists from the start so the sheet is authored once.

`prod/` is gitignored (`.gitignore:26`), so a filled-in example ships at
`docs/examples/MPS Audio Network Standards - Device Models.csv`: all 22 models, every slug copied
from `HOSTNAME_SLUGS`, and descriptions filled only where `docs/Device Model Description.md` states
them outright. The other 15 are blank rather than invented.

### 6. The description surfaces in three places, and not in `__str__`

- **Registry `list_columns` and `detail_fields`** (`views.py:1537`). `NetworkDeviceType` sets no
  `canonical_detail_view`, so unlike `Rack` and `NetworkDevice` its registry fields genuinely render.
- **The admin type picker**, via `label_from_instance` on the `device_type` field
  (`admin.py:2442`, `2536`), so the dropdown reads
  `Amphenol RJD32A3-0050 — Default (Dante Interface with AES3 I/O)`. This is the one that matters:
  selection time is when the confusion happens.
- **`device_detail.html`, by hand.** `NetworkDevice` sets `canonical_detail_view`, so its registry
  `detail_fields` never render — the trap ADR 0023's consequences section documents. Answering "what
  *is* the box in slot 4?" needs a template edit.

**Not `__str__`.** It would fix every dropdown at once, but it is used inside error messages —
`models.py:308` and `models.py:4261` both build
``f"{device_type}'s {type_port.description!r} port is operator-addressed…"`` — where a
60-character description turns a terse error into a paragraph. It would also churn audit-log
entries and every test asserting on `str()`.

### 7. Devices only

`NetworkSwitchType` is untouched. Switches are fewer, the doc is entirely about devices, and opaque
model numbers are a device problem — nobody is unsure what a switch does. The same extraction
applies to `NetworkSwitchType` unchanged, and every argument above was written to be cited rather
than re-derived.

The cost, stated plainly: until that lands, the two Type models diverge — a device type picks a
model row, a switch type carries its own strings — and `HOSTNAME_SLUGS` survives holding only its
four switch entries.

## Rejected alternatives

**`description` on `NetworkDeviceType` plus a validator.** The cheap option the roadmap names. Two
profiles of one model would each carry their own copy, and keeping them equal is not expressible as
a database constraint in MariaDB — it needs a trigger or a Python check that `QuerySet.update()` and
`bulk_create()` bypass, which is the enforcement gap ADR 0010 already documents for locked fields.
It also leaves `hostname_slug` duplicated forever, and would make the eventual entity a second
migration over the same tables.

**Renaming `NetworkDeviceTypePort.description`** to free the word. The port field is the *port
purpose* label — required, unique per type, and the key for `NetworkDevice.operator_addresses`.
Rejected: the collision is cross-table and therefore never ambiguous at a call site
(`device_type.device_model.description` and `type_port.description` read differently everywhere), and
the rename would touch ~28 sites, a migration, two constraint renames, and committed ADR 0010/0022
text for naming hygiene alone. The two fields keep the same name; `help_text` and prose carry the
distinction, and the port one is always "port description".

**A normalized uniqueness key on `(manufacturer, model)`.** See decision 4.

**Moving `hostname_slug` in the same PR as the description.** Cleanest history and one migration,
but it puts a change to live hostname computation in the PR whose job is creating a table. Split
instead; the decision is still made here.

**A `Manufacturer` entity now.** The right eventual fix for the naming half — it would make
`Digico`/`DiGiCo` unrepresentable rather than merely repairable — but it is a second extraction on
top of this one, and it does nothing for the `model` half of the key. Tracked as issue #80.

## Consequences

- **A post-import rename breaks `verify_prod_import.py`.** It looks up device types by
  `manufacturer=`/`model=` (`verify_prod_import.py:562`), so an operator renaming a model after
  import makes the verifier stop matching. This is correct — the verifier's job is to prove the
  import reproduced the sheet, and a rename genuinely is a divergence from it — but it is a
  behaviour change, not an accident.

- **`NetworkDeviceTypeAdmin.get_readonly_fields()` shrinks** (`admin.py:2090`). `manufacturer` and
  `model` are no longer fields on the Type, so the locked list becomes
  `["name", "port_count", "is_add_in_card"]` plus the FK.

- **`sync_roles` must be re-run after migrating.** A new model means new permission rows created by
  `post_migrate`; until then no role holds `view_networkdevicemodel` and the read-only UI hides
  Models from everyone. Same reason ADR 0021 and ADR 0023 both record.

- **`NetworkDeviceModel` must be registered for audit-trail tracking** (ADR 0004). The Type models
  are registered bare and pick up new fields automatically; a new model is not registered at all
  until it is added.

- **The read-only UI's registry-exhaustive tests will fail until updated**, as they did in phase 17.

- **The data migration is deterministic and needs no re-import.** Collapse each distinct
  `(manufacturer, model)` into one row, re-point the FKs, leave `description` blank. Nothing is
  inferred, because the description is new metadata whose correct initial value is blank everywhere.
  Re-importing would reach the identical end state via more steps and discard post-import edits.

- **The word-mark spellings are left as they are.** `Lab.Gruppen` and `DiGiCo` stay in the catalog
  and the database. Decision 3 makes normalizing them a one-row edit whenever that is wanted; doing
  it in the migration would mean changing the importer catalog and `verify_prod_import.py` in the
  same breath, which is a separate decision from creating the entity.

- **No addressing behaviour changes.** No suggestion, materialization, offset or stored address is
  affected, and `DESIGN.md` needs no amendment.

## Known gaps

- **Duplicate models are creatable and not mergeable.** Decision 4 accepts creation; issue #79
  tracks the merge action. The live estate has none today.
- **`HOSTNAME_SLUGS` survives in halves** between PR 2 and the switch work — devices read the table,
  switches read the constant.
- **Migration 0018's divergent copy** carries `("Cisco", "SG350-10P")` that the importer catalog does
  not. Switch-side, so untouched here, but whoever does the switch extraction inherits it.
- **The two Type models diverge** until switches are aligned (#78). See decision 7.
