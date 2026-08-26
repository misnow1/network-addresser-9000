# Implement ADR 0027 — the ordinal is the unit

Revision 1. Written 2026-08-25 against `main` at d10207e, on branch `adr-0027`.

## Context

`docs/adr/0027-the-ordinal-is-the-unit.md` decides that a racked device's every static address is
derived — `range_base + rack_slot + slot_offset` — and that the rack slot is the only thing an
operator sets. This plan builds it.

### Roadmap placement

Not a numbered phase. It closes four issues that predate the current phase numbering (#84, #83, #62,
#28) and retires part of a shipped one (#60). Phase 20 (#27) is explicitly untouched — ADR 0027
decision 7 — and gets easier afterwards.

### The estate, measured

Against the live database via the app container (`network-addresser-9000-app-1`), not `.env`:

- 64 devices, all racked; 132 static racked device ports.
- **130 of 132** already satisfy `address == range_base + rack_slot + slot_offset`.
- The 2 exceptions are the only 2 `address_source=OPERATOR` type ports: Yamaha DM3 and DM7C
  "Device Control", VLAN 201.
- `bej-dm3-1` slot 15 → control ordinal 16 (**+1**, ordinal otherwise free).
- `mps-dm7c-1` slot 5 → control ordinal 4 (**−1**, the only negative offset in the estate).
- No ordinal is claimed twice anywhere.

Re-run `docs/plans/measure-adr-0027.py` (added by this plan) before PR 1 merges, to confirm the
estate has not drifted.

### The blast radius, counted

| Surface | `address_source` | `taken_by` axis | `slot_span` |
|---|---|---|---|
| `inventory/models.py` (5284 lines) | 13 | — | 15 |
| `inventory/views.py` (2258) | 1 | 9 | 10 |
| `inventory/admin.py` (2676) | 4 | — | 2 |
| `inventory/tests.py` | — | 1 | 1 |
| `inventory/test_ui.py` | 4 | 31 | 4 |
| `inventory/test_prod_import.py` | 10 | 1 | — |
| `templates/inventory/rack_detail.html` | — | 4 | — |
| `static/inventory/na9k.css` | — | 3 | — |

The `taken_by` column is the deletion; the other two are rewrites.

## Decisions this plan settles (the ADR left them to the build)

### 1. `address` stays a stored column, written by the system

Derived-at-read was considered and rejected. `unique_device_port_vlan_address_value`, the
cross-table static-address uniqueness checks, `_build_taken_address_map()`'s successor, the address
map view and `verify_prod_import` all query the column directly. Keeping it stored means the change
is "who may write this" — not a rewrite of every query in the project.

So: `address` remains a real column, remains `NOT NULL` for static ports (the existing
`CheckConstraint` at `models.py:4383-4384` stands), and becomes **system-written only**.

### 2. `_derive_offset_siblings()` collapses into one whole-device derivation

Today (ADR 0017, `models.py:5108-5181`) a `slot_offset > 0` port derives from the **offset-0 port on
its VLAN**, and editing the offset-0 address cascades to its siblings. Under ADR 0027 nothing
derives from another port — every port derives independently from `rack_slot + its own offset`.

That deletes the cascade, the `_derive_offset_siblings()` privileged-writer flag (`models.py:4803`)
and the sibling-disagreement validation (`:5181`). Replaced by a single `NetworkDevice`-level
`_derive_addresses()` that writes every static port's address from the device's slot. Strictly less
machinery than what it replaces.

### 3. Derivation runs in `NetworkDevice.save()`, inside the existing transaction

Alongside `_materialize_ports()` (`models.py:4150`), which already runs there and already rolls back
atomically on failure (the phase 10 precedent: static materialization refusing Switched-Mode-shaped
devices "atomically, with a clear error").

The admin add form keeps validating at `clean()` time as it does now — it has `rack`, `rack_slot` and
`device_type`, so it can predict every claimed ordinal and report per-field errors. The save-time
check is a **backstop** for programmatic and importer paths, not the operator's experience.

### 4. The suggester gains a sibling; `lowest_free_run()` is not touched

New in `suggestions.py`, pure, beside it:

```
lowest_free_placement(occupied: set[int], offsets: Iterable[int], slot_count: int) -> int | None
```

Lowest `N ≥ 1` such that `N + offset` is free for every declared offset, and every claimed ordinal
is within `1..slot_count`. `lowest_free_run()` stays for the switch path (`admin.py:1432`, span 1)
and remains correct at what it does.

`occupied_rack_slot_ranges()` (`models.py:378-392`) returns `(start, end)` tuples built from
`Coalesce(Max(slot_offset), 0) + 1`. It gains a set-returning sibling built from **distinct**
offsets. Same two bounded queries; no per-row aggregate.

### 5. Migration `0023` bypasses the type-port lock deliberately

ADR 0010 freezes `NetworkDeviceTypePort` fields once the type has instances
(`models.py:2983-2994`). Migration `0023` must rewrite the DM3 and DM7C "Device Control" type ports
from `address_source=OPERATOR, slot_offset=0` to `slot_offset=1`, and both types have instances.

Migrations operate on historical models and never call `save()`'s lock check, so the bypass is
structural rather than a flag — but it must be **stated in the migration's docstring**, because a
reader who knows ADR 0010 will otherwise read it as a bug.

Order within `0023`: rewrite the two type ports, re-derive both instance addresses, then drop
`address_source` and `PortAddressSource`. Assert 132/132 conformance before the drop and fail loudly
otherwise — the drop is irreversible and the assertion is the only thing standing between a drifted
estate and silent data loss.

### 6. Admin: address fields become read-only, not hidden

`NetworkDevicePort`'s `address` moves to `readonly_fields` on every inline and change form. Shown,
not hidden — the operator still needs to read it, and hiding it would make the derivation feel like
a disappearance. Help text explains that the rack slot sets it.

### 7. The move confirmation is an interstitial, not a JS `confirm()`

Django admin has no built-in "confirm this change" hook, but ADR 0007's removal flows already
establish an interstitial-template pattern in this codebase. Reuse it: on a `rack`/`rack_slot`
change that would alter any address, render old → new per port and require an explicit POST.

**Read-only UI is unaffected** — ADR 0020 decision 2 means the confirmation lives entirely in the
admin.

## Step 0 — done by hand, before PR 1

Mike, in the admin. Not a migration: it re-addresses live equipment once, and writing code for a
state that should not have existed is effort in the wrong place.

1. Park `mps-dm7c-1`'s Device Control address on a free CONSOLES ordinal (19–30 are all free) —
   `unique_device_port_vlan_address_value` refuses the intermediate duplicate otherwise.
2. Move `mps-dm7c-1` from slot 5 to slot 4. Its Dante Primary becomes `10.201.6.4`.
3. Set Device Control to `10.201.6.5`.
4. Re-address both interfaces on the hardware.

`mps-dm7ex-1` stays at ordinal 6 throughout. After this, both surviving `OPERATOR` ports are `+1`
and the estate is 132/132 conforming.

## PR 1 — derived addresses, and the retirement of `OPERATOR`

Closes **#83**, **#62**, **#84**.

- `models.py` — `_derive_addresses()` on `NetworkDevice`; delete `_derive_offset_siblings()` and its
  cascade; drop `address_source`/`PortAddressSource`; `slot_span` → a declared-offsets set;
  `occupied_rack_slot_ranges()` gains its set-returning sibling; the `.255` bound stays one-sided
  (`rack_slot + max(offset) <= slot_count`) since offsets remain positive.
- `suggestions.py` — add `lowest_free_placement()`.
- `admin.py` — device add form calls the new placement suggester; `address` becomes read-only;
  delete the operator-address prompt and `_validate_operator_addresses()`.
- `views.py` — occupancy from the offset set; `Occupant.span` becomes the claimed-ordinal set.
  `Occupant.bracketed` is left **knowingly wrong** for sparse claims — that is #93.
- `migrations/0023_derived_addresses.py` — per decision 5.
- Canonical docs — `CONTEXT.md`, `DESIGN.md`, `ROADMAP.md`.

## PR 2 — delete the `taken_by` axis

Closes the #60 residue.

`views.py` (9), `rack_detail.html` (4), `na9k.css` (3), `test_ui.py` (31), `tests.py` (1),
`test_prod_import.py` (1). Delete `_build_taken_address_map()`, `ElevationCell.taken_by`,
`taken-by-label`, `cell-taken`, `tag-address-taken` and `ElevationEncodingTests`' assertions for
them. Update `ElevationCell`'s docstring, which documents each encoding against the failure it
guards — the removal must be recorded there, not silently dropped.

Amend `PLAN-consumed-slot-addresses.md` with a superseding note. Its decision 1 and review note 6
were correct when taken; the model changed underneath them.

## PR 3 — recompute on move, with confirmation

Closes **#28**.

Recompute on `rack`/`rack_slot` change; the admin interstitial per decision 7. Kept separate from
PR 1 because PR 1 introduces no new operator-facing behaviour and PR 3 introduces a destructive one
— landing them together would be one PR that changes the model *and* adds a scary prompt, hard to
review and hard to roll back.

## Verification

- `set -a; source .env; set +a` then `python manage.py test inventory` — green.
- `docs/plans/measure-adr-0027.py` against the app container reports **132/132** conforming, both
  before `0023` drops the column and after PR 1 merges.
- `python manage.py verify_prod_import` still passes.
- A Shure receiver with offsets `{0, 64}` claims exactly 2 ordinals; ordinals `N+1..N+63` remain
  free and placeable (**#83**).
- `lowest_free_placement()` skips a claimed ordinal and returns a placement whose every offset is
  free (**#62**).
- The rack elevation shows no "+ add device" link on a claimed ordinal, with **no change to
  `add_url`'s own logic** (**#84**).
- A device port's `address` cannot be written through the admin.
- Moving a device recomputes every static address, and the interstitial lists old → new per port
  (**#28**).
- Migration `0023` refuses to run against a non-conforming estate.
- `QueryBudgetTests` unchanged: occupancy stays two bounded queries.
- Zero occurrences of `address_source`, `PortAddressSource`, `taken_by`, `_build_taken_address_map`
  or `_derive_offset_siblings` remain outside the superseded-plan notes.

## Risks and what could still be wrong

- **The type-port lock bypass (decision 5) is the highest-risk item.** If migration `0023` is wrong,
  two device types are left describing ports their instances do not have, and ADR 0010's lock means
  it cannot be corrected through the admin.
- **The conformance assertion is load-bearing.** Dropping `address_source` is irreversible; if the
  estate drifted between measurement and migration, an address becomes unrepresentable and is
  silently rewritten. Hence the pre-drop assertion, and hence step 0 by hand and first.
- **`slot_span` has 32 references across five files.** It changes meaning rather than disappearing,
  which is the shape of change most likely to leave a stale caller reading "one number" where a set
  is now correct.
- **The bracket stays wrong until #93.** Accepted and recorded in ADR 0027's Known gaps, but it will
  look like a bug to anyone who did not read the ADR.
- **`mps-sd9-1` / `mps-sd11-1`.** If the SD9's engine is confirmed while this is in flight, it needs
  CONSOLES ordinal 12, which `mps-sd11-1` holds, and PR 1 will refuse it — correctly, but
  disruptively. `PROD-DATA-ANALYSIS.md:292-295`.
