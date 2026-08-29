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

`mps-dm7ex-1` stays at ordinal 6 throughout.

**Done 2026-08-26 and verified.** `mps-dm7c-1` sits at slot 4 with Dante Primary `10.201.6.4` and
Device Control `10.201.6.5`; `mps-dm7ex-1` is undisturbed at 6; no ordinal is doubly claimed.

The conformance report still reads **130/132**, and that is the correct reading of a *completed*
step 0 — not a failure. `measure-adr-0027.py` compares each address's *implied* offset against its
port's **declared** `slot_offset`, and the two Device Control ports stay `declared=0` until
migration `0023` rewrites their type ports to `slot_offset=1`. What step 0 actually achieves is
narrower and is what the migration needs: **both implied offsets are now `+1`**, so there is a
single positive value for `0023` to write, and no ordinal collides once it does.

132/132 is reachable only *after* `0023`. An earlier revision of this plan claimed step 0 would
reach it, which was wrong.

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
- `docs/plans/measure-adr-0027.py` against the app container reports **PRE-MIGRATION GATE: GREEN**
  before `0023` runs — every `OPERATOR` port's implied offset a single non-negative value, and no
  doubly-claimed ordinal — and **132/132 conforming** after PR 1 merges. It reads 130/132 in
  between, by construction: declared offsets are what `0023` writes.
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

## Review response

PR 1 is implemented and staged as four green, individually committed steps (occupancy becomes a
set; retire `address_source`/`OPERATOR`; universal derivation and a read-only address; docs). Every
build decision (1-7) is implemented as written. Two places where I judged something the ADR/plan
left implicit, and one place I disagree with a consequence of decision 1 as written:

