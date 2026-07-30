# Plan: importing the production addressing data

Design for loading the three production CSVs (see `PROD-DATA-ANALYSIS.md`) into the app.
**Nothing here is built yet** — there is no CSV importer today, and the Django admin is the
only data-entry path. This document defines the sequence, the constraints that make the
sequence load-bearing, and the things that cannot be imported until someone supplies
information the export doesn't contain.

## Prerequisites

The import cannot faithfully reproduce production until all three are implemented:

- **[ADR 0015](docs/adr/0015-minimum-rack-block-size.md)** — without the `/27` floor, honest
  slot counts reproduce 1 of 21 rack bases instead of 19.
- **[ADR 0016](docs/adr/0016-switch-address-materialization.md)** — otherwise ~126 switch
  address rows are hand-entered.
- **[ADR 0017](docs/adr/0017-derived-same-vlan-addresses.md)** — otherwise the DiGiCo
  consoles cannot be created at all.

Plus the source-data fixes in `PROD-DATA-ANALYSIS.md` §5. The duplicated-row collapse (§2.2)
is handled by the importer rather than by editing the sheet, since the rule is mechanical:
one device per `(rack, slot)`.

## The constraint that shapes everything: creation order is load-bearing

`suggest_rack_vlan_range()` is first-fit. It takes the lowest free block of the required
size, which means **the order racks are created in determines which base each one gets.**
Production's bases are a record of the order its racks were added, so the importer must
create racks in ascending `Address Offset` order — `CONTROL`, `WPC1SRU`, `WPC2SRL`, … — or
every base after the first divergence shifts.

This is not a flaw to design around; it is what ADR 0001 chose, and the offsets are
reproducible precisely because the ordering is recoverable from the sheet. But it does mean
the importer is a single ordered pass, not a set of independent row inserts, and it cannot
be safely re-run against a partially-populated database.

Two consequences:

- **Idempotency is not achievable in the useful sense.** A half-completed import leaves
  racks holding blocks that a re-run would allocate differently. The importer should run
  inside one transaction and roll back entirely on any failure, and should refuse to start
  if any `Rack` already exists.
- **`SHURE` and `CONSOLES` must be created without a template.** Their bases sit behind
  deliberate gaps (14 and 8 increments) that first-fit will never leave, so all three of
  their ranges are entered explicitly. Per ADR 0014 decision 11, naming the same VLAN in
  both a template and a manual inline row is a validation error — so these two racks take
  manual ranges only, not template-plus-overrides.

## Sequence

`manage.py seed_defaults` already provides VLAN 1 ("Default VLAN", L2-only) and the system
`Default` switch port profile at container start; the import builds on top of that.

### 1. Audit identity

Create a dedicated import user and set `created_by` on every row. `AuditedModel.created_by`
is nullable (`inventory/models.py:378`), so leaving it null would work — but ADR 0004 exists
to make provenance traceable, and "imported from the production spreadsheet on date X" is
exactly the kind of provenance worth keeping.

### 2. VLANs — 8 rows

Name, `vlan_id`, subnet from the table in `PROD-DATA-ANALYSIS.md` §1. Then:

- **Default gateway** `10.x.0.1` for each (`suggest_default_gateway()`'s value; stored and
  overridable per ADR 0003).
- **DHCP range `10.x.0.2`–`10.x.0.254`.** This must be set **before** any rack range is
  allocated. It is what reserves the bottom `/24` and pushes the first rack block to
  `10.x.1.0/27`, reproducing production's offset of 256. Allocate ranges first and every
  base is 256 too low. The range starts at `.2` because ADR 0011 forbids it containing the
  gateway.

Confirm the real DHCP pool bounds before committing to `.2`–`.254`; the export doesn't
record them, only that nothing static lives below offset 256.

### 3. Switch port VLAN profiles

After VLANs (the FKs are `PROTECT`), before switch types.

