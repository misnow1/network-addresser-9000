# The ordinal is the unit: a device's addresses are derived from its rack slot, not stored

ADR 0019 made the rack the address pool: an ordinal produces an address, `range_base + rack_slot`.
This ADR finishes that sentence. Every static address a racked device holds is **derived** from its
rack slot and its type's declared offsets, and nothing else. The rack slot is the only thing an
operator sets.

That is already how the production spreadsheet works, and it is already what
`docs/plans/PLAN-prod-import.md` §9 decided when it chose to re-address the DMI-DANTE cards on the
hardware rather than hand-override the imported values — **"so ordinal and address stay in
agreement."** The principle has been operative in this project's practice, its import plan and its
own ADR 0019 for months. Only the code disagreed, and four open issues are the bill for that
disagreement.

## The incoherence, and its four symptoms

`ADR 0003` stores a device port's address as an ordinary editable field. `ADR 0019` says the ordinal
determines the address. Both cannot be true, and the gap between them is a reachable state: **an
address that disagrees with its ordinal.**

Every one of the following is that same state seen from a different angle.

- **#84** — the rack elevation offers "+ add device" on an ordinal whose address is already held.
  The slot is empty; the address is gone; the system offers a value it will then refuse.
- **#62** — `lowest_free_run()` proposes exactly that ordinal, because occupancy is computed from
  `rack_slot` alone and knows nothing about addresses. Worth being precise about how bad this is:
  there is no `get_changeform_initial_data` for `rack_slot` anywhere in `admin.py`. The suggestion
  is applied **inside `clean()`** (`admin.py:1213`, `:1432`) as a blank-field fill, so the operator
  never sees it. They leave the field empty and receive a validation error on a value they never
  chose.
- **#83** — `slot_span` is `max(slot_offset) + 1`, a *contiguous* span. A Shure receiver whose
  control address sits at offset 64 therefore claims 65 consecutive ordinals and blocks 64 of them
  in the write path, not merely in the display.
- **#28** — a slot move leaves the old address behind, because nothing recomputes.

`#60` was an attempt to live with the state rather than remove it: it rendered "this slot is empty,
but its address is held" as a visible marker. It shipped on 2026-08-16 and this ADR deletes it. That
is not waste; it is the diagnosis that produced this decision.

## What the estate actually says

Measured against the live database, not inferred:

- **130 of 132** static racked device ports already satisfy `address == range_base + rack_slot +
  slot_offset` exactly.
- The **two** that do not are the only two `address_source=OPERATOR` type ports in existence — the
  Yamaha DM3 and DM7C "Device Control" ports on VLAN 201.
- `bej-dm3-1` sits at slot 15 with control on ordinal 16: offset **+1**.
- `mps-dm7c-1` sits at slot 5 with control on ordinal 4: offset **−1**, the only negative offset
  anywhere.
- **No ordinal in the estate is claimed twice.**

So this ADR does not impose a new discipline on the data. The data already keeps it. What changes is
that the code starts keeping it too, and one console gets moved.

## Decision

### 1. A static address is derived, never stored-and-edited

`range_base + rack_slot + slot_offset`, for the `RackVlanRange` covering that port's VLAN. The
address field stops being operator-editable. Setting the rack slot is the only way to set an
address on a port materialized static from the start.

This is not a new mechanism. It is **ADR 0017's derived-offset addressing, applied universally**
instead of carved out for DiGiCo consoles. ADR 0017 already made non-zero-offset addresses read-only
and recomputed; this extends the same treatment to offset 0.

There is one other edge, and it is deliberate rather than an oversight: ADR 0013 lets an operator
flip an existing DHCP port to static by typing its first address. Read literally, "the address field
stops being operator-editable" would refuse that transition outright — the strictest reading of this
decision, and the one PR 1 shipped with first. But that reading throws away a capability this ADR
never argued for and never measured the cost of; it is a side effect of applying decision 1 to every
port regardless of its current addressing mode, not a conclusion this ADR reached on purpose. Losing
ADR 0013's conversion was never on the table here, so it does not get to disappear as a side effect
of a decision about something else. The fix keeps both decisions intact rather than picking one:
when `is_dhcp` moves from `True` to `False` on a persisted port, `save()` derives the address itself
from the same `range_base + rack_slot + slot_offset` formula, exactly as if the port had been
materialized static to begin with. The operator still never *types* an address — flipping the toggle
is the trigger, the same way setting the rack slot is the trigger for a new port; nothing about "the
system writes this field" changes. An unracked device has no rack slot to derive from, so the flip
is refused for exactly the reason materialization already refuses static addressing for an unracked
device — there is nothing new to argue there, only the existing rule reapplied at a different edge.
The reverse direction (static to DHCP) needs the exact same treatment, not none: applying decision 1
literally locks `address` unconditionally on every persisted row, which makes this transition dead
both ways on its own — clear the address yourself and the lock rejects the edit; leave it and the
DHCP/static check on `clean()` rejects a DHCP port carrying a static address. `save()`/`clean()`
detect the flip the same way as the forward direction (the persisted `is_dhcp` read fresh, `True` ->
`False` there, `False` -> `True` here) and clear `address` themselves, exempting it from the lock for
that transition only. Nothing is derived on this side — there's no formula to run, only the address
to let go of.

