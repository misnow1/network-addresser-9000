> **Revision 2** — incorporates review notes from `REVIEW-1-PLAN-adr-0017.md`.
> See "Review response" for the mapping.

# Implement ADR 0017 — derived same-VLAN addresses

## Context

`ROADMAP.md` phase 12 has one unchecked implementation item, and it is the last
thing standing between the current code and the production import: **implement
ADR 0017**. Everything else in the phase (validation, ADR 0015's `/27` floor,
ADR 0016's switch materialization) has shipped.

The problem ADR 0017 solves: a DiGiCo SD12 occupies two addresses on the control
VLAN. The first is operator-set; the second is the console's audio engine, always
control + 1, assigned by the console software and not editable on the hardware.
The current model refuses that shape outright —
`NetworkDevice._check_static_materialization_possible()` (`inventory/models.py:2823`)
rejects any two addressable type ports on one VLAN, because it was written for
ADR 0013's Switched Mode, where two bridged jacks share *one* address. That
refusal is right for Switched Mode and wrong for a console. The production
spreadsheet worked around it by splitting each console into two rows in two slots,
which produces correct addresses and records none of the facts worth keeping.

Outcome: `NetworkDeviceTypePort` gains `slot_offset`, a port's suggested address
becomes `range_base + rack_slot + slot_offset`, a device occupies the ordinal
*range* its type's max offset implies, and the same-VLAN refusal narrows from
"same VLAN" to "same VLAN **and** same offset" — so Switched Mode is still
refused, by the more precise rule.

This is mechanism only. No concrete SD12/SD7 type is defined here; ADR 0017 notes
the SD7's engine count still needs hardware confirmation, and types are data
created during the import, not code.

## Decisions this plan settles (ADR 0017 left both open)

**`slot_span` is computed, not stored.** A property on `NetworkDeviceType`
(`max(slot_offset) + 1`), with the cross-occupant check annotating
`Max("device_type__type_ports__slot_offset")` over the join. Still a single query,
so the query-cost argument for denormalizing is a wash, and there is no second
copy of a fact the type already owns.

**Flipping an offset-0 port to DHCP cascades to its offset siblings.** They go
DHCP too (`address=None`); flipping back to static re-derives them. Same reasoning
that made the engine address derived rather than merely locked — the alternative
leaves an engine address derived from a control address that no longer exists.

**Span is unconditional, not addressing-dependent.** A DHCP-materialized console
still spans its type's ordinals. `is_dhcp` is editable per port after creation, so
a span that depended on it would drift; ADR 0017's stability argument rests on
span being a property of the immutable type.

## Review response

| Note | Resolution | Section |
|---|---|---|
| 1 [P1] Sibling recompute can't pass locked-field enforcement; `update_fields` does not "clear" the lock — it *enforces* it when the locked field is named, and `clean()` checks with `update_fields=None` regardless | **Accepted — rev 1 had this exactly backwards.** Verified in `_check_locked_fields_unchanged()` (`:172-180`): it returns early *only* when no locked key appears in `update_fields`. Replaced with an explicit privileged writer: a transient `_deriving_address` flag that `_locked_fields()` consults, set only by the one method allowed to write a derived address | 5 |
| 2 [P2] Detecting the control-port change "after `super().save()`" is unsafe — persisted old values are gone by then, and an in-memory change excluded by `update_fields` would wrongly cascade | Accepted. Snapshot persisted `address`/`is_dhcp` *before* `super().save()`, and gate the cascade on normalized `update_fields` actually including one of them | 5 |
| 3 [P1] The `.255` bound stays bypassable — `clean()` isn't called by `save()`/`objects.create()`, and `_validate_static_address()` only checks containment, which `.255` passes | Accepted, scoped precisely. The bound is added to `_check_static_materialization_possible()`, which **does** run on the `objects.create()` path from `_materialize_ports()`. Deliberately *not* adding a new global save-time `slot_count` rule for all racked equipment: that is a pre-existing clean()-time-only gap (unchanged since the mixin was written), it applies equally to switches, and closing it repo-wide is scope this plan doesn't own. DHCP materialization stores no address, so it carries no `.255` risk; its span-occupancy exposure is the gap ADR 0017 already documents as known | 3, 6 |
| 4 [P1] The "non-zero offset requires an offset-0 port" invariant isn't type-level where placed — `_check_static_materialization_possible()` runs only for racked+static and skips L2-only VLANs, so DHCP/unracked devices materialize orphaned offset ports | Accepted. Moved into `_validate_device_type_port_profile()`, which already runs unconditionally from both `NetworkDevice.clean()` and `_materialize_ports()`, and which is the existing home for structural type-shape checks. It ignores the L2-only/addressable distinction, as a structural invariant should | 3 |
| 5 [P2] The numbered sections have unsafe intermediate states if landed in order | Accepted as a clarification: the sections describe one change, not a commit sequence. Stated explicitly below — schema, derivation, copying and bounds land together in a single commit | Changes preamble |
| 6 [P1] The switch-side overlap test is wrong as written — a plain `rack_slot__range` lookup can't find a device starting at 7 that spans through a switch at 8 | Accepted; rev 1 was wrong. Both sides use the annotated device-end query. Only the *device* table needs the aggregate (switches always span 1), but the switch side must still query it | 6 |
| 7 [P2] Verification gaps: no rollback test, no `update_fields` test, no move test, no admin test; and `RackAddressingProductionReplayTests` can't signal offset-0 regressions | Accepted, and the last point corrects a claim rev 1 leaned on. Verified: that class (`tests.py:4293-4310`) creates only VLANs, racks and ranges — never a device type, device, or port. It guards rack-base arithmetic, which this change doesn't touch. The real offset-0 regression signal is `StaticPortAddressingTests` / `PortProfileMaterializationTests`. Test list expanded to 14 | Verification |
| 8 [P2] `DESIGN.md` and `CONTEXT.md` go stale — they omit `slot_offset`, state every port's address is independently editable, and define addressing as base + slot | Accepted. Both are canonical model docs this repo keeps current alongside code | 9 |
| 9 [P3] `source .env` doesn't export to Python; `makemigrations --check` should report *no* changes once 0010 exists; the query snippet needs imports | Accepted. Verified `models.py` imports neither `F`, `Max` nor `Coalesce` — use `models.F`/`models.Max` per house style (`:2722`) plus `from django.db.models.functions import Coalesce` | Verification, 6 |

Also folded in without a numbered note: the `NetworkSwitchPortForm` citation is
corrected to `inventory/admin.py:338-341` (the `__init__` that sets
`disabled=True`); rev 1's `:320` pointed inside the docstring.

## Changes

**These land as one commit.** The sections below describe a single coherent change,
not a landing order — note 5 is right that the tree has incoherent intermediate
states if they are staged separately (narrowing the same-VLAN refusal before the
instance offset is copied and used would let a type through whose ports then
suggest the same address twice).

### 1. Schema — `inventory/models.py`, new migration `0010_slot_offsets.py`

- `NetworkDeviceTypePort.slot_offset` — `PositiveIntegerField(default=0)`, with
  help text in the operator language the other fields use (see migration `0008`),
  e.g. *"Address offset from the device's slot. Leave at 0 unless the hardware
  itself derives this port's address from another port's (e.g. a console engine
  at control + 1)."*
- `NetworkDevicePort.slot_offset` — same field, **copied at materialization**, not
  read live through `source_type_port` (that FK is `SET_NULL`; copying matches
  ADR 0010's seed-once pattern for every other identity field on the row).
- No new DB constraint on `(device_type, vlan, slot_offset)` — two type ports at
  the same VLAN *and* offset is legal for a DHCP or Switched-Mode type; only
  static materialization refuses it, in the pre-flight below.

### 2. Address arithmetic

`suggestions.suggest_slot_address()` is unchanged — ADR 0017 introduces no new
arithmetic. Add `slot_offset: int = 0` to `_suggest_rack_slot_address()`
(`inventory/models.py:247`) and pass `rack_slot + slot_offset` through. Existing
callers (`NetworkSwitchAddress`, offset-0 device ports) keep today's behaviour by
taking the default — switches have no port model and always span one.

### 3. Type-shape invariants

**In `_validate_device_type_port_profile()` (`:233`)** — the existing home for
structural type checks, called unconditionally from both `NetworkDevice.clean()`
and `_materialize_ports()`, on every addressing path:

- A VLAN carrying any non-zero-offset type port must also carry an offset-0 type
  port on that VLAN. Refuse with a message naming the VLAN. Per note 4 this must
  **not** live in the static-only pre-flight, or a DHCP or unracked device
  materializes offset rows with nothing to derive from — rows that could never
  correctly be made static later.

**In `_check_static_materialization_possible()` (`:2823`)** — static-only, runs
from both `clean()` and `_materialize_ports()`:

- Group addressable type ports by `(vlan_id, slot_offset)` instead of `vlan_id`,
  and refuse any group with more than one member, keeping today's error text (it
  still describes exactly the Switched Mode case it now catches alone).
- Suggest and `_validate_static_address()` per type port at that port's own offset.
- **Add the `rack_slot + max(slot_offset) <= rack.slot_count` bound here as well
  as in `clean()`** (note 3). This pre-flight runs on the `objects.create()` path,
  which never calls `clean()`; without it, a device created directly at slot 30 of
  a rack with a `.224`-aligned `/27` still materializes the `x.255` address ADR
  0017 exists in part to prevent, because `_validate_static_address()` only checks
  containment and `.255` is inside both the block and the VLAN subnet.

### 4. Materialization — `NetworkDevice._materialize_ports()` (`:2877`)

Copy `slot_offset` onto each `NetworkDevicePort`. Order the loop by `ordinal` as it
does today; because each port's address is computed from its own offset
independently, no offset-0-first ordering is required at creation.

### 5. Per-port address derivation — `NetworkDevicePort`

- `clean()` (`:3049`): pass `self.slot_offset` into `_suggest_rack_slot_address()`
  for the unsaved-port suggestion.
- `_locked_fields()` (`:3082`): always add `slot_offset`; add `address` **only
  when `slot_offset > 0` and the privileged flag below is not set**. An offset-0
  port keeps `address` editable exactly as ADR 0003 requires.
- **The privileged writer** (note 1). Rev 1 claimed `save(update_fields=["address"])`
  would clear the lock; it does the opposite. So the derived write needs an
  explicit, documented exemption — a transient instance attribute:

  ```python
  #: Set only by _derive_offset_siblings() while it writes a derived
  #: address, and consulted by _locked_fields(). This is the single
  #: legitimate writer of an offset port's locked address; nothing else
  #: may set it. Never a field, never persisted.
  _deriving_address: bool = False
  ```

- **The cascade**, in `save()` inside the existing `transaction.atomic()` block:
  1. *Before* `super().save()`, snapshot the persisted `address`/`is_dhcp` for this
     pk (note 2) — after the write they are unrecoverable.
  2. After `super().save()`, cascade only when `slot_offset == 0`, the snapshot
     differs from what was just written, **and** normalized `update_fields` is
     `None` or actually contains `address`/`is_dhcp` — so
     `save(update_fields=["switch_port"])` never cascades a stale in-memory address.
  3. For each sibling with the same `(device, vlan)` and `slot_offset > 0`: set
     `_deriving_address = True`, then either derive
     `IPv4Address(control.address) + sibling.slot_offset`, or cascade to
     `is_dhcp=True, address=None` when the control port went DHCP. Each sibling
     goes through `full_clean()` + `save()`, so a derived address that collides or
     falls outside the rack's range raises and rolls back the control edit with it.

### 6. Span occupancy and the `.255` bound — `RackSlotAssignmentMixin` (`:1240`)

- Give the mixin a `slot_span` property returning `1`. `NetworkSwitch` inherits it
  unchanged; `NetworkDevice` overrides it to delegate to its type, defensively via
  `_get_related(self, "device_type")` so an unsaved device without a type still
  cleans.
- In `clean()`, replace `rack_slot > slot_count` with
  `rack_slot + slot_span - 1 > slot_count`, naming the span in the error. This is
  the bound ADR 0017 calls the only thing standing between offsets and a
  block-relative broadcast address; §3 adds the same bound on the
  materialization path, which `clean()` does not cover.
- `_check_rack_slot_not_occupied()` on **both** models becomes a range-overlap
  query against the annotated device ends (note 6 — a plain `rack_slot__range`
  test on the switch side cannot see a device that starts at 7 and spans through
  a switch at 8):

  ```python
  # imports: models.F / models.Max per house style (:2722), plus
  # from django.db.models.functions import Coalesce
  NetworkDevice.objects.filter(rack=self.rack, rack_slot__lte=my_end)
      .exclude(pk=self.pk)          # devices only; switches have no self to exclude
      .annotate(span=Coalesce(models.Max("device_type__type_ports__slot_offset"), 0) + 1)
      .annotate(occ_end=models.F("rack_slot") + models.F("span") - 1)
      .filter(occ_end__gte=my_start)
  ```

  `NetworkDevice` must now also check other **devices** — `unique(rack, rack_slot)`
  only catches equal starting ordinals. Switches span 1, so the device side's
  switch query stays a membership test of `switch.rack_slot` within the device's
  span.

### 7. Docstring correction — `RackSlotAssignmentMixin` (`:1251`)

Its docstring defers the rack-occupancy fix to *"phase 3's 'Overlap validation'
work (see ROADMAP.md)"*, which is checked off and shipped something else
(rack-range-vs-range, not slot occupancy). ADR 0017 asks the implementer to either
re-file the gap or fix the pointer. **It is already re-filed** — `ROADMAP.md:113`
carries it under "Later / not yet designed" — so only the stale pointer needs
correcting: point it at that entry. Spans make this acknowledged weakness matter
more without changing its shape; do not attempt to close it here.

### 8. Admin — `inventory/admin.py`

- `NetworkDeviceTypePortInline.fields` (`:516`): add `slot_offset`.
- `NetworkDevicePortInline` (`:579`): show `slot_offset` read-only, and render
  `address` read-only on offset rows. `InlineModelAdmin.get_readonly_fields()`
  can't vary per row, so use a form with `disabled=True` on `address` when
  `instance.slot_offset > 0` — the pattern `NetworkSwitchPortForm.__init__`
  (`:338-341`) already established for exactly this problem, including its note on
  why `disabled=True` beats omitting the field.

### 9. Documentation — `DESIGN.md`, `CONTEXT.md`, `ROADMAP.md`

Per note 8, these are canonical and currently contradict the change:

- `DESIGN.md:114-125` — device-port addressing is no longer solely base + slot;
  record the offset and that an offset port's address is derived and read-only.
- `DESIGN.md:267` — the claim that every port's address/DHCP setting is
  independently editable is now false for offset ports.
- `CONTEXT.md:35-37` — a device occupies an ordinal *range*, not a single slot,
  and `slot_offset` is copied onto the instance port.
- `ROADMAP.md` — tick "Implement ADR 0017". Phase 12's remaining item is then the
  production import alone (`PLAN-prod-import.md`), still blocked on its own three
  mappings.

If implementation contradicts any prediction in ADR 0017's `## Follow-up`, correct
it visibly on the page rather than silently — ADR 0015 keeps its own wrong
prediction there on purpose.

## Verification

`inventory/tests.py` — add `SlotOffsetAddressingTests` near
`StaticPortAddressingTests` (`:1845`), reusing its rack/VLAN/type fixtures.
ADR 0017's six required cases, plus eight the review showed were missing:

1. SD12-shaped type (Control offset 0, Engine offset 1, one VLAN) materializes
   `base + slot` and `base + slot + 1`.
2. The offset port's `address` rejected as read-only after creation.
3. The offset port recomputed when the offset-0 address is edited.
4. The DHCP cascade both ways: Control → DHCP takes Engine with it; Control back
   to static re-derives Engine.
5. **Rollback** — a derived address that collides with an existing address, or
   falls outside the rack's range, rolls back the control edit too, leaving both
   ports at their prior values (note 7).
6. **`update_fields` discipline** — `save(update_fields=["switch_port"])` with a
   dirty in-memory address does *not* cascade (note 2).
7. A second device refused at an ordinal inside an existing device's span —
   **both directions**: a new spanning device over an existing switch, and a new
   switch inside an existing device's span (note 6).
8. Span-query edge cases: a device excludes itself on re-save; a type with zero
   type ports yields span 1; unracked devices are ignored.
9. `rack_slot + max(slot_offset) > slot_count` refused via `full_clean()` — on a
   `.224`-aligned `/27`, where the failure mode is a `.255` address.
10. The same case refused via **direct `objects.create()`**, which bypasses
    `clean()` entirely (note 3) — this is the test that proves the bound is real
    rather than form-only.
11. Switched Mode still refused (same VLAN, **same** offset), proving the
    pre-flight narrowed rather than lost that case.
12. A VLAN with an offset-1 port and no offset-0 port refused — including on the
    **DHCP and unracked paths**, which skip the static pre-flight (note 4).
13. A rack/slot move leaves both stored addresses unchanged, and is blocked when
    they would no longer fit — ADR 0017 is explicit that nothing recomputes on a
    move (note 7).
14. Admin: the offset row's `address` widget is disabled, and a POST that tries to
    smuggle a value past it is ignored.

Existing coverage that must not move: `StaticPortAddressingTests`,
`PortProfileMaterializationTests`, `MaterializedPortLockTests` — these, not
`RackAddressingProductionReplayTests`, are the offset-0 regression signal
(note 7). Every existing type port takes `slot_offset=0`, and ADR 0017 promises
current addressing is bit-for-bit unchanged.

```bash
set -a; source .env; set +a     # a bare `source` doesn't export to the child python
python manage.py test inventory  # record the baseline count BEFORE touching code
python manage.py makemigrations --check --dry-run   # no changes, once 0010 exists
pre-commit run --all-files
```

`.env` must hold the local-dev shape from `.env.example` (`DJANGO_DEBUG=true`,
`DB_PASSWORD=na9000dev`, `DB_HOST=127.0.0.1`), not the deployment shape.

## Process

Per `CLAUDE.md`, this runs through `/plan-cycle`: independent `codex` review of the
plan (done — `REVIEW-1-PLAN-adr-0017.md`, folded in above) → implement on a Sonnet
subagent → independent `codex` review of the code → fix or argue back → commit on
branch `adr-0017`. The orchestrating session stays on Opus.
