> **Revision 2** — incorporates review notes from `REVIEW-1-PLAN-adr-0018.md`.
> See "Review response" for the mapping.

# Implement ADR 0018 — device companions

## Context

`docs/adr/0018-device-companions.md` is committed (`ac32f3a`, `33442b8`) and unimplemented.
It records that a Yamaha DM7C/DM3 reaches stage boxes through a second Dante interface
that the production data carries as its own row — and that **nothing in the model connects
the two**. A console can be created with no interface, an interface with no console, either
can be deleted without the other, and the only thing recording that
`bej-dm3-1-device-control` belongs to `bej-dm3-1` is a hostname the model knows nothing about.

`slot_offset` (ADR 0017) is the wrong instrument and the ADR says so: production sets the
DM7C's interface one address *below* its console (`10.201.6.4` vs `10.201.6.5`) and the DM3's
one *above* (`10.201.6.16` vs `10.201.6.15`). There is no offset to declare. The missing
concept is **existence and lifecycle**, not addressing.

Outcome: `NetworkDeviceType.companion_type` and `NetworkDevice.host`; a host materializes its
companion in the same transaction as its own ports; deleting the host cascades and deleting
the companion alone is refused; the assembly moves as a unit; the two production pairs are
linked by data migration; the importer pairs rows before creating them.

This is lifecycle only. **No address is derived, offset, or recomputed anywhere in this plan.**

## Decisions this plan settles (ADR 0018 left them open)

Resolved with Mike, 2026-08-02.

1. **Companion slot on the host change form: always present, blank = preserve the current
   relative offset.** A filled value is an explicit absolute slot. So an ordinary host move
   relocates both rows with no operator action, which is what decision 4 wants; changing the
   offset stays possible. A prefilled-absolute field would strand the companion whenever the
   operator forgot it — and with no JavaScript (`admin.py:429-432`) the prefill can only be
   computed at GET time, so it *would* be forgotten.
2. **Moves are sequenced vacate-then-place.** Shifting a pair down by one (host 5→4, companion
   4→3) makes the host's `UPDATE` land on the companion's occupied slot, and
   `unique_device_rack_slot` (`models.py:2881`) is checked per statement. Park the companion,
   save the host, then place the companion. Order-independent across shift-down, shift-up,
   swap and cross-rack. **Revised in rev 2:** the park is conditional and is a real `save()`,
   not a `QuerySet.update()` — see "The move path" below, which review note 6 rewrote.
3. **Blank companion hostname copies the host's**, verbatim, on submit. Literal reading of
   decision 1's "prefilled from the host's, editable", expressed as a server-side fallback.
   Duplicate hostnames are already legal (`hostname` is `blank=True`, no unique constraint);
   the importer always passes the real name.
4. **The data migration matches case-insensitively, within one rack, and fails loud.**
   `dm7c-1-device-control` minus the suffix is `dm7c-1`; its host row is `DM7C-1`. Exact
   matching finds the DM3 pair and misses the DM7C one.
5. **The importer pairs on hostname suffix, cross-checked against the type catalog.** Same
   rule as the migration, so the convention is stated once; the catalog cross-check means a
   rename fails loudly rather than pairing two unrelated rows.
6. **The companion slot is required exactly when the companion is unracked and the host is
   being racked.** That is decision 1's own condition applied to the change form, and it
   closes the gap where an assembly created in the spare pool has no offset to preserve.

Decisions taken without asking, for the record:

- **`host` is a `OneToOneField`**, not a plain FK — decision 1 says *exactly one* companion per
  instance, so the DB should say it too. Precedent: `NetworkDevicePort.switch_port` (`:3262`),
  the only other one-to-one here.
- **The companion inherits the host's `port_addressing`.** The assembly is one creation-time
  choice (ADR 0013); a companion materializing DHCP under a static host would be incoherent.
- **The companion's slot is its own occupancy, not part of the host's `slot_span`.** It is a
  separate row with its own ordinal range. Rev 2 note: this does *not* mean the existing
  occupancy query works unchanged — see review note 1 and "Pair-aware occupancy" below.

## Model changes — `inventory/models.py`

### `NetworkDeviceType` (`:2609`)

- `companion_type = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
  related_name="companion_of")`.
