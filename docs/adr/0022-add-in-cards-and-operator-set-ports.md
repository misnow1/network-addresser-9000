# Add-in cards are devices; a box's extra addresses are ports

`ROADMAP.md` phase 17 asked a small question — where do `docs/MORE_MUSINGS.md`'s `-engine` and
`-device-control` hostname suffixes live? Answering it required deciding whether a DiGiCo SD12's
audio engine is a *port* or a *device*, and pulling that thread showed the model's three boxes —
port, companion, independent device — were being sorted by criteria that have nothing to do with
what separates them.

The question stayed unanswerable for as long as it was argued from mechanism. It became easy the
moment Mike wrote down ten pieces of real hardware and what each one *does*
(`docs/Constituent Device Design - Sheet1.csv`), because two of his columns sort every row:

| Hardware | Optional? | Removable? | Host sets address? | Hostname same as host? |
|---|---|---|---|---|
| DiGiCo SD12 audio engine | No | No | Forced by host | Slug: `-engine` |
| Yamaha Device Control | No | No | Set by user in host UI | Slug: `-device-control` |
| Marian Clara E — in a Waves LiveBox-D | No | No | No | Yes |
| Martin Audio IK Dante card | **Yes** | **No** | No | Yes |
| DiGiCo DMI-DANTE | Yes | Yes | No | No |
| Behringer X-Dante | Yes | Yes | No | No |
| Allen & Heath AH-M-SQ-SDANTE64-A | Yes | Yes | No | No |
| Marian Clara E — generic | Yes | Yes | No | No |
| DiGiCo DMI-WAVES | Yes | Yes | No | N/A |

**Can you leave it out? Can you take it out?** Those two questions produce three tiers, and every
row above lands in exactly one of them.

## What the old boundaries got wrong

**ADR 0017 used an addressing test** — *"does the hardware compute the second address from the
first and refuse to let anyone change it?"* That is a sound rule and it correctly identified that an
SD12's engine is always control + 1. But it was asked to answer a *modelling* question, and so
concluded that anything failing the test must be a separate device. That is how the Yamaha Device
Control became its own `NetworkDevice`.

**ADR 0018 used a procurement test** — is it an optional accessory, bought separately? Whether
hardware was on the same purchase order says nothing about how it behaves. Two IK-42s in `XE300-1`
have no Dante card fitted and fifteen do; that is a fact about those units, not about what an IK-42
*is*.

**Form factor is not the test either.** A DMI-DANTE is a sliver of PCB inside a console and is its
own device; an SD12's audio engine is inside the same chassis and is not.

None of the three asks about lifecycle, which is the only thing that actually varies.

## Tier 1 — one physical box is one device

An SD12's audio engine and a Yamaha console's Device Control interface are both **not optional and
not removable**. You cannot buy the console without them, and you cannot take them out and keep
them. They are addresses on one box, and the box is the device.

A Yamaha console's built-in Dante interface belongs here too, and is the reason this tier is worth
stating rather than assuming: it is already modelled as two ports on the console, and it stays that
way. It appeared in an earlier revision of the hardware table and was struck from it, which is the
correct instinct — a built-in interface with no separate identity is not *constituent hardware*, it
is what the console is.

The hostnames in the addressing sheet — `SD12-96-1-Engine`, `dm7c-1-device-control` — are
bookkeeping labels for those addresses, not network identities of separate hardware. Nothing racks
an SD12's engine; you rack an SD12.

Three port mechanisms cover the tier, and Mike's "Host sets address?" column names which one each
piece of hardware needs:

- **"Forced by host"** — `slot_offset` (ADR 0017), derived from the offset-0 port on the same VLAN
  and locked against edits. The SD12 engine, and nothing else in the estate.
- **"Set by user in host UI"** where the port is the *first* on its VLAN — the ordinary case, today's
  behaviour: an address suggested from the rack slot and freely editable afterwards (ADR 0003, ADR
  0019). A Yamaha console's built-in Dante Primary and Secondary.
