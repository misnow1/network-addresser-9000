# Derived same-VLAN addresses: per-port slot offsets and multi-ordinal devices

> **Amended by [ADR 0022](./0022-add-in-cards-and-operator-set-ports.md).** The mechanism below
> (`slot_offset`) is untouched. Its "Scope boundary" section's own worked example is not: `bej-dm3-1`
> and `bej-dm3-1-device-control` no longer "stay two ordinary `NetworkDevice`s in two slots" — the
> Device Control interface is now `address_source=OPERATOR` port on the console's own Type (ADR 0022
> closes issue #42, which this ADR's own text names as the gap that pushed that pairing into two
> devices in the first place). The scope-boundary *test* itself — "does the hardware compute the
> second address from the first and refuse to let anyone change it?" — still stands and still
> separates `slot_offset` (an SD12's engine) from an add-in card (a DMI-DANTE); only this section's
> Device Control example no longer speaks for the model.

A DiGiCo SD12 always occupies two addresses on the control network. The first is set by
the operator and is what iPad control, OSC, and everything else talks to. The second is the
console's audio engine, it is **always** the control address plus one, it is assigned
automatically by the console software, and it cannot be edited on the hardware at all. The
SD7 is a dual-engine console and behaves the same way.

The current model cannot express this. Two ports on one VLAN is precisely the shape
`NetworkDevice._check_static_materialization_possible()` (`inventory/models.py:2700`)
refuses outright, with an error that says the device "has no way to give one address to all
of them." That refusal is correct for the case it was written for — ADR 0013's Switched
Mode, where two bridged jacks genuinely share a *single* address — but it is wrong here.
An SD12 does not want one address for two ports. It wants two addresses, in a fixed
relationship, and it knows exactly what that relationship is.

The production spreadsheet worked around this by splitting each console into two rows in
two slots (`SD12-96-1-Control` at `10.200.6.7`, `SD12-96-1-Engine` at `10.200.6.8`). That
produces the right addresses and loses every fact worth recording: that these are one
console, that the second address is derived, and that slot 8 is not available for anything
else.

## Decision

`NetworkDeviceTypePort` gains a `slot_offset` (`PositiveIntegerField`, default `0`), and a
port's suggested address becomes:

```
range_base + rack_slot + slot_offset
```

An SD12 type is then two ports on VLAN 200 — `Control` at offset 0, `Engine` at offset 1 —
and a console in slot 7 of a rack whose Control range is `10.200.6.0/27` materializes
`10.200.6.7` and `10.200.6.8`. That is exactly what production already has, arrived at
honestly.

This introduces no new arithmetic. `suggest_slot_address(range_cidr, slot)` is already
`base + slot`; offsets change what is passed as `slot`, nothing more. Every existing type
port takes the default `0`, so all current addressing behaviour is bit-for-bit unchanged.

### A device occupies a range of ordinals, not one

A device whose type declares a maximum offset of *n* occupies ordinals
`rack_slot … rack_slot + n`. This is the half of the decision that makes the other half
safe: without it, nothing stops a later device being assigned slot 8 and colliding with the
console's engine address.

`slot_span` is a property of the device **type** — `max(slot_offset) + 1` across its type
ports — and a type's port list is immutable once any instance exists (ADR 0010), so the
span of a created device can never drift. Whether to denormalize it onto `NetworkDevice` to
keep the occupancy check a plain range query, rather than aggregating over ports on every
slot validation, is left to the implementation plan; both are stable, and the choice is
about query cost, not correctness.

Switches keep a span of exactly one. `NetworkSwitchAddress` has no port model and no offset
concept, and no switch hardware here needs one.

### The same-VLAN refusal narrows rather than disappearing

`_check_static_materialization_possible()` groups addressable ports by `(vlan_id,
slot_offset)` instead of by `vlan_id`, and refuses any group holding more than one port.

This is a *more precise* check, not a weakened one. Switched Mode (Shure ULXD4Q/D, and the
generic bridged 2-port case) is two ports on the same VLAN at the same offset — one
address for two jacks — and is still refused with the same error, because that is still a
device shape this model cannot represent. What changes is that the check no longer catches
the SD12 by accident. The distinction it now draws is the real one: same VLAN and same
offset means two ports contending for one address, which is a contradiction; same VLAN and
different offsets means two ports with two defined addresses, which is a console.

The logical-interface work tracked as #27 is unaffected and still needed.

### The engine address is not independently editable

A port materialized at a non-zero offset has a read-only address, derived from the offset-0
port on its VLAN. `NetworkDevicePort` already locks `description`, `vlan`, `port_type`,
`ordinal`, and provenance after creation (`_locked_fields()`, `:2959`), leaving
`is_dhcp`/`address`/`switch_port` editable; this moves `address` into the locked set when
`slot_offset > 0`, and adds `slot_offset` itself to it.

`slot_offset` is **copied onto `NetworkDevicePort`** at materialization rather than read
live through `source_type_port`. That FK is `SET_NULL`, so a port that lost its type port
would otherwise lose the offset that makes its address make sense. Copying it is also what
ADR 0010's seed-once pattern already does for every other identity field on the row.

## What this costs ADR 0003, precisely

ADR 0003 says a device's address is stored rather than derived at read time, and must stay
mutable so a future device-replacement workflow can carry an existing address onto a new
instance. A read-only engine address is a carve-out from that, and it should be recorded as
one — but a narrow one, and it is worth being exact about the scope because it is easy to
overstate.

What changes: an offset port's address cannot be edited, and it is recomputed when the
offset-0 address it derives from is edited. Overriding it independently would describe a
state the console physically cannot be in — the software assigns that address and offers no
field to change it — so a mutable engine address does not model flexibility, it models a
value that is wrong.

What does **not** change: nothing recomputes on a rack or slot move. Addresses remain
stored and static exactly as ADR 0003 requires; moving a console changes neither address,
and `_validate_existing_addresses_still_fit()` (`:2805`) blocks the move if they would fall
outside the new rack's range, the same as for any other device. The derived relationship
survives a move untouched precisely *because* neither value recomputes. ADR 0003's
device-replacement motivation is also intact: the offset-0 address remains mutable, which
is the one a replacement workflow would need to carry over, and the engine follows it.

The declined alternative was to lock the engine address without deriving it — static, and
simply not editable. That keeps ADR 0003 fully intact at the cost of allowing an engine
address to sit permanently wrong if its control address is ever changed, with no way to
correct it. Given the whole point is that the hardware guarantees `control + 1`, a stored
value that can silently stop satisfying that is worse than the carve-out.

## Scope boundary: this is only for derived, mandatorily-consecutive addresses

`slot_offset` is not a general mechanism for hardware that comes in several pieces, and the
ADR states this because the temptation to reach for it will be constant.

A Yamaha DM7 with a DM7-EX extender is two parts of one console, but their addresses are
independent, assigned by hand, and under no requirement to be consecutive. They stay two
ordinary `NetworkDevice`s in two slots — which is how the production data already has them
(`DM7C-1` in slot 5, `DM7-EX-1` in slot 6), and it is correct there. Same for
`bej-dm3-1` and `bej-dm3-1-device-control`.

The test is not "is this one chassis?" or "does this need several addresses?" — it is
"**does the hardware compute the second address from the first and refuse to let anyone
change it?**" If an operator can type both values independently, they are separate devices.
If `slot_offset` were applied on the looser reading, it would drift into being a
physical-rack-position field, which `CONTEXT.md` explicitly rejects and which slot ordinals
have deliberately never been.

Add-in cards are separate devices under the same test. A DiGiCo DMI-DANTE card has its own
Dante Primary and Secondary addresses with no derived relationship to the console's control
address, so it is its own `NetworkDeviceType` in its own slot, with its own serial number —
independently trackable and swappable, which is what it is in the field. (The production
spreadsheet lists the card's Dante addresses on the SD12's own row, annotated `Used as
DMI-DANTE2 Addresses?`; that was a field-programming convenience, and the SD12 itself has
no built-in Dante interface at all.)

## The bound that keeps `.255` avoidance true

`rack_slot + max(slot_offset)` must be validated against `rack.slot_count`, in
`RackSlotAssignmentMixin.clean()` alongside the existing `rack_slot > slot_count` check
(`:1252`).

This is not only about capacity. `required_block_size()` reserves a block's top index so no
slot address reads as a block-relative broadcast, which is what makes `DESIGN.md`'s
"avoid octets of all 1s" guidance structural rather than remembered (see ADR 0015). Offset
addresses bypass the `rack_slot`-based bound that currently guarantees this: on a `/27`
based at `x.224` — and the production data has two such racks — ordinal 31 is `x.255`. A
device at slot 30 with an offset-1 port reaches it. The bound above is the only thing
standing between offsets and the exact address class this project has gone out of its way
to never assign.

## Known gap: span overlap is validated in `clean()`, not by the database

Today's `unique(rack, rack_slot)` constraints on `NetworkSwitch` and `NetworkDevice` give a
database-level guarantee that two rows can't claim one slot. No `UniqueConstraint` can
express "no two occupants' ordinal *ranges* overlap," so the span check necessarily lives
in `clean()`.

The recommended shape keeps the existing DB constraint on the starting ordinal — which
still catches the common case — and adds the span-overlap check in `clean()` next to
`_check_rack_slot_not_occupied()` (`:2151`, `:2799`). The result is that a same-starting-slot
collision is still refused by the database, while an overlap-without-collision (device at
slot 7 spanning 8, new device at slot 8) is refused only at `full_clean()` time and remains
reachable by direct ORM writes.

This lands in territory the codebase already documents rather than opening new ground.
`RackSlotAssignmentMixin` (`:1225`) already describes its cross-table occupancy check as
"an interim, form/full_clean-time guard, not a concurrency-safe one," and already names a
shared rack-occupancy table as the real fix. So spans make an acknowledged weakness matter
more without changing its shape — recorded here in the same terms ADR 0013 and ADR 0014 use
for their own known gaps: pre-existing in kind, exercised more often, not closed.

One correction worth making while this is in view: that docstring defers the fix to "phase
3's 'Overlap validation' work (see `ROADMAP.md`)", and **that deferral no longer has a live
home.** `ROADMAP.md:24`'s "Overlap validation" item is checked off, and what shipped under it
was rack-range-versus-range and rack-range-versus-DHCP overlap — not rack *slot* occupancy,
which is a different problem against a different table. The docstring's forward-reference is
stale and points readers at completed work. Whoever implements this ADR should either
re-file the rack-occupancy gap under `ROADMAP.md`'s "Later / not yet designed" section
(alongside #27 and #28, which it is closely related to) or correct the docstring to stop
promising a home it doesn't have.

## Follow-up

Implementation is a separate, independently reviewed plan. This ADR is materially larger
than ADRs 0015 and 0016 and should not share a plan with them — it touches the type-port
schema, the materialization path, the locked-field rules, rack-slot validation, and the
same-VLAN pre-flight, and it amends ADR 0003.

Coverage the plan must include:

- An SD12-shaped type materializing `base + slot` and `base + slot + 1` on one VLAN.
- The offset port's address rejected as read-only after creation, and recomputed when the
  offset-0 address is edited.
- A second device refused at an ordinal inside an existing device's span.
- `rack_slot + max(slot_offset) > slot_count` refused — specifically on a `.224`-aligned
  `/27`, where the failure mode is a `.255` address.
- Switched Mode still refused (same VLAN, same offset), proving the pre-flight narrowed
  rather than lost that case.
- Every existing device type, at offset 0 throughout, addressing exactly as before.

An SD7's engine count needs confirming from the hardware before its type is defined; the
mechanism covers any count (offsets `1..n`), so this blocks that one type, not the design.