- Add `"companion_type": self.companion_type_id` to **both** locked-field dicts —
  `save()` `:2644-2649` and `clean()` `:2681-2686` — so it locks once instances exist exactly
  as `manufacturer`/`model`/`name`/`port_count` do (ADR 0010, decision 5). Reuses
  `_check_locked_fields_unchanged()` (`:146`) unchanged.
- New `_validate_companion_type()`, called from `clean()` **and** `save()` (types are created
  by `objects.create()` in the importer and tests, and `save()` never calls `clean()`), refusing:
  - self-reference (`companion_type_id == self.pk`);
  - a chain downward — the chosen companion itself declares a `companion_type`;
  - a chain upward — `self` is already some other type's `companion_type`
    (`self.companion_of.exists()`) and is now declaring one.
- **Locking (review note 7).** `save()` currently locks only `self` (`:2639`). Take both rows
  in **one** `_lock_type_rows(NetworkDeviceType, self.pk, self.companion_type_id)` call —
  the helper already sorts its ids (`:210-224`), which is exactly what makes two concurrent
  saves acquire them in the same order instead of deadlocking. Run `_validate_companion_type()`
  **after** the lock is held and re-read from the database, not from memory, so a concurrent
  `A.companion_type=B` / `B.companion_type=A` pair cannot both validate against stale state
  and commit a cycle.

### `NetworkDevice` (`:2858`)

- `host = models.OneToOneField("self", null=True, blank=True, on_delete=models.CASCADE,
  related_name="companion")`. `CASCADE` is decision 3 and is what puts the companion on
  Django's delete-confirmation page for free (ADR §"What this asks of ADR 0007").
- Add `"host": self.host_id` to the locked dict in `save()` (`:2905`) and `clean()` (`:2927`) —
  a companion can never be reparented.
- Two creation/move-time inputs that are not fields, following `_port_addressing` (`:2877`)
  exactly — `_`-prefixed class default plus a property and setter, so `objects.create(...)`
  accepts them: `_companion_rack_slot: int | None = None` and
  `_companion_hostname: str | None = None`. The slot setter validates `>= 1` or `None`.
- **`_host_managed_move: bool = False`** — a class-level escape flag, set only by
  `_move_companion()`, modelled exactly on `NetworkDevicePort._deriving_address` (`:3232`,
  docstring at `:3224-3231`). It is the single legitimate writer of a companion's rack/slot.

#### Type compatibility (review note 3)

`host` being merely non-null is not enough. Enforce in **both** `clean()` and `save()`:

- a device whose type is some other type's `companion_type` must have a `host`;
- a device with a `host` must satisfy `self.device_type_id == host.device_type.companion_type_id`;
- a device whose type declares a `companion_type` may not itself have a `host`.

Without the second rule a companion type can be hung off an arbitrary host through the ORM,
and an ordinary type can be made someone's companion — neither is a relationship the declared
type graph allows.

#### Host-managed rack/slot (review note 4)

Admin `readonly_fields` is presentation, not enforcement. A companion's `rack`/`rack_slot`
must be **locked at the model layer** whenever `host_id` is not null: extend the `save()` and
`clean()` locked-field dicts to include them for hosted devices, and skip that lock only when
`_host_managed_move` is set. This is the same shape as `NetworkDevicePort._locked_fields()`
(`:3446-3488`), including its lesson that the **persisted** `host_id` is what must be
consulted — an in-memory `host = None` must not unlock the check.

#### Pair-aware occupancy (review note 1)

`RackSlotAssignmentMixin.clean()` (`:1330`) runs *before* anything in `save()`, and
`_check_rack_slot_not_occupied()` (`:3098-3133`) excludes only `self.pk`. So the shift-down
move is rejected in `clean()` before vacate-then-place ever gets to run, and the companion's
pre-flight sees the host still sitting at its old slot. Creation has the mirror-image
disagreement: the host row does not exist yet during the companion pre-flight, then does.

Fix: `_check_rack_slot_not_occupied()` excludes the **other half of its own pair** — a helper
`_companion_pair_pks()` returning `{self.pk, companion_pk_or_host_pk}` — and the pair's own
two target ranges are then checked against each other explicitly, once, in the host's
pre-flight. Excluding the partner without that explicit check would let a host and its
companion be placed on top of each other.