Expressiveness is not lost. `_address_containment_error()` already requires every racked static
address to sit inside the rack's assigned range, so every address in the system is *already*
`range_base + k` for some `k`. Deriving it constrains **how you say it**, not **which addresses are
reachable**.

### 2. A device claims a set of ordinals, not a contiguous span

`{rack_slot + offset}` for each offset its type declares. A device with offsets `{0, 64}` claims two
ordinals and is indifferent to the 63 between them.

`slot_span` as "one number, `max + 1`" is retired. `lowest_free_run()` is **kept unchanged** and
remains correct for the switch path (`admin.py:1432`, span 1) and anything else genuinely about
runs; the device path stops being a caller and gains a sibling that asks the right question — *the
lowest `N` such that `N + offset` is free for every declared offset*.

This closes #83. The elevation's **bracket** encoding (`Occupant.bracketed`, `views.py:267-269`)
becomes wrong for a sparse claim, since a bracket asserts contiguity. That is deliberately deferred
to **#93**, to be taken with the other rack-cell work (#82 and the device-model-description pass)
rather than designing that cell three times.

### 3. `address_source` and `OPERATOR` are retired entirely

ADR 0022 decision 2 introduced `SLOT`/`OPERATOR` so one device could carry two independent static
addresses on one VLAN, closing #42. Decision 3 required the operator to supply the address at
creation.