- **DHCP → static conversion after creation is now impossible, and this plan never says so.**
  Decision 1's "the address field stops being operator-editable... setting the rack slot is the
  only way to set an address" is unconditional in the ADR's own text, and I implemented it that way:
  `NetworkDevicePort._locked_fields()` locks `address` for every persisted row, full stop. That
  necessarily also locks the case ADR 0013 explicitly designed for — flipping an existing DHCP port
  to static by typing its first address (`test_device_port_dhcp_to_static_via_manual_address_is_
  refused`, `inventory/tests.py`). Materialization-time DHCP/static is untouched (that choice still
  works exactly as before); what's gone is *changing your mind afterward* without recreating the
  device. Neither the ADR nor the plan discusses this transition at all — decision 1 is framed
  entirely around a *static* port's address, and this is a side effect of applying it literally to
  every port regardless of its current addressing mode. I implemented the strict reading rather than
  inventing an auto-derive-on-DHCP-flip mechanism the ADR never asked for, but this is a real,
  user-visible regression from ADR 0013 that deserves a decision, not a silent side effect: either
  the ADR should say this is intended, or a follow-up needs to teach `save()` to derive the address
  itself when `is_dhcp` flips to `False` on an existing row (the same formula `_derive_addresses()`
  already computes, just triggered by a different edge than "device just created").

  **Resolved 2026-08-26, Mike's ruling: build the derive-on-flip mechanism.** Losing ADR 0013's
  conversion is a capability loss this ADR never argued for or measured, and it should not ride
  along with decision 1 as an unexamined side effect. Implemented: `NetworkDevicePort.
  _persisted_is_dhcp()` / `_derive_address_on_flip_to_static()` derive the address from the same
  formula when `is_dhcp` flips `True` → `False` on a persisted row, called from both `save()` (the
  backstop — the same reason `_check_locked_fields_unchanged()` itself runs there and not only in
  `clean()`) and `clean()`. `_locked_fields()`'s `"address"` entry is exempted from the comparison
  only for that specific, freshly-detected transition (read fresh from the database each time, not
  from a self-reported flag on `self`) — every other transition still locks it unconditionally. An
  unracked device is refused with the same message `clean()` already gives static addressing on an
  unracked device generally; nothing new to argue there. A derived address that collides or falls
  outside the rack's range fails via `_validate_static_address()`, inside the same
  `transaction.atomic()` block `save()` already wraps everything in, so a bad derivation aborts the
  whole save rather than persisting half-written. `test_device_port_dhcp_to_static_via_manual_
  address_is_refused` is renamed to `test_device_port_dhcp_to_static_flip_derives_the_address` and
  rewritten to assert the derivation — typing a *wrong* address on purpose, to prove it's
  overwritten rather than trusted, since the operator flips a toggle and never types one for real
  (the admin's own `address` field stays `disabled`). Its sibling in `MaterializedPortLockTests`
  (`..._still_refused_once_racked`) is likewise split into an unracked-refusal test and a
  racked-derivation test, since racking the device is now exactly what makes the flip succeed.
  ADR 0027 decision 1 now argues this explicitly rather than leaving it to this plan's aside.
- **The offset-0 delete guard (`NetworkDevicePortQuerySet.delete()` / `NetworkDevicePort.delete()`)
  is left untouched, deliberately, and neither the ADR nor the plan says whether it should be.** Its
  original reasoning — "an offset sibling derives its address from this row, so deleting it would
  strand that derivation" — is no longer literally true: every port now derives from the *device's*
  rack slot independently, not from a sibling port. But the guard still protects a real invariant
  once you set that stale reasoning aside: `_validate_device_type_port_profile()` requires every
  offset-carrying VLAN to also carry an offset-0 port at the *type* level, and nothing revalidates
  that shape on an *instance* after one port is deleted. Deleting an instance's offset-0 port while
  its offset sibling survives would leave a live device silently violating its own type's declared
  shape. I kept the guard rather than deleting it with the cascade, since removing it would open that
  gap; the delete-guard tests are untouched because their assertions still hold for this reason, not
  the original one. Worth an explicit ADR 0027 sentence either confirming this reasoning or
  overriding it.

  **The comment is now corrected to state this actual reasoning** —
  `NetworkDevicePortQuerySet.delete()`'s class docstring, `NetworkDevicePort.delete()`'s inline
  comment, and the two raised `ValidationError` messages (wording only; the guard's behaviour is
  unchanged). Whether ADR 0027 should formally confirm or override this reasoning is still open —
  not decided here, left for the reviewers to weigh in on as noted above.
- **Migration `0023`'s conformance assertion has no dedicated automated test.** The project's own
  convention (`RetireCompanionsMigrationTests`, `HostnameNormaliseMigrationTests`, and others in
  `inventory/tests.py`) is a `MigrationExecutor`-driven `TransactionTestCase` reconstructing the
  pre-migration schema and exercising the `RunPython` functions directly, including the failure
  path. I did not add one for `0023` — the plan's own Risks section calls the type-port lock bypass
  "the highest-risk item" in this PR, and its assertion is exactly what stands between a drifted
  estate and silent data loss on an irreversible column drop. This is a real gap against the
  project's own testing standard for exactly this class of migration, flagged here rather than left
  quietly uncovered.

  **Resolved: added.** `DerivedAddressesMigrationTests` (direct-function tests against the
  reconstructed `0022` schema, the `RetireCompanionsMigrationTests`/`HostnameNormaliseMigrationTests`
  shape) covers the happy path — a conforming console estate's operator-sourced type port and
  instance rewritten to `slot_offset=1` with the address re-derived from scratch (typed wrong on
  purpose, to prove it's recomputed rather than trusted), a DHCP-configured operator-sourced
  instance getting the offset without an address touch, and the two guard clauses inside the rewrite
  itself (unracked device, missing Rack VLAN Range) — and the failure path: an *ordinary*
  (non-operator) port's pre-existing drift, untouched by the rewrite step, reaches
  `_assert_full_conformance` unchanged and raises `RuntimeError` naming the divergent port.
  `DerivedAddressesMigrationExecutorTests` adds one real `MigrationExecutor` run (the
  `HostnameSlugMoveMigrationExecutorTests` shape) proving the failure path holds at the schema level
  too: a non-conforming estate raises before `RemoveField` runs, `0023` is never recorded as applied,
  and `address_source` survives on the table — then fixes the drift and confirms a normal forward
  migrate proceeds cleanly from that same database.
- **Minor, out of PR 1's explicit scope but worth a mention:** `templates/inventory/device_detail.
  html`'s "derived" tag (`{% if port.slot_offset > 0 %}`) now under-reports — every static port is
  derived under ADR 0027, not just offset>0 ones — but the read-only UI wasn't named in this PR's
  brief and I left it alone rather than guess at scope. `#93` (the bracket) and this are the same
  shape of leftover: display code that was correct under the old model and is now quietly wrong
  under the new one, without being what either ADR 0027 or this plan actually asked PR 1 to fix.

### Council fixes

An independent review council pass verified a further batch of findings against the code after the
above. Each verified finding is applied directly (no re-litigating); recorded here per this
project's convention of folding findings in rather than defending the original.

- **Migration `0023`'s `_assert_preconditions` gate had no test coverage at all**, despite being the
  load-bearing check (module docstring step 1): the one place that can catch two operator-sourced
  consoles whose instances imply *different* offsets, before the rewrite has already declared one
  `slot_offset` for the type and made `implied == declared` trivially true for both regardless of
  which was right. Added: a `DerivedAddressesMigrationExecutorTests` real-`MigrationExecutor` test
  reproducing exactly that (`test_forward_migration_raises_on_inconsistent_operator_offsets_before_
  any_rewrite`) — its cleanup reconciles the estate's drift before re-migrating rather than trying to
  re-migrate a database the new gate would refuse a second time. Plus six direct-function tests
  against `_assert_preconditions` itself in `DerivedAddressesMigrationTests` (the fresh-database
  no-op, a consistent conforming estate passing, inconsistent offsets across two consoles, a negative
  implied offset, an unracked operator-sourced device, a missing Rack VLAN Range, and — the one
  invariant 2 exists for — a post-rewrite ordinal collision with an unrelated device already racked
  at the ordinal the rewrite is about to claim).
- **A forged `NetworkDevicePort.slot_offset` survives the DHCP-to-static flip's own locked-field
  check.** `save()`'s flip carve-out deletes only `"address"` from `_locked_fields()` before running
  `_check_locked_fields_unchanged()`; a `save(update_fields=["is_dhcp", "address"])` then intersects
  none of the *remaining* locked keys (`slot_offset` included), so the check short-circuits without
  ever comparing it. `_derive_address_on_flip_to_static()` then derived from `self.slot_offset` — the
  same untrusted-`self` shape `_persisted_delete_guard_fields()` already exists to defend against on
  the delete path — so a caller that forges `slot_offset` in memory before the flip gets a silently
  wrong, permanently-locked address written under a `slot_offset` that itself stays correctly
  unchanged. Fixed: the derivation now reads persisted `slot_offset`/`vlan_id` fresh (one query),
  mirroring `_persisted_delete_guard_fields()`'s own reasoning, so a forged in-memory value can no
  longer reach the formula at all. Proven by
  `test_flip_to_static_derives_from_persisted_slot_offset_not_forged_self`
  (`SlotOffsetAddressingTests`).
- **The same flip, given a narrow `update_fields`, could derive/clear `self.address` in memory and
  then never write it** — `save(update_fields=["is_dhcp"])` alone leaves the actually-persisted
  `address` unchanged on both sides of the boundary, tripping the bare
  `device_port_dhcp_xor_static_address` CHECK regardless of which direction the flip runs. Fixed:
  `save()` now widens `update_fields` to include `"address"` whenever a flip is happening and it
  isn't already present. Checked stage 8's static→DHCP carve-out for the identical hole and found it
  shares the same fix, since both directions go through the same `if flipping_to_static or
  flipping_to_dhcp` branch. Proven by `test_flip_to_static_with_narrow_update_fields_still_persists_
  address` and `test_flip_to_dhcp_with_narrow_update_fields_still_clears_address`
  (`SlotOffsetAddressingTests`).
- **The rack elevation's occupancy map claimed a contiguous `range(span)` for every device, not the
  sparse ordinal set ADR 0027 decision 2 actually defines.** `_build_occupancy` derived each device's
  occupied ordinals from `resolve_slot_spans`'s `span` (`max(offset) + 1`), the same number
  `NetworkDeviceType.slot_span`'s own docstring warns is "the highest ordinal reached," not the full
  set — a type declaring offsets `{0, 64}` has `span=65` but `claimed_offsets={0, 64}`. The write
  path (`NetworkDevice._check_rack_slot_not_occupied`, `suggestions.lowest_free_placement`) already
  used the sparse set and correctly allows a second device on any of the 63 in-between ordinals; the
  read side then saw two occupants claiming that same ordinal and rendered `state="conflict"` — the
  encoding ADR 0027 decision 5 reserves for a real, bypassed-write violation, not this map's own
  arithmetic. A legal placement read as data corruption, and every one of those in-between ordinals
  lost its `add_url` though genuinely free. Fixed: a new `resolve_claimed_offsets()` (the
  `resolve_slot_spans` bulk-query shape, for the full offset set rather than its maximum) feeds
  `_build_occupancy` the sparse set directly; `spans` still flows to `_device_row` unchanged, since
  `Occupant.span`/`bracketed` stay a deliberate, documented approximation for a sparse claim (issue
  #93 — left alone, per the finding's own instruction not to touch it). Adds one flat query to the
  view's budget, so the two `QueryBudgetTests`/`HostnameDivergesMarkerTests` absolute-count
  assertions move from 12 to 13 (still independent of device/rack size — the property those tests
  actually guard). Proven by `SparseClaimedOffsetsOccupancyTests` (`test_ui.py`): a `{0, 64}` device
  plus an in-between device at ordinal 2 — every ordinal both devices actually claim has
  `conflicts == []` and the correct occupant, and every genuinely free ordinal in between has
  `conflicts == []`, `occupant is None`, and an `add_url`.