#### The move path (review notes 2, 5, 6)

`_move_companion()`, called from `save()`'s not-new branch, guarded on `self.host_id is None`
so a companion never re-enters it.

- **`update_fields` correctness (note 5).** Derive the move from **effective post-save**
  values, reconciled through `_normalize_update_fields()` (`:123-143`) — the machinery
  `NetworkDevicePort.save()` already uses for exactly this at `:3336-3376`. When
  `update_fields` is given and includes neither `rack` nor `rack_slot`, **return without
  touching the companion at all**: `save(update_fields=["hostname"])` on a host whose
  in-memory `rack_slot` was mutated must not move anything, and a hostname-only save must not
  park and re-save the companion.
- **Both rows' addresses (note 2).** `_validate_existing_addresses_still_fit()` is reachable
  only from `clean()` (`:1354`), and `save()` never calls `clean()`. So the move path calls it
  explicitly for **the host as well as** the companion. Rev 1 claimed the existing machinery
  covered both rows; that was true on the admin path and false on a bare `host.save()`, and
  the asymmetry — validating the companion's addresses but not the host's — was the worse half.
- **Conditional park, real `save()` (note 6).** `NetworkDevice` is registered with auditlog
  tracking `rack` and `rack_slot` (`config/settings.py:244`), so a `QuerySet.update()` park is
  invisible to the audit trail and makes the following `save()` log a false `None → target`.
  Therefore:
  - park **only** when the host's target range actually overlaps the companion's currently
    occupied range — the rare collision case. Every ordinary move is then a single
    `companion.save()` with a truthful `old → new` audit entry;
  - when a park is needed, do it as a real `save(update_fields=["rack", "rack_slot"])` under
    `_host_managed_move`, so auditlog records `old → None` then `None → target`: two truthful
    entries describing what actually happened inside the transaction, rather than one false
    one. A test asserts the entry pairs for both the collision and non-collision cases.

  The park is still invisible outside the transaction — the surrounding `atomic()` rolls it
  back if placement raises, which the reviewer confirmed independently.

#### Materialization

In `save()`'s `is_new` branch, inside the `transaction.atomic()` already open at `:2902`,
after `self._materialize_ports()` (`:2915`): `self._materialize_companion()` — the ADR's "one
level up" of ADR 0010 seed-once. Builds the companion with `host=self`,
`device_type=self.device_type.companion_type`, `rack=self.rack`,
`rack_slot=self._companion_rack_slot`, `hostname=self._companion_hostname or self.hostname`,
`port_addressing=self.port_addressing`, `created_by=self.created_by`, then `full_clean()` +
`save()` so the companion's own ports materialize through the ordinary path. One transaction,
so a failure anywhere rolls back host, host ports, companion and companion ports.

The `is_new` branch also re-checks the standalone-companion and type-compatibility refusals,
since `save()` never calls `clean()` — that is the `objects.create()` half of the ADR's
coverage list.

#### `clean()` pre-flight

Mirrors how `_check_static_materialization_possible()` (`:2969`) is called from both `clean()`
and `_materialize_ports()`, so the admin gets a form error rather than
`changeform_view()`'s redirect-with-message. Covers: the standalone-companion refusal, type
compatibility, the required companion slot (racked host, and decision 6's unracked-companion
case), and the pair-aware target-range check. Watch the trap at `:3032-3041` — a dict-keyed
`ValidationError` out of a related object's `full_clean()` crashes `add_error()` on a model
with no such field; re-raise `exc.messages` flat.

### Delete guards — the `NetworkDevicePort` pattern one table up

`NetworkDevice` has **no custom manager today**; add one.

- New `NetworkDeviceQuerySet(models.QuerySet)` with a `delete()` that refuses any row with a
  non-null `host_id`, wired with `objects = NetworkDeviceQuerySet.as_manager()`. Copy the
  docstring shape of `NetworkDevicePortQuerySet` (`:3149-3183`), including the paragraph
  explaining that it deliberately does **not** fire under the host's cascade, because Django's
  `Collector` issues child deletes directly (confirmed by the reviewer).