Both go, and the field is dropped. #42's need survives and is met better: a device carries two ports
on one VLAN at **different offsets**, which the one-port-per-`(vlan, slot_offset)` rule already
permits and which ADR 0017's requirement of an offset-0 port on the same VLAN is already satisfied
by (a console's built-in Dante Primary).

The estate supports this: after `mps-dm7c-1` moves to slot 4, both remaining `OPERATOR` ports become
ordinary `slot_offset=1` ports and the field has zero users.

The rest of ADR 0022 is untouched — `is_add_in_card`, the `host` FK, `hostname_suffix`, and the
add-in-card tier table all stand.

### 4. `slot_offset` stays a `PositiveIntegerField`

The alternative was signing it to express `mps-dm7c-1`'s −1. Rejected in favour of moving the
console: it is one unit, and a model in which every offset is positive is materially easier to reason
about than one where offsets run both ways. The move is `mps-dm7c-1` slot 5 → slot 4, with its Dante
Primary and Device Control addresses swapping (`.6.5` ↔ `.6.4`), leaving `mps-dm7ex-1` at ordinal 6
undisturbed.

Done by hand, before the model change lands, rather than as a data migration. The swap needs a
parking address because `unique_device_port_vlan_address_value` refuses the intermediate duplicate;
CONSOLES ordinals 19–30 are free.

### 5. Strict enforcement, and no divergence report

A write that leaves a violation is refused, including a pre-existing one. There is no "no worse than
before" carve-out.

This costs nothing today because the estate has no violations, and coding around a case that should
not exist is effort spent on the wrong thing. A violation is reachable only through a bypassed write
(a bare `.save()`), and the elevation **already renders exactly that**: `ElevationCell(state=
"conflict")` and `ElevationRow.conflicts`, built for "more than one occupant claims this ordinal"
(`views.py:553-563`). Under this ADR a double-claimed ordinal *is* that, so violations flow into
machinery that exists. No new report, no `slot_addresses_diverge` property.

This is where the project's **"report, don't enforce"** habit (phase 19, ADR 0025's
`range_offsets_diverge`, ADR 0023's `hostname_diverges`) does **not** apply, and the distinction is
worth stating because it will be raised. That habit is about *pre-existing reality the system
inherited* — do not strand an operator over data that was true before the system had an opinion. It
has never been a blanket prohibition on constraints, and it says nothing about a write the system is
about to make itself.

### 6. A move recomputes addresses, behind a confirmation

Changing a device's rack or slot recomputes every derived address. ADR 0003 and ADR 0017 both state
that nothing recomputes on a move; this reverses that, and closes #28 by making the stale state
unrepresentable rather than by reporting it.

The hazard is real: a move becomes an instruction to reconfigure hardware, and there is no undo. So a
move that would change any address is **confirm-first**, showing old → new per port, in the shape
ADR 0007 established for removal. The operator stays in control, which is what ADR 0003 actually
cared about — it is the *stored divergence*, not the operator's authority, that this ADR removes.

### 7. What this is not

- **Not phase 20 (#27).** That is *how many ports share one address*; this is *where an address comes
  from*. Separable, and phase 20 is easier afterwards because the offset becomes the address's
  identity. Phase 20 stays where it is in the ROADMAP.
- **Not the jack-versus-interface problem** (`docs/Virtual Network Ports.md`). A computer with several
  tagged VLANs on one NIC is still `base + k` per address and is fully expressible here; what it
  breaks is `NetworkDevicePort`'s one-jack-one-address-one-VLAN assumption and `switch_port`'s
  `OneToOneField`. Independent, still deferred, and explicitly *not* an argument against this ADR.

## Rejected alternatives

**Keep `OPERATOR` as an escape hatch.** Cheapest today and preserves the exception that generates
#84, #62, #60 and #28. The estate says the hatch has one user, and that user is a console that can be
moved. Keeping a general mechanism for a single case that a hardware move dissolves is the trade this
project has declined before (ADR 0018's companion machinery, retired by ADR 0022 for the same
reason).

**Sign `slot_offset`.** Avoids re-addressing one console at the cost of a two-sided bound
(`rack_slot + min(offset) >= 1` as well as `+ max(offset) <= slot_count`), a migration, and a model
where an offset can point either way. One hardware job is cheaper than that permanently.

**Occupancy derived from stored addresses rather than declared offsets.** Considered at length. It
keeps addresses editable and makes occupancy *follow* them — but it makes occupancy depend on mutable
data, so it can drift, it costs address arithmetic across every VLAN range inside `clean()`, and it
still leaves "address that disagrees with its ordinal" reachable. Deriving the address instead
returns occupancy to its current cheap, immutable, type-derived form and removes the divergent state
outright.

**A divergence report at rest instead of enforcement.** #28's original framing, and the house habit.
Rejected on the measurement: there is nothing to report. A report is the right tool for inherited
reality, and this estate has none.

## Consequences

- **Stranding is accepted.** `mps-dm7c-1` claiming ordinal 4 for a VLAN 201 address makes ordinal 4's
  VLAN 200 address unusable, though nothing holds it. Affordable and deliberate: `Rack Increment` is
  a global 32 and production racks occupy 2–19 slots, so every rack carries at least 13 spare
  ordinals. This is also exactly what the spreadsheet does.
- **New refusals are accepted.** A genuinely free address is refused when its ordinal is claimed by a
  device addressed on some other VLAN. The error is better than what it replaces: "ordinal 4 is
  claimed by mps-dm7c-1" rather than a unique-constraint failure on an address.
- **ADR 0003 is superseded.** Its sole stated reason for mutability was a device-replacement workflow
  that remains `design deferred` 24 ADRs later — and which derivation serves *better*: put the
  replacement in the same slot and the address follows.
- **ADR 0001's override narrows** to the slot rather than the address. You still choose where a device
  goes; you no longer choose its address independently of that.
- **#60's `taken_by` axis is deleted** — `taken_by`, `taken-by-label`, `tag-address-taken`,
  `cell-taken`, `_build_taken_address_map()`, `ElevationRow.has_taken_address` and their tests.
  Derivation removes the state it existed to show wherever an address is set the ordinary way —
  through the admin (a device port's address field is read-only) or at materialization. It does not
  remove it everywhere; see "Known gaps" for the two residues (#99, #103) this deletion leaves open
  rather than closes. The operator-set-port tag in the device port table goes with `OPERATOR` itself.
- **#84 closes with no UI code.** `add_url` is set only where an ordinal has no occupant
  (`views.py:555-558`); once the ordinal is genuinely claimed, the link is simply absent.
- **A latent production question becomes a hard refusal.** `PROD-DATA-ANALYSIS.md:292-295` flagged
  that if `mps-sd9-1` (CONSOLES slot 11) has an engine, it needs ordinal 12, which `mps-sd11-1`
  holds. Today that is latent. Under this ADR, adding the engine port is refused outright — which is
  the system doing its job.

## Known gaps

- **Type ports are locked once instances exist** (ADR 0010, `models.py:2983-2994`), so rewriting the
  DM3 and DM7C type ports from `OPERATOR` to `slot_offset=1` requires a migration that bypasses the
  lock deliberately. This is the fiddliest part of the implementation and is called out here so it is
  designed rather than discovered.
- **The bracket encoding is knowingly wrong** between this ADR landing and #93 being taken. A sparse
  claim renders as two marked rows with a bracket asserting a contiguity that is not there.
- **The DMI-DANTE hardware divergence (#41) is unaffected.** The database is already the correct side;
  the cards still need re-addressing on the hardware.
- **A device port created directly with an explicit address bypasses derivation** (#99, open, planned
  immediately after this PR). `NetworkDevicePort.clean()` (`models.py:5101`) derives an address only
  when none was supplied; a programmatic `objects.create()` that supplies one in range keeps it,
  validated for containment and cross-table uniqueness but never checked against
  `range_base + rack_slot + slot_offset`. The admin's own address field is read-only, so this is
  reachable only by code that constructs a port directly rather than through the admin — but until
  #99 lands, the #60 marker's scenario (an empty ordinal whose would-be address is already held) is
  still reachable for a device, not only for a switch.
- **A switch address stays admin-editable and is never re-derived** (#103, open).
  `NetworkSwitchAddress.clean()` only auto-fills a blank address on insert
  (`models.py:3339-3369`) and validates by containment and cross-table uniqueness only, never
  against `range_base + rack_slot`; the admin inline declares no `readonly_fields`. An operator
  can therefore set a racked switch's address to the value another ordinal would offer, through
  the ordinary admin with full validation running.
