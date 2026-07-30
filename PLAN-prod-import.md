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

### 6. Switch types — **blocked on seven strings**

`NetworkSwitchType`'s identity is `(manufacturer, model, name)`, all three required and
non-blank, unique together (`unique_switch_type`). `Switch Ports.csv` gives five port tables
and satisfies that for none of them:

| Table | Layout | Missing |
|---|---|---|
| Cisco SG300-10MP | 3× amp Control, 3× amp Dante, 4× patch-panel trunk (2 fibre-convertible) | profile **name** |
| Cisco SG350-10 | 2× amp Control, 2× amp Dante, 4× PP trunk, 1 unused, 1 fibre trunk | profile **name** |
| Netgear Managed Switch | 2× LM26 Dante, 4 unused, 4× PP trunk | **model**, name |
| Unmanaged Switch | 2× amp Control, 2× amp Dante, 6× trunk | **manufacturer**, **model**, name |
| Cisco SG300-10MP | 1× WAP, 1× LM44, 2× PP Dante access, 1× PP Control access, 5× trunk | profile **name**, distinct from the first |

The two SG300-10MP tables are the hard stop: identical manufacturer and model, so without
distinct names they collide on the unique constraint.

**Seven strings unblock this** — five profile names, the Netgear model, and the unmanaged
switch's manufacturer and model. The layouts suggest the names strongly (`For Amp Rack
(3 Amps)`, `For Amp Rack (2 Amps)`, `For LM26 Rack`, `For Drive Rack`), but a profile name is
an operator-facing label inside a `PROTECT`ed identity that locks the moment a switch exists
— worth 60 seconds of confirmation rather than a guess.

Also needed, lower stakes: per-port `port_type` values. `Convertable to Fiber` →
`1gbe_combo`; the rest need confirming as `1gbe_rj45` or otherwise.

Drop the trailing `Patch Panel 4 / Analogue Backup` row — it has no port number and is not
an ethernet port. `NetworkSwitchTypePort.port_number` is required and must form a contiguous
`1..port_count`, so it cannot be represented and shouldn't be.

`Unused` ports take the seeded system `Default` profile (see analysis §4).

#### Four switch profiles the export doesn't contain at all

Separate from the naming problem, and more interesting: there are deployed switches with no
port table anywhere in the file.

1. **No redundant-switch layout exists.** Seven redundant switches are deployed (4 WPC +
   3 WPM). A redundant switch's amp ports carry Dante **Secondary**, but every table in the
   file shows Dante **Primary**. There is no sixth table. Either both switches in a rack are
   genuinely wired identically, or a table was never exported — worth checking a running
   config before assuming the former.
2. **The TP-Link SG108E has no table.** Three are addressed in `FLOATSWITCH`
   (`mps-tlsg108e-01/02/03`).
3. **`Spare SG300-26` has no table.** 26 ports, so neither 10-port SG300 profile fits.
4. **`CONSOLES` slots 2 and 3 are blank-description rows carrying all three VLANs** — which
   is exactly the Primary/Redundant Switch signature. They look like switches whose
   descriptions were lost, with slot 1 empty above them.

23 addressed switches need types. Roughly 19 are confidently placeable via §6a below; these
four cases are not.

### 6a. Rack↔switch-type mapping — mostly inferable

The amp and processor counts line up 1:1 with the port tables, so this is far less blocked
than it first appears:

| Rack family | Composition | Table that fits, and why |
|---|---|---|
| WPC1SRU / 2SRL / 3SLU / 4SLL | Primary + Redundant switch, 3× IK42 | SG300-10MP #1 — **three** amp port-pairs |
| WPM1SR / 2SL / 3 | Primary + Redundant switch, IK81 + PLM20K | SG350-10 — **two** amp port-pairs, and its port-2 note "Not used by Lab amp" is confirmed: the PLM20K has no Control interface (`PROD-DATA-ANALYSIS.md` §5.1) |
| W8LM1SR / 2SL / 3 | Primary switch, 2× LM26 | Netgear — ports literally labelled "LM26 1 Dante" / "LM26 2 Dante", and its **total absence of Control ports** is correct, not an omission (§5.1) |
| FOH Drive #1 / #2 | Primary switch, LM44 | SG300-10MP #2 — has an explicit LM44 port |
| XE300-1 / 2 | 2× IK42, **slots 1–2 empty** | Unmanaged — an unmanaged switch has no IP, so it has no row in the addressing sheet |

The XE300 case is the useful one: those two empty slots aren't missing data, they're switches
that cannot be addressed by definition.

Treat this table as a proposal to confirm, not a derived fact — but the residual work is
"confirm five mappings", not "supply a mapping that doesn't exist".

### 7. Device types

38 distinct descriptions across 66 device instances, in three tiers of recoverability.

**Tier 1 — unambiguous. 32 instances, 6 types.** Full `(manufacturer, model, name)` triples,
following the `Model — Name` convention `DESIGN.md:109` sets out. No guesses remain here:

| Manufacturer | Model | Name | Instances | Ports |
|---|---|---|---|---|
| Martin Audio | IK-42 | with Dante Card | 15 | Control 200, Dante Pri 201, Dante Sec 202 |
| Martin Audio | IK-42 | without Dante Card | 2 | Control 200 **only** |
| Martin Audio | IK-81 | with Dante Card | 4 | Control 200, Dante Pri 201, Dante Sec 202 |
| Lab.Gruppen | LM26 | Redundant Mode | 6 | Dante Pri 201, Dante Sec 202 |
| Lab.Gruppen | LM44 | Redundant Mode | 2 | Dante Pri 201, Dante Sec 202 |
| Lab.Gruppen | PLM20000Q | Redundant Mode | 3 | Dante Pri 201, Dante Sec 202 |

The Dante card is a genuine order-time option on every current-generation Martin Audio amp, so
`with Dante Card` / `without Dante Card` is a real profile distinction rather than a label —
which is exactly the `(manufacturer, model, name)` identity doing its job, and it is already
`DESIGN.md:131-136`'s worked example. It applies to the IK-81 too; all four of ours have cards.

**The two `without Dante Card` instances are `XE300-1` slots 3 and 4**, and their Dante
addresses in the sheet are spurious — four addresses allocated for consistency to interfaces
that don't exist. See `PROD-DATA-ANALYSIS.md` §5.2. This is the same class of finding as §5.1's
Lab.Gruppen Control addresses, arrived at from the opposite direction.

Note that `XE300-1` and `XE300-2` therefore hold *different* device types despite looking
identical in the sheet — `XE300-2`'s two IK-42s do have cards. The rack↔switch-type inference
in §6a is unaffected: both racks still fit the Unmanaged Switch table, with `XE300-1` simply
leaving its two Dante ports unused.

The Martin Audio amps have a real Control interface; the three Lab.Gruppen types do not, and
their 11 production Control addresses are dropped — see below.

The `PLM20K` shorthand is resolved: all three are **Lab.Gruppen PLM20000Q** power amps with
redundant Dante interfaces, so this is one type, not two. `Redundant Mode` as the profile
label follows `DESIGN.md`'s existing vocabulary for exactly this distinction (its Shure and
generic 2-port entries use `Redundant Mode` against `Switched Mode`), and it matches what the
data shows: both Dante addresses populated, one port per VLAN.

Note the manufacturer: **Lab.Gruppen** (that stylization, capital G) for all three, including
the LM-series processors. Lab.Gruppen bought the Lake processing technology from Dolby years
ago, so "Lake" names the DSP inside the box, not the company that makes it — calling an LM44
"a Lake" is shop shorthand. `DESIGN.md:142` used to read "Lake LM44 or Lake LM26", which named
a manufacturer that isn't one; corrected in this change, along with a note recording the
no-control-interface rule for the whole product line.

**Tier 2 — mostly inferable. 15 console rows.** The models read out of the hostnames:
`DM7C-1` → Yamaha DM7C, `SD12-96-1/2` → DiGiCo SD12, `SD9`/`SD11` → DiGiCo, `SQ5-1` → Allen
& Heath SQ-5, `bej-tio1608-d2-1` → Yamaha Tio1608-D2, `bej-dm3-1` → Yamaha DM3. Under ADR
0017 the four SD12 rows collapse to two devices. What is *not* inferable: whether the two
`-device-control` rows (`dm7c-1-device-control`, `bej-dm3-1-device-control`, both Dante
Primary only) are separate devices or second interfaces of their consoles, and what the two
blank-description rows at `CONSOLES` slots 2–3 are (see §6's fourth case — they look like
switches).

**Tier 3 — genuinely opaque. 19 AVIO instances, ~7 types.** These hostnames name a
*function*, not a product, so `(manufacturer, model)` cannot be recovered from them:

| Hostname family | Count | Needs |
|---|---|---|
| `mps-avio-amph-output-1..4` | 4 | manufacturer + model |
| `mps-avio-avio-output-1/2` | 2 | manufacturer + model |
| `mps-avio-avio-input-1/2` | 2 | manufacturer + model |
| `mps-avio-avio-aes-io-1/2` | 2 | manufacturer + model |
| `mps-avio-avio-usb-io-1/2` | 2 | manufacturer + model |
| `mps-avio-na2-dline-1..3` | 3 | manufacturer + model |
| `mps-avio-radial-tx`, `-rx-1..3` | 4 | manufacturer + model (one type or two?) |

All 19 share Control + Dante Primary, with no Dante Secondary. The naming hints at Audinate
AVIO adapters and Neutrik NA2-IO-DLINE, but guessing hardware models into a `PROTECT`ed
identity that locks once instanced is the wrong place to be clever. **This is a seven-row
lookup table someone can write in a few minutes with the rack in front of them** — the
cheapest of the three blockers to clear, and it unblocks 19 of the 66 devices.

Beyond tier 1's five, the console and card types the export supports:

| Manufacturer | Model | Name | Ports |
|---|---|---|---|
| DiGiCo | SD12 | Default | Control 200 **offset 0**, Engine 200 **offset 1** |
| DiGiCo | DMI-DANTE | Default | Dante Pri 201, Dante Sec 202 |
| Yamaha | DM7C | Default | Control 200, Dante Pri 201, Dante Sec 202 |
| Yamaha | DM7-EX | Default | Control 200 |

All ports at offset 0 except the SD12's engine. DM7C and DM7-EX stay two independent types —
their addresses are independent and need not be consecutive (ADR 0017's scope boundary). The
remaining tier-2 consoles (SD9, SD11, SQ-5, Tio1608-D2, DM3) need their profile labels and
their engine question settled before their types can be written down; see blockers 5 and 6.

**Lab.Gruppen devices take no Control port, and their 11 production Control addresses are not
imported** — control traffic rides both Dante ports on every Lab.Gruppen product, so there is
no control interface to address. `DESIGN.md:142-144` already had this right. See
`PROD-DATA-ANALYSIS.md` §5.1 for the eleven addresses and why the ports file was the reliable
source. All eleven run Redundant Mode (one port per VLAN), so they need no ADR 0017 offsets.

**Dante port configuration is an immutable property of the materialized device — decided, not
deferred.** Switched/Bridged versus Redundant lives in the `NetworkDeviceType`'s profile name
and port list, so it is fixed when the device is created and changing it means dropping the
device and adding a new one. That is a deliberate acceptance: this isn't something to change in
the field, and reconfiguring a rack is rare enough that drop-and-recreate is the cheaper answer
than making mode mutable.

It also needs no new machinery — it is precisely ADR 0010's existing rule that a device's Type
is immutable after creation and re-typing means remove-and-recreate, which `DESIGN.md:138`
already applies to adding or removing an amp's Dante card. Mode is one more instance of the
same pattern, not an exception to it.

Every Lab.Gruppen unit in production runs Redundant Mode — two Dante jacks, one per Dante VLAN,
one address each — which imports cleanly and needs no ADR 0017 offsets. Worth knowing what a
future Switched/Bridged type would run into: both jacks bridge into one logical interface on
Dante Primary sharing a single address, which is the #27 shape ADR 0013 refuses outright. So a
`LM26 — Switched Mode` type could not be tracked as a static device until #27 lands, though
DHCP would remain available. Nothing in production is in that state.

**Blocked:** tier 3's hostname → `(manufacturer, model)` lookup, above.

**Also blocked:** SD7 engine count, and whether SD9/SD11 follow the SD-series control+engine
pattern. The SD9/SD11 answer changes the `CONSOLES` allocation rather than just adding a
type — if they need engines, SD9's would want `.6.12`, which SD11 currently holds.

### 8. Switches

23 addressed switches: 12 "Primary Switch", 7 "Redundant Switch", 3 `mps-tlsg108e-*`, and
1 "Spare SG300-26" — each at its rack and slot. Addresses materialize from the rack's ranges
(ADR 0016) and should land exactly on the sheet's values.

Plus, not in the addressing sheet at all: the unmanaged switches in `XE300-1`/`XE300-2` slots
1–2, which have no addresses to record (§6a). Whether to create them as `NetworkSwitch` rows
with no `NetworkSwitchAddress` — legal, and more truthful about what's in the rack — or leave
those slots empty is a judgement call worth making deliberately.

**Type assignment:** the five rack families in §6a cover 19 of the 23. The remaining four are
the switches whose port layout the export never contained (§6's second half) — the redundant
switches' Dante-Secondary variant, the TP-Link SG108Es, and the SG300-26. A rack with an
unaddressed or untyped switch is legal, so the import can proceed without them and add them
once configs are available; it does not have to block on all 23.

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

Ordered by effort-to-clear, not by section:

| # | Blocker | Effort | Blocks |
|---|---|---|---|
| 1 | Seven strings: five switch profile names, the Netgear model, the unmanaged switch's manufacturer + model | minutes | §6, §8 — the largest unblock per keystroke |
| 2 | AVIO hostname → `(manufacturer, model)`: a seven-row lookup | minutes, rack access | §7 tier 3, §9 — 19 of 66 devices |
| 3 | Four switch profiles absent from the export: redundant-switch layout, TP-Link SG108E, SG300-26, and whatever `CONSOLES` slots 2–3 are | needs running configs | §6, §8 — 4+ of 23 switches |
| 4 | Confirm the five rack↔switch-type mappings proposed in §6a | review, not discovery | §8 |
| 5 | Are the two `-device-control` rows separate devices or second interfaces? | decision | §7 tier 2, §9 |
| 6 | SD9 / SD11 engine status | check the gear | §5 slot counts, §7, §9 — and reworks `CONSOLES` if yes |
| 7 | SD7 engine count | check the gear | §7, that one type only |
| 8 | Real DHCP pool bounds | check the DHCP server | §2 |
| 9 | Whether racks carry 3 VLANs or 8 | decision | §4, and every switch's address count |
| 10 | DMI-DANTE re-address vs. override | decision | §9 |

**1–3 are what actually block a complete import.** 4 is confirmation of work already done
here. 5–10 have safe defaults but change the result, so they want deciding rather than
defaulting silently.

A useful property: 1 and 2 together are perhaps ten minutes of someone's time and clear the
majority of the import surface. 3 is the only blocker needing real investigation, and it
concerns 4 switches out of 23 — the import could reasonably proceed without them and add
them later, since a rack with an unaddressed switch is legal.

## Verification

The export is its own test oracle, which is the best property this import has:

- **Address-for-address diff.** After the import, dump every `NetworkDevicePort.address` and
  `NetworkSwitchAddress.address` and compare against the CSV. Mind which figure you compare
  against — the sheet's raw assignment count double-counts the duplicated rows:

  | Figure | Count |
  |---|---|
  | Raw assignments as written in the sheet | 259 |
  | …minus the duplicate-row surplus (§2.2 of the analysis, 10 rows) | 30 |
  | **Distinct assignments across the 89 occupied slots** | **229** |
  | …minus Lab.Gruppen Control addresses — no control interface exists (analysis §5.1) | 11 |
  | …minus `XE300-1`'s two IK-42 Dante addresses — no Dante card fitted (analysis §5.2) | 4 |
  | **What the import should place** | **214** |

  Of those 214, the 2–4 DMI-DANTE card addresses will differ from the sheet by design (§9 —
  the card becomes its own device at its own ordinal). Row 103's two addresses appear in
  neither count, having no rack or slot to be counted against.

  So the pass condition is: **214 addresses placed, 210–212 of them byte-identical to the
  sheet, and every difference on the DMI-DANTE list.** Anything else is a bug — the point of
  enumerating it this precisely is that "close enough" is not a check.

  Worth stating plainly, since it is the headline number people will remember: **15 of the
  sheet's 229 distinct assignments are dropped, not reproduced** — 11 Lab.Gruppen Control and
  4 `XE300-1` Dante. A faithful import is *supposed* to place fewer addresses than the
  spreadsheet holds, and an import that reproduces all 229 has faithfully copied 15 mistakes.
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