- `NetworkDevice.delete()` override reading the **persisted** `host_id` through a
  `_persisted_host_id()` helper — same reasoning as `_persisted_delete_guard_fields()`
  (`:3561-3587`): `delete()` has no locked-field validation, so `self.host_id` is untrusted.
  Error names the host.

### Documented bypasses (review note 11)

`QuerySet.update()` and `bulk_create()` bypass every guard here, exactly as
`_check_locked_fields_unchanged()`'s docstring already records for locked fields
(`:180-196`). State the same limitation in the new guards' docstrings — companions are not
defended against those paths, and the tests use them deliberately to reach otherwise
unreachable states, which is already how this suite works (`tests.py:3970-3978`).

## Admin — `inventory/admin.py`

- **`NetworkDeviceAddForm`** (`:351`): `Meta.exclude = ["host"]` — currently `[]`, which would
  put the new editable `host` FK straight onto the add page and let an operator build an
  arbitrary parent/child pair (review note 3). Add `companion_rack_slot` (`IntegerField`,
  `required=False`, `min_value=1`) and `companion_hostname` (`CharField`, `required=False`),
  assigned to `self.instance` in `_post_clean()` **before** `super()._post_clean()` — the
  established idiom, with the same `or`-not-`.get(default)` comment. Help text states the
  blank-copies-the-host rule. Filter the `device_type` queryset to exclude types that are some
  other type's `companion_type`; the model refusal remains the enforcement.
- **New `NetworkDeviceChangeForm`**: also excludes `host`. Carries `companion_rack_slot`
  (`required=False`) when `instance.companion` exists, help text naming the companion's current
  slot and offset. Returned from `NetworkDeviceAdmin.get_form()` when `obj is not None`.
- **`NetworkDeviceAdmin.get_readonly_fields()`** (`:857`): keep `device_type`; add `host`
  whenever `obj`; add `rack` and `rack_slot` when `obj.host_id is not None`. Presentation only
  — the model lock above is the enforcement. Add `host` to `list_display`.
- **`NetworkDeviceTypeAdmin.get_readonly_fields()`** (`:841`): add `companion_type`, gated on
  the same `_profile_locked(obj, "devices")` (`:483`). Restrict the `companion_type` dropdown
  to types that neither declare a companion nor are already one, and exclude `self`.
- **Delete error handling (review note 8).** `AuditedModelAdminMixin` (`:47`) overrides only
  `changeform_view`, so a `ValidationError` from `delete()` or `QuerySet.delete()` reaches the
  admin unhandled and 500s on **both** deletion routes. Add a `delete_view()` override with
  the same try/except → `messages.error` + redirect, and the same handling in the module-level
  `delete_selected` action (`:773-789`). Without this the plan's own smoke step is unreachable.
- No template change: `_scary_warning.html` already renders on every inventory delete
  confirmation, and `CASCADE` puts the companion on Django's own object list.

## Migrations — `inventory/migrations/0011_device_companions.py`

Next number confirmed as `0011`. One migration, 0006's shape: the two `AddField`s, then
`RunPython(link_production_companions, reverse_code=migrations.RunPython.noop)` with the
hand-written ADR header comment and the "databases are rebuilt, not migrated over" paragraph
this project repeats.

`link_production_companions(apps, schema_editor)`, via `apps.get_model` historical models
(which have no custom `save()`, so the type lock does not bite):

- for each `NetworkDevice` with `hostname__iendswith="-device-control"` and `host_id` null,
  strip the suffix and look up
  `NetworkDevice.objects.filter(rack=..., hostname__iexact=stem).exclude(pk=...)`;
- exactly one match → set `companion.host`, and set the host's
  `device_type.companion_type` to the companion's `device_type`;
- zero or several matches, or a host type already pointing at a *different* companion type →
  `RuntimeError` naming rack, slot and hostname;
- already-linked rows are skipped, so a re-run is idempotent; an empty database is a no-op.

## Importer — `inventory/management/commands/import_prod_data.py`

- `DeviceTypeSpec` (`:136`) gains `companion_key: str | None = None`; `dm7c` (`:218`) declares
  `"dm7c_devctrl"` and `dm3` (`:237`) declares `"dm3_devctrl"`.