- **"Set by user in host UI"** where the port is the *second* on a VLAN that already carries one —
  **not currently expressible**, and the reason ADR 0018 exists. The Yamaha Device Control, whose
  table entry states the constraint exactly: *"Different VLAN from host, same VLAN as host's Dante
  Primary interface."* It shares the console's **Dante Primary** VLAN, not its control VLAN — which
  is what production shows (`10.201.6.4`, annotated *"Only on Dante Primary for controlling
  snakes"*), and worth stating because an earlier draft of this ADR asserted the opposite.

That last gap is issue #42. `_check_static_materialization_possible()` (`inventory/models.py:3379-
3390`) groups a type's ports by `(vlan_id, slot_offset)` and refuses any group above one, because
`suggest_slot_address()` has exactly one address to offer per `(slot, VLAN)`. The Yamaha Device
Control needs a second address on VLAN 201 with no derivable relationship to the first — production
points *both ways*, `dm7c-1-device-control` at `10.201.6.4` against its console's `10.201.6.5`, and
`bej-dm3-1-device-control` at `10.201.6.16` against `10.201.6.15` — so `slot_offset` cannot express
it in either direction, and is a `PositiveIntegerField` besides.

ADR 0018 worked around that gap by making the interface a separate device and inventing an
inseparable-companion relationship to hold the two together. That was a workaround for a limitation
already filed as an issue, not a conclusion about what the hardware is. **This ADR closes the gap
instead, and ADR 0018 goes away entirely.**

## Tier 2 — optional but fixed is a Type Profile

A Martin Audio IK Dante card is **optional and not removable**: an IK-42 may be bought with or
without one, and once fitted it stays. Card presence is therefore decided at purchase and never
changes for that unit — which is exactly what a Type Profile expresses, and exactly what the
importer already does (`ik42_with_card` / `ik42_without_card`, `import_prod_data.py:169-186`).

The same reasoning covers the Marian Clara E when it is installed in a Waves LiveBox-D: not
optional, not removable, no hostname of its own. It is part of what a LiveBox-D *is*, so its ports
belong to the LiveBox-D's type. The identical physical card in a generic PC is optional and
removable, and lands in tier 3. **The same hardware is modelled two ways because it behaves two
ways** — which is what Mike's table does by giving it two rows.

Nothing is built for this tier. It already works.

## Tier 3 — removable hardware is its own device

A DiGiCo DMI-DANTE is **optional and removable**. One of them is in a box beside the console storage
wall right now, holding the address programmed into it, belonging to no console at all. A card
exists independently of a console and a console exists independently of the card; they are joined
for a while and then they are not.

That is an ordinary `NetworkDevice`, with one addition: a nullable link recording which host it is
currently fitted to.

The card takes **its own rack slot**, like any device. This is not a claim that a card occupies rack
units — `CONSOLES` is not a physical rack, it is an address pool (ADR 0019), and a slot number
exists to compute an address. A card that has an address belongs in a pool and needs an ordinal to
get one. The link to its host carries **no addressing meaning whatsoever**: it does not derive an
address, constrain a VLAN, share an ordinal, or move anything.

## Decision

### 1. The test is *optional* × *removable*

Recorded here so future hardware is sorted by lifecycle rather than by chassis, price list, or
whether Dante is involved:

| | Not removable | Removable |
|---|---|---|
| **Not optional** | ports on the host (tier 1) | — no known case |
| **Optional** | Type Profile (tier 2) | its own device, linked (tier 3) |

`CONTEXT.md`'s avoid-lists are rewritten against this table.

### 2. `NetworkDeviceTypePort.address_source` — `SLOT` or `OPERATOR`

A `TextChoices` field beside `slot_offset`, orthogonal to it:

- **`SLOT`** (default) — the address is computed from the rack range base plus the device's slot
  plus `slot_offset`, exactly as today, and remains editable afterwards.
- **`OPERATOR`** — there is no computed address. The operator supplies one at creation and it stays
  editable forever.

`OPERATOR` ports are **exempt from the one-port-per-`(vlan, slot_offset)` refusal**, which is what
lets one device carry two independent static addresses on one VLAN. This closes **issue #42** and
amends ADR 0013's one-address-per-VLAN rule, which is narrowed from "a device may not have two ports
on a VLAN" to "a device may not have two *slot-addressed* ports on a VLAN". ADR 0013's actual
constraint — that `suggest_slot_address()` has one address to give — is untouched, because an
`OPERATOR` port never asks it for one.

`slot_offset` and ADR 0017 survive unchanged. The two fields answer different questions: `slot_offset`
is *"is this address derived from another port's?"*, `address_source` is *"does the system compute
this address at all?"*

### 3. The address is supplied at creation, not patched afterwards

`NetworkDevicePort` has a `CheckConstraint` (`inventory/models.py:4383-4384`) requiring
`is_dhcp=True AND address IS NULL` or `is_dhcp=False AND address IS NOT NULL`, so a static port with
a blank address cannot exist even transiently. An `OPERATOR` port on a racked device therefore takes
its address from the device add form, which refuses creation without it.

This is a straight swap: `NetworkDeviceAddForm` already prompts for `companion_hostname` and
`companion_rack_slot`, and both are deleted by this ADR. It also matches ADR 0013's principle that a
port's addressing is decided at creation rather than corrected later, and Mike's own column heading
— *"Set by user in host UI"*.

An unracked device materializes DHCP as it does today, and an `OPERATOR` port is no exception.

### 4. `NetworkDeviceTypePort.hostname_suffix`, derived read-only on the instance

A blank-by-default `CharField` on the type port. Where it is set, `NetworkDevicePort` exposes a
derived, read-only `hostname` property — `<device.hostname>-<suffix>` — and stores nothing. Where it
is blank, the port has no name of its own and is simply part of the device's identity.

That covers Mike's hostname column exactly: `-engine` and `-device-control` are suffixes, a Yamaha
console's built-in Dante is blank, and a card's *"Hostname same as host? No"* is not a port question
at all — a card is a device and carries an ordinary `hostname` field.

**`hostname_suffix` is exempt from the type-port profile lock.** Every other field on
`NetworkDeviceTypePort` is frozen once the type has instances (`inventory/models.py:2983-2994`, ADR
0010), because an edit would silently disagree with already-materialized ports. A derived label has
no materialized counterpart to disagree with, and a typo'd suffix must stay fixable without creating
a new named profile for every DM7C in the estate. This is the same reasoning
`PLAN-hostname-ingredients.md` decision 6 applies to `hostname_slug`.

Assembly, collision handling and total-length validation are **not** settled here; they belong to
the hostname ADR. This ADR settles only *where a port label lives*, because that was the question
blocking phase 17.

> **Since written:** that ADR is `docs/adr/0023-hostname-scheme.md`, written in phase 17 rather
> than 18, and it covers the ingredients as well as the computation. It settles the three items
> above, and puts derived port hostnames *inside* the uniqueness check rather than outside it.

### 5. `NetworkDevice.host` and `NetworkDeviceType.is_add_in_card`

`host` is `ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
related_name="installed_cards")`. It records which device a card is currently fitted to and nothing
else. `is_add_in_card` is a boolean on the Type; the "fit a card" picker offers only those types,
and a device whose type is not marked cannot be given a host.

The boolean is deliberately the smaller instrument. A `fits_host_types` compatibility matrix was
considered and rejected — see below.

### 6. The three validation edges

- **No nesting.** A card cannot be fitted to a card. Nothing observed needs it, and one level keeps
  every fit, pull and delete path finite.
- **Cross-rack is allowed.** A card may sit in a different rack from its host. A rack is an address
  pool, not a chassis, so nothing physical is violated, and forbidding it would invent a rule the
  hardware does not have.
- **Deleting a host is allowed and clears the link.** The cards keep their slots, their addresses
  and their rows, and simply stop being fitted to anything. This is not ADR 0007's container case: a
  console does not contain the card's address, the rack does.

The third is a real loosening — today deleting a host deletes its companion — and it is the correct
one, because a card outliving its console is the entire point of tier 3.

### 7. Hardware with no address is out of scope

A DiGiCo DMI-WAVES has no IPv4 address at all; it uses IPv6 link-local only, and Mike's table gives
its hostname as `N/A`. It is not modelled. This system's ordinal *is* an address, so hardware
needing no address has no place in a pool, and a rackless, addressless, hostnameless row would be
the first thing in this database the application has nothing to say about.

The knowledge this forgoes — *which of an SD12's two DMI slots are free?* — is an asset-tracking
question, and answering it properly would mean modelling card-slot capacity on host types. That is a
different system.

### 8. ADR 0018 is superseded and its machinery is deleted

`dm7c_devctrl` and `dm3_devctrl` stop being device types and become fourth ports on their consoles,
`address_source=OPERATOR` on VLAN 201 with `hostname_suffix="device-control"`. With them go
`NetworkDeviceType.companion_type`, `_materialize_companion()`, `_companion_rack_slot`,
`_companion_hostname`, `_check_companion_creation_possible()`, `_check_companion_type_compatibility()`,
`_plan_companion_move()`, `_park_companion_if_colliding()`, `_finish_companion_move()`, the
`_host_managed_move` privileged-writer flag, the companion pair's slot-occupancy exclusion, and the
companion fields on `NetworkDeviceAddForm`.

Decision 5's `host` FK shares no code with any of it. ADR 0018's relationship was inseparable,
materialized with its host, moved as a unit and cascaded on delete; this one is separable, fitted
later, moves independently and survives its host. Reusing the machinery would mean keeping a
mechanism whose every property is the opposite of what is needed.

**The word "companion" is retired with it.** `is_add_in_card` and "fitted to" replace it in
`CONTEXT.md`, the admin and the UI, because a term that meant *"cannot exist without its host"* until
this ADR and *"routinely sits in a box on its own"* after it would make every earlier document
ambiguous.

## What this does to production data

`prod/MPS Audio Network Standards - IP Addressing mk2.csv` rows 57-58 and 69-70:

```
dm7c-1-device-control,CONSOLES,4,,10.201.6.4,,Only on Dante Primary for controlling snakes
DM7C-1,CONSOLES,5,10.200.6.5,10.201.6.5,10.202.6.5,
bej-dm3-1,CONSOLES,15,10.200.6.15,10.201.6.15,10.202.6.15,
bej-dm3-1-device-control,CONSOLES,16,,10.201.6.16,,
```

These stop being four rows and become two devices. `DM7C-1` at slot 5 carries Control `10.200.6.5`,
Dante Primary `10.201.6.5`, Dante Secondary `10.202.6.5` and a Device Control port at `10.201.6.4`;
`bej-dm3-1` at slot 15 carries the same shape with its Device Control at `10.201.6.16`. **Slots 4
and 16 are released** — the interfaces stop consuming an ordinal to hold an address that was never
derived from one. Every address value in the sheet is reproduced exactly.

Rows 60-63 are unchanged: the SD12's engine is already an offset port and stays one.

**Issue #41 stays open.** The DMI-DANTE card keeps its own slot (`CONSOLES` 17,
`import_prod_data.py:893`) and therefore keeps the addresses `10.201.6.17` / `10.202.6.17` rather
than the `10.201.6.7` / `10.202.6.7` on the SD12's own row — so the physical cards are still
re-addressed next time they are out of a console, as `PLAN-prod-import.md` §9 already decided. Making
the sheet's values reproduce would require the card to share its host's ordinal, and a card whose
address is programmed into it and travels with it between consoles has no business deriving that
address from whichever console it currently sits in.

**No data migration.** The spreadsheet remains the source of truth and the database is rebuilt from
the CSVs.

## Rejected alternatives

**Widen "companion" to cover cards and extenders** (the earlier draft of this ADR). It sorted
hardware by *"can it work without a host, and does it have its own identity?"*, and needed two
ordinal modes, a `detachable` flag, a `slot_claim` column, a replacement for
`unique_device_rack_slot`, allocator changes and a host-port VLAN binding to do it. Every one of
those exists to make a card's address follow its host's ordinal — which is wrong for hardware whose
address is programmed into it and survives the move. Rejected as machinery built to reproduce a
spreadsheet convenience.

**Type Profiles for removable cards too**, with no `host` link at all — `SD12 — with DMI-DANTE` as a
profile. It reproduces the production sheet byte for byte and closes #41 for free. Rejected because
`device_type` is locked after creation (`inventory/models.py:3669-3687`), so fitting a card to a
console already in service would mean deleting and recreating the console, and because it cannot
represent the card in the box: a profile describes what a console has, and has nowhere to put a card
that is currently in nothing.

**Allow switching profile within one manufacturer and model**, reconciling ports on the switch. It
keeps the row and its identity while avoiding a `host` FK entirely. Rejected for the same second
reason: a loose card is still unrepresentable, and Mike's cards are loose often enough that one is
loose right now.

**A card shares its host's rack slot.** Reproduces the sheet and closes #41. Rejected because it
needs the constraint swap and allocator changes above, because it breaks outright when two
DMI-DANTEs sit in one SD12 — both compute the same two addresses — and because a card's address does
not in fact follow its host.

**A card is never racked at all**, holding a static address while unracked, with its position shown
through its host. Cleaner in one respect: the card in the box is the natural state rather than a
special case. Rejected because it contradicts ADR 0013 decision 3's flat "unracked is always DHCP"
and ADR 0019's "the rack is the address pool" for no gain — a card that has an address belongs in a
pool.

**A `fits_host_types` compatibility matrix** rather than decision 5's boolean. Rejected because its
failure mode is blocking hardware that genuinely fits, and because Mike's own host-type column is
prose (*"Yamaha console as described in column B"*, *"Generic PC"*) — documentation rather than a
constraint anybody wants enforced. The boolean gets the picker filtering, which was the only
concrete benefit.

**A signed `slot_offset`**, letting the Device Control be `−1` on a DM7C and `+1` on a DM3. Rejected
because those directions are an accident of how two consoles were addressed, not a property of the
hardware — encoding them as derivation would lock an address the operator must be able to set, which
is the exact defect this ADR is fixing.

**Keeping the word "companion"** for decision 5's relationship. Rejected per decision 8: the new
meaning is the inverse of the old one.

## Consequences

ADR 0018 is **superseded**. ADR 0013 is **amended** (decision 5's one-port-per-VLAN rule is narrowed
to slot-addressed ports). ADR 0017 is **amended** — its mechanism is untouched, but its
scope-boundary section no longer speaks for the model, since add-in cards are now linked to their
hosts and the Device Control is now a port.

`CONTEXT.md`'s **Device Companion** entry is deleted and replaced by an **Add-in Card** entry; the
**Network Device Type Port** entry gains `address_source` and `hostname_suffix`; the **Type Profile**
entry gains the optional-but-fixed case as its motivating example; the **Spare Pool** entry is
unchanged, because a pulled card keeps its rack slot and never enters it.

Issue **#42 closes**. Issue **#41 stays open**, unchanged. Issue **#27** is untouched — two jacks
bridged onto one VLAN *sharing* one address is still unrepresentable, and `address_source` does not
help, because that shape needs one address on two ports rather than two addresses on two ports.

`ROADMAP.md` phase 17 resumes. `hostname_suffix` joins its field list, and phase 18's hostname ADR
inherits one named gap: a derived port hostname (`sd12-96-1-engine`) sits outside the planned
cross-table uniqueness check across `NetworkSwitch` and `NetworkDevice`, so nothing stops a device
being hand-named to collide with one.