| Profile | Mode | Native | Allowed | Notes |
|---|---|---|---|---|
| All Configured VLANs Trunk | trunk | 201 | 100, 101, 200, 202, 207, 220, 221 | matches the running config in §4 of the analysis |
| Control Access | access | 200 | — | access forbids `all_vlans_allowed` and any allowed list |
| Dante Primary Access | access | 201 | — | |

The trunk profile references all eight VLANs, so all eight must exist first.

### 4. Rack template

One `RackTemplate` — "Audio Rack", VLANs 200/201/202.

**Decision needed:** whether racks get ranges on only the three audio VLANs or on all
eight. This is not cosmetic under ADR 0016, which gives a switch one address per rack
range: a rack carrying all eight VLANs produces switches with eight addresses, where
production has three. The export only ever exercises 200/201/202, so the template lists
those three, and any rack genuinely needing Lighting/AES67/Video/NDI ranges gets them added
explicitly.

### 5. Racks — 21 rows, in ascending offset order

Slot counts from real occupancy, with whatever headroom is wanted, capped at 30 (a `/27`
holds 30 usable ordinals; above that ADR 0015's floor gives way to the normal sizing and the
block becomes a `/26`, which would break the uniform 32-address spacing).

Under ADR 0017, a rack's `slot_count` must also cover `rack_slot + max(slot_offset)` for
every occupant — `CONSOLES` holds SD12s at slots 7 and 9 each spanning two ordinals, so 8
and 10 must be inside the count. Its highest occupied ordinal is 16 regardless, so this
binds nowhere in the current data, but the importer should assert it rather than assume.

19 racks are created from the template and take their bases automatically. `SHURE`
(`10.200.5.0/27`, `10.201.5.0/27`, `10.202.5.0/27`) and `CONSOLES` (`…6.0/27` on each)
are created with those three ranges entered explicitly.

### 6. Switch types — **blocked**

Five profiles from `Switch Ports.csv`, all needing information the export lacks:

- Profile **names** for all five (`(Manufacturer, Model, Name)` is the identity; the two
  Cisco SG300-10MP tables need distinct names — the analysis suggests they're a drive-rack
  and an amp-rack wiring).
- **Model** for the "Netgear Managed Switch" table.
- **Manufacturer and model** for the "Unmanaged Switch" table.
- Per-port `port_type` values. `Convertable to Fiber` → `1gbe_combo`; the rest need
  confirming as `1gbe_rj45` or otherwise.

Drop the trailing `Patch Panel 4 / Analogue Backup` row — it has no port number and is not
an ethernet port. `NetworkSwitchTypePort.port_number` is required and must form a contiguous
`1..port_count`, so it cannot be represented and shouldn't be.

`Unused` ports take the seeded system `Default` profile (see analysis §4).

### 7. Device types

Recoverable from the export:

| Type | Ports |
|---|---|
| Martin Audio IK-42 — with Dante Card | Control 200, Dante Pri 201, Dante Sec 202 |
| Martin Audio IK-81 | Control 200, Dante Pri 201, Dante Sec 202 |
| Lab.gruppen PLM20K | Control 200, Dante Pri 201, Dante Sec 202 |
| Lake LM44 | Control 200, Dante Pri 201, Dante Sec 202 |
| Lake LM26 | Control 200, Dante Pri 201, Dante Sec 202 |
| DiGiCo SD12 | Control 200 **offset 0**, Engine 200 **offset 1** |
| DiGiCo DMI-DANTE (card) | Dante Pri 201, Dante Sec 202 |
| Yamaha DM7 | Control 200 |
| Yamaha DM7-EX | Control 200 |