- **`DEVICE_TYPES` is a tuple, not a mapping** (review note 9). Build an explicit
  `SPEC_BY_KEY: dict[str, DeviceTypeSpec]` at module level, raising on a duplicate key, and
  look specs up through it. Rev 1's `DEVICE_TYPES[host_key]` would have raised `TypeError`.
- `_stage7_device_types()` (`:728`): after the catalog loop, a **second linking pass** setting
  `companion_type` — necessary because `dm7c_devctrl` is declared after `dm7c`, so a
  single-pass lookup would `KeyError`. No devices exist yet, so nothing is locked.
- `_DeviceEntry` (`:404`) gains `companion_slot: int | None = None` and
  `companion_hostname: str | None = None`.
- `_classify_addressing_rows()` (`:754`): a **companion pre-pass before the main loop** — the
  existing `consumed`/`by_key` machinery, but keyed on hostname, not the SD12 pass's
  slot-adjacency (`:791`), which cannot express a companion below *and* above its host. For
  each description ending `-device-control` (case-insensitive): stem-match its host
  case-insensitively within the same rack, assert
  `SPEC_BY_KEY[host_key].companion_key == companion_key`, emit **one** `_DeviceEntry` at the
  **host's** slot carrying the companion's own slot and hostname, and `consumed.add()` both
  keys. Zero matches, several, or a catalog mismatch → `CommandError` citing the ADR, never a
  guess — the house style at `:812-829`. The pre-pass runs before the loop because the DM7C
  companion row precedes its host in CSV order while the DM3's follows it.
- `_stage9_devices()` (`:883`): pass `companion_rack_slot=` and `companion_hostname=` to the
  `NetworkDevice(...)` constructor. Nothing else changes.

## Verifier — `inventory/management/commands/verify_prod_import.py`

Independence rule holds: re-declare, never import from the importer (module docstring `:1-19`).

- New `EXPECTED_COMPANION_TYPES: dict[identity, identity]` mapping
  `("Yamaha","DM7C","Default") → ("Yamaha","DM7C","Device Control Interface")` and the DM3
  equivalent, re-typed from ADR 0018.
- `_check_device_type_ports()` (`:811`) asserts each type's `companion_type` matches the
  expectation — including `None` where none is expected, so the new field cannot be silently
  ignored.
- The device-row pass gets its own independent companion match (mirroring its own SD12
  lookahead at `:534-575`) asserting `device.host` links the right two rows, in the right
  direction, at their own slots.
- Both rows are still verified at their own slots with their own addresses, so the verifier's
  address accounting (`:648-659`) balances exactly as before — nothing is derived, and no
  address count changes.

## Tests

New `DeviceCompanionTests` in `inventory/tests.py`, sized like `SlotOffsetAddressingTests`
(`:2165`), using `_make_device_type()` (`:107` — rev 1 cited `:86`, the top of the factory
block). Covers ADR 0018's `## Follow-up` list plus the concrete cases review note 11 named:

- host materializes companion + both port sets in one transaction; a failure anywhere rolls
  back the whole assembly (pattern: `PortProfileAtomicityTests` `:2893`);
- a companion type refused on the bare add page **and** through `objects.create()`; a
  companion attached to a **wrong-typed host** refused; an ordinary type refused as a companion;
- deleting a host removes both rows — through `delete()` **and** `QuerySet.delete()`; deleting
  a companion alone refused through both;
- a host move relocating both rows with **every address unchanged**: the shift-down collision,
  a shift-up, a cross-rack move, an explicit companion slot, and a preserved (blank) one;
- a plain `host.save()` (no `full_clean()`) moving the companion and validating **both** rows'
  addresses; `save(update_fields=["hostname"])` with a mutated in-memory slot moving **nothing**;
- an independent `companion.rack_slot = X; companion.full_clean(); companion.save()` refused;
- auditlog entries correct for both the parked and unparked move (review note 6);
- racking a spare-pool assembly with a blank companion slot refused; with a slot given, both
  rows land;
- `companion_type` locked once instances exist; companion-of-a-companion refused;
  self-referential companion refused;
- hostname fallback: blank companion hostname copies the host's, a given one wins;
- every device type without a `companion_type` creating exactly as it does today.

