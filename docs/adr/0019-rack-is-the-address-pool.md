# Rack is the address pool; the ordinal is suggested, not typed

`docs/RACK-MUSINGS.md` raised a concern about the Rack concept and proposed replacing it
with "address pools within a subnet":

> While useful in physical racks, the concept breaks down when we start thinking about
> arbitrary groupings of devices. Allowing the creation of address pools within a subnet
> helps break free of the rack concept, at least in name. But it introduces some complexity,
> specifically around the alignment of addresses for devices that span multiple VLANs.

The worked examples were an AVIO device taking `10.201.1.4`, an SD9 console then taking
`10.200.1.4`, and an IK-42 after them taking `10.200.1.5` / `10.201.1.5` / `10.202.1.4` —
described, correctly, as "just weird."

This ADR declines the new concept. Rack remains the sole allocator and the sole grouping,
keeping its name. One thing changes: the system suggests a slot ordinal instead of requiring
someone to type one.

## The misalignment is a property of dense allocation, not of pools

The examples above all assume a pool hands out **the next free address on each VLAN
independently**. That assumption, not the pool, is what produces the weirdness — and it is
the one thing the current model already refuses to do.

`NetworkDevice`/`NetworkSwitch` carry a stored `rack_slot`
(`inventory/models.py:2163`), unique per `(rack, rack_slot)`, and
`_suggest_rack_slot_address()` (`inventory/models.py:289`) computes every port's address as
`range_base + rack_slot + slot_offset` — against *whichever* VLAN's range that port sits on.
The same ordinal is therefore applied to every VLAN the rack carries. Cross-VLAN consistency
is not a convention the operators maintain; it is arithmetic that cannot produce anything
else.

Replaying the musings' own examples through the existing ordinal model:

| Equipment | Ordinal | VLAN 200 | VLAN 201 | VLAN 202 |
| --- | --- | --- | --- | --- |
| AVIO (Dante Primary only) | 4 | *unused* | `10.201.1.4` | *unused* |
| SD9 (Control only) | 5 | `10.200.1.5` | *unused* | *unused* |
| IK-42 (all three) | 6 | `10.200.1.6` | `10.201.1.6` | `10.202.1.6` |

Nothing is weird, and no address is misaligned. What the AVIO "wastes" is `10.200.1.4` and
`10.202.1.4` — two addresses reserved and never used, which is the price of the guarantee and
is the same price production has always paid. The site currently uses 672 of a `/21`'s 2048
host offsets on VLAN 201 (21 racks × 32), so address scarcity is not a live pressure and
would not be a reason to trade the guarantee away.

## The rack concept was already abstract

The other half of the musings — that the *word* is wrong for arbitrary groupings — is a fair
observation about vocabulary, but the model was designed for it from the start. `CONTEXT.md`
already says a Rack is "an abstract grouping of equipment" and that a slot is "an *addressing
ordinal* (base address + slot number) — not a physical rack-unit position; physical RU
height/placement is deliberately not modeled."

Production bears this out. Of the 21 racks, `AVIO` (19 single-port Dante devices),
`CONSOLES`, `SHURE`, `FLOATSWITCH` and `SPARE` are groupings rather than enclosures, and they
address exactly as well as `WPC1SRU` does. The word is only wrong for some rows; the model is
wrong for none.

A rename was considered and declined — see "Rejected alternatives" below.

## Decision

1. **No new grouping concept.** Rack remains the only thing that allocates addresses and the
   only thing equipment belongs to. `RackVlanRange`, `rack_slot`, `slot_span`,
   `RackSlotAssignmentMixin` and the suggestion path are all unchanged in shape.

2. **Placing equipment suggests the next free ordinal.** The default is the lowest run of
   `slot_span` *consecutive* free ordinals in the rack — a run, not a single free slot,
   because a device whose type declares a non-zero Slot Offset occupies an ordinal range
   (ADR 0017) and must not be offered a slot whose successor is taken. This applies to
   `NetworkSwitch` and `NetworkDevice` alike.

3. **The suggestion is a default, not a lock.** An operator may type any free ordinal. This
   follows ADR 0001's suggest-with-override stance and ADR 0003's stored-not-derived stance,
   and production requires it: `AVIO` has `mps-avio-amph-output-4` at ordinal 15 while
   outputs 1–3 sit at 1–3, and `CONSOLES` starts at ordinal 4. Neither is reachable by a
   first-fit suggester, and neither is an error.

4. **Reserving a block of addresses is an empty Rack.** Create a Rack of the required shape,
   hand-place its ranges (ADR 0001 already permits this), and leave it unoccupied until it is
   wanted. `RackVlanRange` overlap validation already prevents any other rack being allocated
   into those offsets, so the reservation is real and enforced, not advisory.

5. **`CONTEXT.md`'s Rack entry is sharpened** to state that a Rack need not correspond to
   physical hardware, and that an empty Rack is a reservation of its address block while
   remaining an ordinary Rack in every other respect.

## Regions are not CIDR-shaped, so a reservation may take several racks

A `RackVlanRange` is an `IPv4Network` with `strict=True`, so a reservation must decompose into
properly-aligned CIDR blocks. The mnemonic gap `PROD-DATA-ANALYSIS` §7.2 describes — offsets
864–1279, held empty so wireless equipment is identifiable by eye — is 416 addresses and is
not one block. It decomposes into three:

| Offsets | Size | Block on VLAN 201 | Rack `slot_count` |
| --- | --- | --- | --- |
| 864–895 | 32 | `10.201.3.96/27` | 30 |
| 896–1023 | 128 | `10.201.3.128/25` | 126 |
| 1024–1279 | 256 | `10.201.4.0/24` | 254 |

So one mnemonic region is three reservation racks. That is clumsier than a single named
region, and it is recorded here as the honest cost of not building the address-regions
feature rather than glossed over. It is still strictly better than today, where nothing
prevents first-fit consuming the gap at all.

## The boundary with ADR 0015

ADR 0015's "`slot_count` stays honest" section rejects choosing a `slot_count` to obtain a
desired block size, calling it "a lie told to the schema to work around a missing rule." The
`slot_count` values in the table above are chosen for exactly that reason, so the boundary
needs stating rather than leaving for a later reader to trip over.

The cases are different, and the difference is occupancy. ADR 0015 rejected setting
`slot_count` to 30 on a rack **holding two devices**, and gave two concrete reasons: "how big
is this rack" becomes unanswerable from the data, and `Rack.clean()`'s growth guard loses its
meaning because the rack would accept equipment at any ordinal up to 30. Neither applies to an
empty reservation. A reservation rack holding nothing, with `slot_count` 254 and a `/24`, can
genuinely address 254 ordinals; that number *is* its capacity, answerable from the data and
true the moment anything is placed in it. The growth guard still means what it says.

**ADR 0015 is not amended.** Its rule — a `slot_count` must not exceed what the rack can
really hold — is untouched, and every reservation rack described here satisfies it exactly.

## What this does not solve

**Aligned rack allocation is a different problem and remains open.** `PROD-DATA-ANALYSIS`
§6.1 documents that a rack's *base offset* is allocated per `(rack, VLAN)` by independent
first-fit, so two VLANs with different DHCP geometry can hand the same rack different
offsets. That is a property of where a rack's block sits; this ADR is about the ordinal
*inside* the block. They are easy to conflate because both are called alignment. Nothing here
changes §6.1, and `ROADMAP.md`'s "Aligned rack allocation" item stands as written.

**Rack slot occupancy still has no DB-level overlap guarantee** once a device spans several
ordinals (ADR 0017's known gap, tracked as #40). Suggesting the lowest free *run* makes the
common path correct but is a default, not a constraint — an operator may still type an
ordinal that overlaps a spanning device's range, and `RackSlotAssignmentMixin` catches it in
`clean()` rather than in the schema.

## Rejected alternatives

**A separate Pool concept alongside Rack.** Two grouping concepts, one that allocates and one
that does not. Rejected because the non-allocating case has no live requirement — every
grouping in production allocates — and because a device would then need a rack *and* a set of
pools, with nothing to stop the two disagreeing about where it lives.

**Pools that allocate densely, replacing Rack.** This is the musings' literal proposal.
Rejected because it abandons cross-VLAN consistency, which `DESIGN.md`'s entire "Address
Computation" section is built on; because ADR 0017's Slot Offset becomes incoherent without an
ordinal to offset from; because the mnemonic property of §7.2 goes with it; and because the
183 imported production addresses were placed by `base + slot` and would no longer be
reproducible by the verifier. The saving is address space that is not scarce.

**Renaming Rack to Address Pool.** Rejected on cost against benefit. It would move the model
and table, `RackVlanRange`, `rack_slot`, `RackTemplate` (ADR 0014) and the importer and
verifier, and would touch ADRs 0001, 0014, 0015, 0016, 0017 and 0018 plus `DESIGN.md` and
`CONTEXT.md` — to fix a word that is accurate for most of the rows it names. `WPC1SRU`,
`W8LM3` and the amp racks are physical racks, and the operators call them racks. Sharpening
the glossary entry addresses the same confusion for the price of one edit.

**A `purpose` or `reserved` flag on Rack**, to mark reservations as a distinct kind.
Rejected: `CONTEXT.md` states that Rack has no purpose field, deliberately, and ADR 0014
declined the nearest neighbour of this idea. A reservation is an ordinary empty Rack, exactly
as a rack of spares is an ordinary Rack.

## Consequences

- **`ROADMAP.md`'s "Address regions" item is downgraded, not deleted.** Reserving offset space
  is now possible without it, which was the item's main motivation. What regions would still
  buy is automatic enforcement — the suggester restricting its search to a window, so nobody
  has to remember to create the reservation racks first — plus a single named region instead of
  a CIDR decomposition. That is a real but much smaller benefit than the item currently claims,
  and the item is rewritten to say so.

- **`docs/RACK-MUSINGS.md` is superseded by this ADR** and removed; its content and examples
  are carried above.

- **The UI sketch's rack elevation is unaffected**, since ordinals survive. Its screen 3
  warning banner remains the right treatment: first-fit will still offer an offset inside a
  reserved gap if no reservation rack has been created there.

- **Implementation is small**: a suggestion helper that finds the lowest free run of
  `slot_span` ordinals, wired into the admin add forms for `NetworkSwitch` and `NetworkDevice`
  as an initial value. No migration, no model change, no change to any stored address.