All ports at offset 0 except the SD12's engine. DM7 and DM7-EX stay two independent types —
their addresses are independent and need not be consecutive (ADR 0017's scope boundary).

Note that LM44/LM26 carry Control addresses in production but `DESIGN.md:142-144` gives them
Dante only. The table above follows production; `DESIGN.md` needs updating or the discrepancy
needs resolving.

**Blocked:** the AVIO rows (19 of them) and most console rows carry *hostnames*
(`mps-avio-na2-dline-1`, `mps-avio-radial-rx-2`), not manufacturers and models. At least
seven distinct product families are visible in the naming — Amphenol outputs, AVIO
input/output/AES/USB adapters, NA2-DLINE, Radial TX/RX — but the mapping from hostname to
`(manufacturer, model)` has to come from Mike.

**Also blocked:** SD7 engine count, and whether SD9/SD11 follow the SD-series control+engine
pattern. The SD9/SD11 answer changes the `CONSOLES` allocation rather than just adding a
type — if they need engines, SD9's would want `.6.12`, which SD11 currently holds.

### 8. Switches

One per "Primary Switch" / "Redundant Switch" / `mps-tlsg108e-*` / "Spare SG300-26" row, at
its rack and slot. Addresses materialize from the rack's ranges (ADR 0016) and should land
exactly on the sheet's values.

**Blocked on the same gap as §6:** nothing in any of the three files says which switch type
each rack uses. The addressing sheet says only "Primary Switch"; the ports file has no rack
column.

### 9. Devices

One per distinct `(rack, slot)`, static addressing (ADR 0013's default). Ports materialize
with `base + slot` addresses, and the SD12s with `base + slot` and `base + slot + 1`.

The DMI-DANTE cards get their own slots. **This re-addresses them:** as its own device at
its own ordinal, a card's Dante addresses become `base + N`, not the `10.201.6.7` /
`10.202.6.7` currently on the console's row — which were only ever an artifact of the
conflated row, and which the sheet itself annotates `Used as DMI-DANTE2 Addresses?`. Either
the cards are re-addressed on the hardware or those two values are hand-overridden (ADR 0003
permits it) at the cost of an ordinal that doesn't match the address. Decide before running,
not during. `CONSOLES` has room either way — 16 of 30 usable ordinals in use.

## Blockers, collected

| # | Blocker | Blocks |
|---|---|---|
| 1 | Switch profile names, plus Netgear model and unmanaged-switch manufacturer/model | §6, §8 |
| 2 | Rack↔switch-type mapping | §8 |
| 3 | AVIO and console hostname → `(manufacturer, model)` mapping | §7, §9 |
| 4 | SD9 / SD11 engine status | §5 slot counts, §7, §9 for `CONSOLES` |
| 5 | SD7 engine count | §7 for that one type |
| 6 | Real DHCP pool bounds | §2 |
| 7 | Whether racks carry 3 VLANs or 8 | §4, and every switch's address count |
| 8 | DMI-DANTE re-address vs. override | §9 |

1–3 block a complete import. 4–8 have safe defaults but change the result, so they should be
decided rather than defaulted silently.

## Verification

The export is its own test oracle, which is the best property this import has:

- **Address-for-address diff.** After the import, dump every `NetworkDevicePort.address` and
  `NetworkSwitchAddress.address` and compare against the CSV. All 259 assignments should
  match, and the diff should be empty except for rows deliberately changed (the DMI-DANTE
  cards, per §9, and anything resolved from §5's defects).
- **Rack bases.** All 21 `RackVlanRange` blocks on VLAN 200 should equal the offsets in
  `PROD-DATA-ANALYSIS.md` §1. This is the check that catches a creation-order mistake, and
  it should run before any equipment is created — a wrong base is much cheaper to find at
  stage 5 than at stage 9.
- **Cross-VLAN alignment.** Assert that every device's addresses share a host octet across
  200/201/202. Nothing in the system enforces this (analysis §6.1), so the import is the
  one moment it can be checked cheaply against a known-good answer.
- **Nothing ends `.255`**, and nothing lands in a DHCP range or on a gateway.

Whether the importer ships as a management command or as a one-off script is worth deciding
on its own merits. It runs once, but the verification steps above are worth keeping
runnable, and a command is easier to test than a script.