Admin tests: object-level (bare `AdminSite()` + `RequestFactory`, the `:2149` idiom) for the
readonly sets, the excluded `host` field, and the two new form fields; **full HTTP** for the
add page, a shift-down move, and — because these are the 500s note 8 found — single-object
companion delete and `delete_selected` companion delete, both asserting a message rather than
an exception.

Migration tests, in this repo's established style — import the migration module and call
`link_production_companions(real_apps, None)` directly, as `tests.py:3916-3937` already does
for `0006`'s `seed_defaults` — covering: both production-shaped pairs linked including the
case-insensitive `DM7C-1`/`dm7c-1-device-control` one; a companion whose stem matches nothing;
one whose stem matches several rows; a host type already pointing at a different companion
type; already-linked rows skipped on re-run; and an empty database as a no-op.

`inventory/test_prod_import.py`: `ADDRESSING_ROWS` (`:141`) currently has **no** DM7C, DM3 or
`-device-control` row at all — add a pair in each direction (companion below its host, and
above) to the shared fixture, plus a variant fixture via
`write_fixture_csvs(addressing_source_rows=...)` (`:354`) for the unmatched-companion refusal,
following `ImportProdDataMalformedDmiDanteTests` (`:562`). Add a negative verifier test in the
established style — corrupt with `objects.filter(pk=...).update(host=None)`, assert
`CommandError`.

## Docs

- `CONTEXT.md:42` already carries the Companion Device definition (uncommitted in the working
  tree) — confirm it matches what ships.
- **`DESIGN.md` (review note 13):** its data-model section lists `NetworkDeviceType` and
  `NetworkDevice` field by field, down to ADR 0017's Slot Offset (`:109-120`), and would
  otherwise go stale. Add `companion_type` under Network Device Type and `host` under Network
  Device, with the lifecycle semantics: mandatory composition, created with the host, moves
  with the host, cascades on the host's removal, never independently creatable or deletable.
- Tick the ADR 0018 item in `ROADMAP.md`. Issue #42 stays open: the ADR is explicit that this
  neither closes nor depends on it.

## Verification

1. `python manage.py makemigrations --check --dry-run` — no unexpected model drift.
2. Full suite: `python manage.py test` — record the baseline count **before** touching code.
   Export the env first (`set -a; source .env; set +a`); nothing auto-loads it, and `.env`
   must hold the local-dev shape from `.env.example`, not the deployment shape.
3. Importer round trip against the **synthetic** fixtures:
   `python manage.py import_prod_data --data-dir <tmp>` then
   `python manage.py verify_prod_import --data-dir <tmp>` — expect a clean report with its
   accounting balanced. **Rev 2 correction (review note 12):** rev 1 said "183 addresses" here;
   183 is `PLAN-prod-import.md`'s budget for the **real export**, which the suite never reads.
   The verifier counts placed addresses dynamically and has no hard-coded total, so the
   assertion is "accounting balances and the count is unchanged from the pre-change fixture
   run", not a literal number.
4. Migration on an empty database (`migrate` from scratch) — the `RunPython` must be a no-op.
5. Admin smoke: create a DM7C-shaped assembly through `/admin/inventory/networkdevice/add/`;
   confirm two rows, two port sets, no `host` field on the form, and the companion's rack/slot
   read-only on its own change page; move the host down one slot and confirm both rows
   relocate with **every address unchanged**; delete the companion alone (refused with a
   message, not a 500); delete the host and confirm the confirmation page lists the companion.
6. `pre-commit run --all-files` (ruff check/format, mypy over `config inventory manage.py`).

## Review response

| Note | Resolution | Section |
|---|---|---|
| 1 — P1 occupancy check rejects the move in `clean()` before `save()` can park; pre-flight and post-insert disagree | **Accepted.** Verified: `_check_rack_slot_not_occupied()` (`:3119-3126`) excludes only `self.pk`, and `RackSlotAssignmentMixin.clean()` runs first. Added `_companion_pair_pks()` exclusion plus an explicit pair-target-range check, without which excluding the partner would let the pair overlap itself. | Pair-aware occupancy |
| 2 — P1 the move path validates the companion's addresses but not the host's | **Accepted.** Verified: `_validate_existing_addresses_still_fit()` is reachable only from `clean()` (`:1354`). Rev 1's claim was true on the admin path and false on a bare `save()`. The move path now calls it for both rows. | The move path |
| 3 — P1 `host` unenforced against the type graph, and editable on the add form | **Accepted.** Verified `Meta.exclude = []` at `admin.py:371`. Added `exclude = ["host"]` on both forms and three type-compatibility rules in `clean()` and `save()`. | Type compatibility; Admin |
| 4 — P1 a companion stays independently movable through the ORM | **Accepted.** Admin readonly is presentation. Added a model-level rack/slot lock for hosted devices with a `_host_managed_move` escape, on the `NetworkDevicePort._deriving_address` pattern. | Host-managed rack/slot |
| 5 — P1 `_move_companion()` wrong under `update_fields` | **Accepted.** Derived from effective post-save values via `_normalize_update_fields()`, and an early return when neither rack field is in `update_fields`. | The move path |
| 6 — P1 the `QuerySet.update()` park corrupts audit history | **Accepted.** Verified `settings.py:244` tracks `rack`/`rack_slot` on `NetworkDevice`. Park is now conditional (only on real collision) and is a real `save()` under the escape flag, so entries are truthful in both cases. The reviewer's aside that "`update()` avoids `clean()`" was a weak justification is correct — plain `save()` skips `clean()` too. | The move path (decision 2 amended) |
| 7 — P1 cycle validation raceable; naive locking deadlocks | **Accepted.** Both type rows acquired in one `_lock_type_rows()` call, which sorts ids, and validation re-reads from the database under the lock. | `NetworkDeviceType` |
| 8 — P1 admin companion deletion 500s on both routes | **Accepted.** Verified `AuditedModelAdminMixin` (`:47-100`) overrides only `changeform_view`/`save_model`/`save_formset`. Added `delete_view()` handling and the same in the `delete_selected` action, plus full HTTP tests for both. | Admin; Tests |
| 9 — P2 `DEVICE_TYPES[host_key]` indexes a tuple | **Accepted.** My error. Added an explicit `SPEC_BY_KEY` mapping with duplicate-key validation. | Importer |
| 10 — P2 migration verification underspecified; use `MigrationExecutor` | **Accepted in substance, prescription rejected.** The underspecification is real and six concrete cases are now named. The `MigrationExecutor`/historical-state idiom is **not** adopted: this repo's migration tests import the module and call the function with `real_apps` (`tests.py:3916-3937`), which does exercise the linking logic against real rows, and introducing a second migration-test idiom for one migration buys rigour the house style already gets more cheaply. | Tests |
| 11 — P2 several tests can still give false confidence; `bulk_create`/`update()` ungated | **Accepted.** Every named case added to the test list. The manager bypasses are **documented as unsupported** rather than guarded — consistent with `_check_locked_fields_unchanged()`'s existing docstring (`:180-196`) and with a suite that already uses those paths deliberately to reach guarded states. | Tests; Documented bypasses |
| 12 — P2 the synthetic fixture does not contain 183 addresses | **Accepted.** My error — 183 is `PLAN-prod-import.md`'s real-export budget and the suite never reads the real export. Verification step 3 restated as balanced accounting against the pre-change fixture run. | Verification |
| 13 — P2 `DESIGN.md` left stale | **Accepted.** Both relationships and their lifecycle semantics added to the data-model section. | Docs |
| Closing — `is_new` idiom correct; no recursion; `Collector` bypasses the queryset guard as intended; parked state cannot escape the transaction; `0011` is the right number; helpers all exist; `_make_device_type` starts at `:107` not `:86` | **Noted, no change** beyond correcting the `_make_device_type` citation. These confirm rev 1's reasoning rather than contradicting it. | Tests |

## Out of scope

- Issue #42 (collapsing the pair into one device with two same-VLAN Dante Primary ports).
- Any change to `slot_offset` semantics. Companions and offsets stay orthogonal; a type may
  use either, both, or neither.
- Optional accessories. A DM7-EX is two ordinary devices' worth of nothing new — optionality
  lives in ADR 0010 profile names, not in a companion link.
- Guarding `QuerySet.update()` / `bulk_create()` against companion-lifecycle violations; see
  "Documented bypasses".
