# Plan: importing the production addressing data

**Revision 2.** Revised after a working session that cleared 9 of the 10 blockers and against
a corrected set of export CSVs. See `## Review response` at the end for what changed and why.
Where this revision contradicts the first, the first was wrong — those points are called out
in place rather than silently overwritten.

Design for loading the production CSVs (see `PROD-DATA-ANALYSIS.md`) into the app.
**Nothing here is built yet** — there is no CSV importer today, and the Django admin is the
only data-entry path. This document defines the sequence, the constraints that make the
sequence load-bearing, and the things that cannot be imported until someone supplies
information the export doesn't contain.

## Prerequisites — all three shipped

The import could not faithfully reproduce production until all three were implemented. All
three now are, and this revision assumes them:

- **[ADR 0015](docs/adr/0015-minimum-rack-block-size.md)** (#35) — without the `/27` floor,
  honest slot counts reproduce 1 of 21 rack bases instead of 19.
- **[ADR 0016](docs/adr/0016-switch-address-materialization.md)** (#36) — otherwise every
  switch address row is hand-entered. Confirmed correct as written against the corrected
  export: a managed switch holds an address on each of its rack's VLANs whether or not it is
  physically connected to that VLAN, so one address per `RackVlanRange` is right.
- **[ADR 0017](docs/adr/0017-derived-same-vlan-addresses.md)** (#39) — otherwise the DiGiCo
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
- **DHCP range `10.x.0.2`–`10.x.0.254` — declared, not observed.** This must be set
  **before** any rack range is allocated. It is what stops first-fit allocating inside the
  bottom `/24` and pushes the first rack block to `10.x.1.0/27`, reproducing production's
  offset of 256. Allocate ranges first and every base is 256 too low. The range starts at
  `.2` because ADR 0011 forbids it containing the gateway (`10.x.0.1`).

**The real pool is `10.x.0.2`–`10.x.0.100`. The recorded value is deliberately wider, and
this is a known departure from the field.**

Nothing in the network actually reserves `10.x.0.0/24`. The gateway uses `.0.1`, DHCP runs
to `.0.100`, and `.0.101`–`.0.255` is empty space that convention has kept clear.
`suggest_rack_vlan_range()` walks `/27` blocks in order and takes the first that overlaps
nothing, so with honest bounds it takes `10.200.0.128/27` and `CONTROL` lands at offset 128
— shifting all 21 racks and reproducing none of production's bases. Only a DHCP end at
`.224` or above blocks the last `/27` of the bottom `/24`; where the pool *starts* is
irrelevant to the search.

Measured, replaying the 19 automatically-allocated racks through the real suggester:

| Declared DHCP range | Result |
|---|---|
| `.2`–`.100` (true) | `10.200.0.128/27`, `.0.160/27`, `.0.192/27` … — 0 of 19 match |
| `.2`–`.253` | 19 of 19 production bases reproduced |
| `.2`–`.254` | 19 of 19 production bases reproduced |

`.253` and `.254` are equivalent; `.254` is recorded because it reads as the conventional
top-of-pool. Neither is near a broadcast address — the subnet is a `/21`, broadcasting at
`10.200.7.255`.

The correct fix is **Address Regions** (`ROADMAP.md`), which would let the bottom `/24` be a
declared reserved window and the DHCP range be recorded truthfully, with the bases still
reproducing. This plan does not block on that unbuilt design work; the departure is recorded
here and filed so it resurfaces when Regions land. It is the second concrete motivation for
that roadmap item — the first being the mnemonic offset gaps in `PROD-DATA-ANALYSIS.md` §7.2.

### 3. Switch port VLAN profiles

After VLANs (the FKs are `PROTECT`), before switch types.

| Profile | Mode | Native | Allowed | Notes |
|---|---|---|---|---|
| Audio Trunk | trunk | 201 | 200, 202, 207 | the primary-switch trunk |
| Audio Trunk Secondary | trunk | 202 | 200, 201, 207 | the secondary-switch trunk |
| Control Access | access | 200 | — | access forbids `all_vlans_allowed` and any allowed list |
| Dante Primary Access | access | 201 | — | |
| Dante Secondary Access | access | 202 | — | secondary switches' amp ports |

**The trunk is an audio trunk, not "all configured VLANs".** The corrected export gives every
trunk port Native `201`, Allowed `200, 202-207` — which excludes 100, 101, 220 and 221
(Lighting ×2, Video, NDI). An earlier revision of this plan proposed a single
`All Configured VLANs Trunk` carrying all eight; that was wrong, and the name is now taken
from the export's own `Audio Trunk` port descriptions.

`202-207` spans four VLAN IDs that do not exist — only 202 and 207 are real (§1 of the
analysis). `allowed_vlans` is a `PROTECT`-FK through model, so only real VLANs can be
recorded; the range on the switch is future-proofing and nothing operational is lost.

**The secondary profiles are derived, not exported.** The export carries one note against the
three primary tables: *"Secondary switch control ports are unused; ports with Native VLAN 201
become Native VLAN 202; Update Allowed VLANs list accordingly."* Applying it: amp Dante access
ports move 201→202, trunk ports move their native 201→202 and swap 201 into the allowed list
in place of 202, and the control ports **keep their `Control Access` configuration** — they
are unpatched, not unconfigured. That last point is confirmed, and it matters: it means a
secondary type differs from its primary by the 201→202 swap alone.

Note the consequence: the system-seeded `Default` profile ends up used by no port anywhere in
this import. Throughout this export `Unused` describes a port with nothing patched into it,
never an unconfigured one.

The profiles reference VLANs 200, 201, 202 and 207, all of which must exist first.

### 4. Rack template

One `RackTemplate` — "Audio Rack", VLANs 200/201/202.

**Three VLANs, not eight — settled.** Audio is the pilot, not the scope: all eight VLANs are
in scope on a phased rollout, with video adopting now and lighting later
(`PROD-DATA-ANALYSIS.md` §7.1). But today's 21 racks are *audio* racks, and this matters under
ADR 0016, which gives a switch one address per rack range — a rack carrying all eight VLANs
would produce switches with eight addresses where production has three.

So the "Audio Rack" template lists 200/201/202. Video racks arrive later behind a "Video Rack"
template, which is exactly the shape ADR 0014 anticipated and `DESIGN.md` already sketches. Any
rack that genuinely needs a Lighting/AES67/Video/NDI range gets it added explicitly. AES67 has
no gear yet, so it stays unexercised rather than unused.

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

**Verified.** Replaying the 19 template-allocated racks in ascending offset order through
`suggest_rack_vlan_range()`, with the declared DHCP range from §2, reproduces **19 of 19**
production bases exactly — `CONTROL` at offset 256 through `FLOATSWITCH` at 832. This is the
check that the §2 departure exists to buy, so it is worth running before anything else.

### 6. Switch types — seven types, one deferred

`NetworkSwitchType`'s identity is `(manufacturer, model, name)`, all three required and
non-blank, unique together (`unique_switch_type`) and immutable once any switch exists
(ADR 0010). `name` is guarded by its own CheckConstraint (`networkswitchtype_name_not_blank`,
`inventory/models.py:1955`), so "leave it blank" is not available — the field's help text
names the convention instead: *"Profile label, e.g. 'For Drive Rack', or 'Default' for a
single-profile model."*

The corrected export supplies the profile names in its own table headers, which is what
unblocked this section:

| Manufacturer | Model | Name | Ports | Instances |
|---|---|---|---|---|
| Cisco | SG300-10MP | For 3xAmp Rack Primary | 10 | 4 |
| Cisco | SG300-10MP | For 3xAmp Rack Secondary | 10 | 4 |
| Cisco | SG350-10 | For 2xAmp Rack Primary | 10 | 3 |
| Cisco | SG350-10 | For 2xAmp Rack Secondary | 10 | 3 |
| Cisco | SG300-10MP | For Drive Rack Primary | 10 | 2 |
| TP-Link | TL-SG108E | Default | 8 | 3 |
| Cisco | SG300-26P | Default | 26 | 1 |

Three SG300-10MP profiles share a manufacturer and model and are separated only by name,
which is exactly the case `unique_switch_type` exists for.

`port_type` comes from the export's own `Port Type` column: `1GbE Copper` → `1gbe_rj45`,
`Combo Port (1GbE + SFP)` → `1gbe_combo`. No inference required.

#### Two types not created, and why

- **Netgear (W8LM racks) — deferred, model unknown.** The export gives manufacturer and
  profile name (`Netgear Managed Switch (For W8LM Rack)`) but "Managed Switch" is not a model.
  `NetworkSwitch.switch_type` is a non-null FK (`:2159`), so **an untyped switch is
  impossible** — an earlier revision of this plan claimed "a rack with an unaddressed or
  untyped switch is legal", and that is wrong. A placeholder model is worse than waiting:
  ADR 0010 locks the type once instanced, so correcting it later means deleting all three
  switches and their nine materialized addresses and recreating them. The three W8LM racks,
  their ranges and their six LM26s all import now; the switches follow in a second pass.
- **Unmanaged Switch — not created.** It served only `XE300-1`/`XE300-2` slots 1–2, and those
  slots stay empty (§8). With no instances there is nothing to type, and the available
  strings are placeholders rather than a real manufacturer and model.
- **`For Drive Rack Secondary` — not created.** Both FOH Drive racks hold a slot-1 switch
  only, so the type would have no instances. Same reasoning as the unmanaged switch.

#### What the corrected export resolved

Four gaps in the previous revision closed at source rather than by decision:

1. **The secondary-switch layout exists** — as a derivation rule rather than a sixth table
   (§3). The previous revision was right to refuse to assume both switches were wired
   identically: a secondary switch's amp ports carry Dante **Secondary**, and an amp's
   secondary jack in a 201 access port would have been on the wrong subnet.
2. **The TP-Link TL-SG108E has a table** — 8 ports, all `Audio Trunk`.
3. **The `Spare SG300-26P` has a table** — 26 ports, and the model gains its `P` suffix.
4. **`CONSOLES` slots 2–3 are gone from the export**, deleted because those ordinals are
   genuinely unallocated. They were never switches with lost descriptions.

Also resolved at source: the trailing `Patch Panel 4 / Analogue Backup` row has been removed
from the export, so the previous revision's instruction to drop it during import is moot.
`NetworkSwitchTypePort.port_number` must still form a contiguous `1..port_count`, and every
table now does.

20 of the 23 addressed switches are typed and imported. The three W8LM Netgear switches are
the deferral above.

### 6a. Rack↔switch-type mapping — now stated, not inferred

**This is no longer a blocker.** Every switch row in the corrected addressing export carries
its own type string, so the mapping is read from the data rather than derived. The inference
below is kept because it independently reproduces what the export now states — which is the
best evidence available that the port tables and the addressing sheet describe the same site:

| Rack family | Composition | Table that fits, and why |
|---|---|---|
| WPC1SRU / 2SRL / 3SLU / 4SLL | Primary + Redundant switch, 3× IK42 | SG300-10MP #1 — **three** amp port-pairs |
| WPM1SR / 2SL / 3 | Primary + Redundant switch, IK81 + PLM20K | SG350-10 — **two** amp port-pairs, and its port-2 note "Not used by Lab amp" is confirmed: the PLM20K has no Control interface (`PROD-DATA-ANALYSIS.md` §5.1) |
| W8LM1SR / 2SL / 3 | Primary switch, 2× LM26 | Netgear — ports literally labelled "LM26 1 Dante" / "LM26 2 Dante", and its **total absence of Control ports** is correct, not an omission (§5.1) |
| FOH Drive #1 / #2 | Primary switch, LM44 | SG300-10MP #2 — has an explicit LM44 port |
| XE300-1 / 2 | 2× IK42, **slots 1–2 empty** | Unmanaged — an unmanaged switch has no IP, so it has no row in the addressing sheet |

The XE300 case is the useful one: those two empty slots aren't missing data, they're switches
that cannot be addressed by definition.

All five inferences match the export's own type strings exactly. The one thing the inference
could not have produced is the Primary/Secondary split — the sheet now distinguishes
`… Primary` from `… Secondary` per slot, which the amp counts alone never revealed.

### 7. Device types

The corrected export holds **64 device slots**, which become **63 `NetworkDevice` rows**: the
four SD12 rows collapse to two consoles under ADR 0017, and the DMI-DANTE card gains a slot of
its own (§9). Three tiers of recoverability, all now resolved.

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

**Tier 2 — resolved. 15 console rows.** The models read out of the hostnames:
`DM7C-1` → Yamaha DM7C, `SD12-96-1/2` → DiGiCo SD12, `SD9`/`SD11` → DiGiCo, `SQ5-1` → Allen
& Heath SQ-5, `bej-tio1608-d2-1` → Yamaha Tio1608-D2, `bej-dm3-1` → Yamaha DM3. Under ADR
0017 the four SD12 rows collapse to two devices.

**SD9 and SD11 take one address each — Control only, no Dante, no engine.** They do not
follow the SD-series control+engine pattern, so `CONSOLES` needs no rework and the feared
collision at `.6.12` cannot arise. **The SD7 is out of scope entirely** — there are none on
site — so the engine-count question that blocked its type is withdrawn rather than answered.

**The two `-device-control` rows are separate `NetworkDevice`s.** `dm7c-1-device-control` and
`bej-dm3-1-device-control` are the Yamaha "For Device Control" interface, which a console uses
to talk to its stage boxes for head-amp control. It sits on Dante Primary and is constrained
only to share that subnet while differing from the Mixer Control subnet — both of which the
VLAN model satisfies by construction, since a VLAN 201 port is in VLAN 201's subnet and a
VLAN 200 port is not.

That places it firmly on the "separate devices" side of ADR 0017's test — *does the hardware
compute the second address from the first and refuse to let anyone change it?* It does not;
the operator sets both. ADR 0017 in fact names these two rows explicitly. Two further facts
confirm it rather than merely permit it:

- **The addresses are hand-assigned, and the sheet proves it.** DM7C's device-control sits one
  *below* its console (`10.201.6.4` vs `.6.5`); DM3's sits one *above* (`.6.16` vs `.6.15`). A
  hardware-derived relationship would point the same way for both.
- **`slot_offset` cannot express it anyway.** Offsets are positive-only, so DM7C's layout would
  need the console moved to slot 4 — swapping two live addresses — or left at slot 5, where its
  two-ordinal span collides with `DM7-EX-1` at slot 6.

**`CONSOLES` slots 2–3 are not devices.** Those rows have been removed from the export; the
ordinals are genuinely unallocated (§6).

This is a shape the model does not have: two ports, one VLAN, two *independent* addresses.
`_check_static_materialization_possible()` groups by `(vlan_id, slot_offset)` and refuses any
group above one (`inventory/models.py:2995-3004`), so it sits between #27's shared-address
case and ADR 0017's derived case. Separate devices is the correct answer today; the gap is
filed for the record and costs this import two ordinals in a rack using 16 of 30.

**Tier 3 — resolved. 19 AVIO instances, 8 types.** These hostnames name a *function*, not a
product, so `(manufacturer, model)` could not be recovered from them and had to be supplied.
Every one takes the same single port — `Dante Primary` (VLAN 201), 1×1GbE copper:

| Manufacturer | Model | Hostname family | Count |
|---|---|---|---|
| Amphenol | RJD1212-0050 | `mps-avio-amph-output-1..4` | 4 |
| Amphenol | RJD2203-0050 | `mps-avio-avio-input-1/2` | 2 |
| Amphenol | RJD32A3-0050 | `mps-avio-avio-aes-io-1/2` | 2 |
| Amphenol | RJD32U1-0050 | `mps-avio-avio-usb-io-1/2` | 2 |
| Audinate | AVIO-AO2 | `mps-avio-avio-output-1/2` | 2 |
| Neutrik | NA2-DLINE | `mps-avio-na2-dline-1..3` | 3 |
| Radial | DiNET DAN-TX | `mps-avio-radial-tx` | 1 |
| Radial | DiNET DAN-RX | `mps-avio-radial-rx-1..3` | 3 |

All take `name = Default`. `DAN-TX` and `DAN-RX` are two models, not one — TX is an audio
input, RX an audio output.

**"AVIO" is a rack grouping, not a vendor.** These four manufacturers are unrelated; the
devices sit together because they are all portable adapters for getting signal into or out of
a Dante network. Reading the sheet's `AVIO-` hostname prefix or its `AVIO-AES`/`AVIO-USB`
model shorthands as Audinate products would have mis-attributed six of the nineteen.

**The port shape was never in doubt.** All 19 are single-RJ45 Dante adapters — one port each,
Dante Primary, 1×1GbE copper — which makes them the simplest types in the dataset. Their
Control addresses in the sheet are spurious and dropped (`PROD-DATA-ANALYSIS.md` §5.3).

Beyond tier 1's six, the console and card types, now complete:

| Manufacturer | Model | Name | Ports |
|---|---|---|---|
| DiGiCo | SD12 | Default | Control 200 **offset 0**, Engine 200 **offset 1** |
| DiGiCo | SD9 | Default | Control 200 |
| DiGiCo | SD11 | Default | Control 200 |
| DiGiCo | DMI-DANTE | Default | Dante Pri 201, Dante Sec 202 |
| Yamaha | DM7C | Default | Control 200, Dante Pri 201, Dante Sec 202 |
| Yamaha | DM7C | Device Control Interface | Dante Pri 201 |
| Yamaha | DM7-EX | Default | Control 200 |
| Yamaha | DM3 | Default | Control 200, Dante Pri 201, Dante Sec 202 |
| Yamaha | DM3 | Device Control Interface | Dante Pri 201 |
| Yamaha | Tio1608-D2 | Default | Control 200, Dante Pri 201, Dante Sec 202 |
| Allen & Heath | SQ-5 | Default | Control 200, Dante Pri 201, Dante Sec 202 |

All ports at offset 0 except the SD12's engine — the only `slot_offset` in the entire import.
DM7C and DM7-EX stay two independent types: their addresses are independent and need not be
consecutive (ADR 0017's scope boundary). The two `Device Control Interface` profiles are the
Yamaha second-interface case argued above; `(manufacturer, model, name)` carries the
distinction, which is what that identity is for.

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

**Nothing in §7 is blocked.** Tier 3's lookup is supplied above, SD9/SD11 are Control-only,
and the SD7 is out of scope.

**Hostnames come from the addressing export's `Device Description` column only.** A third
export, `Dante Devices.csv`, supplies the AVIO model strings but its own hostname and location
columns are incomplete and internally inconsistent with the addressing sheet — it disagrees on
AVIO hostnames (`-avio-input-` vs `-amph-input-`), swaps `W8LM1SR`/`W8LM2SL` against
`W8LM2SR`/`W8LM1SL`, omits `WPM3` entirely, and carries `devno` placeholder rows plus devices
with no addresses at all (`AVIO-USBC`, `AVIO-BT`, a Yamaha RIO). The addressing sheet wins on
every disagreement, and that file is not otherwise an import source. Computed hostnames are a
later ADR (#31).

### 8. Switches

**20 of the 23 addressed switches are imported**: 11 primaries, 7 secondaries, 3
`mps-tlsg108e-*`, and 1 `Spare SG300-26P` — each at its rack and slot, typed from the table in
§6. The three W8LM Netgear switches are deferred to a second pass pending their model (§6).

Addresses materialize from the rack's ranges (ADR 0016): **three per switch, one per rack
range**, so 60 rows across the 20.

**The export records only two addresses per switch, and that is not a contradiction.** A
managed switch holds an address on each of its rack's VLANs whether or not it is physically
connected to that VLAN; the export's blank third column records the patching, not the
addressing. So the import correctly places 20 addresses the sheet does not list — one per
switch — and §Verification accounts for them explicitly rather than treating them as
failures.

**The unmanaged switches in `XE300-1`/`XE300-2` slots 1–2 are not created.** They have no
addresses to record (§6a) and no type to carry (§6), and leaving the slots empty keeps the
import to what the addressing export actually describes. The cost is that those two racks
look emptier than they are.

**An untyped switch is not possible.** `NetworkSwitch.switch_type` is a non-null FK
(`inventory/models.py:2159`), so the previous revision's claim that "a rack with an
unaddressed or untyped switch is legal" was wrong on the second half. Unaddressed is legal;
untyped is not. This is why the Netgear deferral drops the switches entirely rather than
creating them bare.

### 9. Devices

One per distinct `(rack, slot)`, static addressing (ADR 0013's default). Ports materialize
with `base + slot` addresses, and the SD12s with `base + slot` and `base + slot + 1`.

The DMI-DANTE cards get their own slots. **This re-addresses them:** as its own device at
its own ordinal, a card's Dante addresses become `base + N`, not the `10.201.6.7` /
`10.202.6.7` currently on the console's row — which were only ever an artifact of the
conflated row, and which the sheet itself annotates `Used as DMI-DANTE2 Addresses?`.

**Decided: the cards are re-addressed on the hardware**, not hand-overridden. The import
places whatever `base + N` its ordinal gives, and the physical card is reconfigured to match
next time it is out of the console. This keeps ordinal and address in agreement and avoids a
permanent override describing nothing. It does mean the two addresses differ from the sheet
by design until the hardware catches up — filed so it is not forgotten. `CONSOLES` has room
either way: 16 of 30 usable ordinals in use, and slots 1–3 are free.

The two Yamaha `Device Control Interface` devices also take their own ordinals, at slots 4 and
16 where the export already has them — no re-addressing needed there, since the export's own
layout already treats them as separate occupants.

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
| 9 | DMI-DANTE re-address vs. override | decision | §9 |
| 10 | Per-device-type wiring rule (which switch port each device port patches to) | a dictated lookup | §9 — optional, but see below |

**Status: 9 of 10 cleared.** Only blocker 10 remains, and it is optional. See
`## Review response` for how each was answered and what changed as a result.

| # | Outcome |
|---|---|
| 1 | **Cleared.** Profile names supplied in the export's table headers; the unmanaged switch retired; the Netgear model deferred with its three switches (§6). |
| 2 | **Cleared.** Four manufacturers, eight models (§7 tier 3). |
| 3 | **Cleared at source.** The export gained TL-SG108E and SG300-26P tables and a secondary-switch derivation rule; `CONSOLES` 2–3 were deleted as unallocated (§6). |
| 4 | **Obsolete.** Every switch row now carries its own type string; the mapping is read, not inferred (§6a). |
| 5 | **Decided.** Separate `NetworkDevice`s (§7 tier 2). |
| 6 | **Answered.** SD9 and SD11 are Control-only; `CONSOLES` unchanged (§7 tier 2). |
| 7 | **Withdrawn.** No SD7 on site (§7 tier 2). |
| 8 | **Answered, then deliberately departed from.** Real pool is `.2`–`.100`; `.2`–`.254` is recorded so first-fit reproduces the rack bases (§2). |
| 9 | **Decided.** Re-address the cards on the hardware (§9). |
| 10 | **Open, optional.** Largely reconstructible from the port tables — see below. |

**Blocker 10 got much cheaper.** The port tables are themselves the wiring rule: `Amp 1
Control` → port 1, `Amp 1 Dante` → port 4, and so on. Since each rack's amps sit at
consecutive slots, "Amp 1/2/3" resolves to ordinals and the graph reconstructs for amps,
LM26s, the LM44 and the WAP. What it does not cover is anything patched through a patch panel
— consoles and AVIO adapters — which have no switch port to point at.

**Resolved before this list was first written** (`PROD-DATA-ANALYSIS.md` §7):

- *Whether racks carry 3 VLANs or 8* — audio is the pilot, not the scope. All eight VLANs are
  in scope on a phased rollout (video adopting now, lighting later), but today's racks are
  audio racks and should carry 200/201/202 only. Video racks arrive later with a "Video Rack"
  template. AES67 has no gear yet.
- *Which switch is the DHCP server* — by convention the drive-rack switches. Set
  `dhcp_server_enabled` on `FOH Drive #1` and `FOH Drive #2`'s primary switches (§7.7).

**Blocker 10 is the difference between importing addresses and importing the topology.** No
device-to-switch-port link exists in the export, but the wiring is identical across instances
of each device type, so a per-type rule plus slot position reconstructs the whole graph
(`PROD-DATA-ANALYSIS.md` §7.4). Skipping it leaves every `NetworkDevicePort.switch_port` null
— legal, and fillable later, but it is 63 devices × up to 3 ports of hand-entry to do
afterwards versus one dictated table now.

## Verification

The export is its own test oracle, which is the best property this import has:

- **Address-for-address diff.** After the import, dump every `NetworkDevicePort.address` and
  `NetworkSwitchAddress.address` and compare against the CSV. **These counts are recomputed
  against the corrected export** — the earlier revision's 259/229/195 figures were measured on
  the original sheet, which has since had its switch third column blanked and `CONSOLES` 2–3
  removed, and no longer hold.

  Devices, of 87 distinct occupied `(rack, slot)` pairs:

  | Figure | Count |
  |---|---|
  | Device address cells across 64 device slots | 157 |
  | …minus Lab.Gruppen Control addresses — no control interface exists (analysis §5.1) | 11 |
  | …minus `XE300-1`'s two IK-42 Dante addresses — no Dante card fitted (analysis §5.2) | 4 |
  | …minus AVIO Control addresses — single-port adapters (analysis §5.3) | 19 |
  | **Device addresses the import should place** | **123** |

  Switches: 20 imported × 3 rack ranges = **60**, against 40 cells in the sheet — the export
  records two per switch and the extra 20 are correct-but-unrecorded (§8).

  | Outcome | Count |
  |---|---|
  | **Total addresses placed** | **183** |
  | …byte-identical to a sheet cell | 161 |
  | …differing from the sheet by design (DMI-DANTE, §9) | 2 |
  | …correct but absent from the sheet (switch third address, §8) | 20 |

  So the pass condition is: **183 addresses placed, exactly 161 byte-identical, the 2
  differences on the DMI-DANTE list, and the 20 extras all switch addresses on a rack's third
  VLAN.** Anything else is a bug — the point of enumerating it this precisely is that "close
  enough" is not a check.

  A further 6 sheet cells belong to the three deferred W8LM switches and are out of scope for
  this pass; the `Used by BEJ for his gear` row is ignored entirely, having no rack or slot.

  Worth stating plainly, since it is the headline number people will remember: **34 device
  addresses in the sheet are dropped, not reproduced**, across the three findings in
  `PROD-DATA-ANALYSIS.md` §5.1–5.3. That figure is unchanged by the export's corrections,
  which is itself a useful check on the analysis. A faithful import is *supposed* to place
  materially fewer device addresses than the spreadsheet holds, and an import that reproduces
  all 157 has faithfully copied 34 mistakes.
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

## Review response

Revision 2. Each row is a question the first revision left open or got wrong, the answer, and
what moved as a result.

| # | Question | Answer | Changed |
|---|---|---|---|
| 1 | Switch profile names, Netgear model, unmanaged switch identity | Names supplied in the export's table headers. Netgear model unknown. | §6 — seven types listed; Netgear + its 3 switches deferred; unmanaged and `For Drive Rack Secondary` not created (no instances) |
| 2 | AVIO `(manufacturer, model)` | Four unrelated manufacturers: Amphenol, Audinate, Neutrik, Radial. "AVIO" is a rack grouping, not a vendor. | §7 tier 3 — 8 types, full part numbers |
| 3 | Four switch layouts missing from the export | Supplied at source: TL-SG108E and SG300-26P tables added, secondary switches given a derivation rule, `CONSOLES` 2–3 deleted as unallocated | §3, §6 |
| 4 | Confirm five rack↔switch-type mappings | Obsolete — every switch row now carries its type string | §6a reframed from inference to confirmation |
| 5 | `-device-control` rows: devices or interfaces? | Separate `NetworkDevice`s. Yamaha's Device Control address is operator-set, so ADR 0017's test excludes it. | §7 tier 2 — 4 Yamaha types; a modeling gap recorded |
| 6 | SD9 / SD11 engines | One Control address each, no engine | §7 — types added, `CONSOLES` unchanged |
| 7 | SD7 engine count | No SD7 on site | §7 — withdrawn |
| 8 | Real DHCP pool bounds | `.2`–`.100`; `.2`–`.254` recorded deliberately | §2 — departure documented with replay evidence; §5 gains 19/19 verification |
| 9 | DMI-DANTE re-address vs. override | Re-address on the hardware | §9 |
| 10 | Per-device-type wiring rule | Capture it — and the port tables already supply most of it | Blockers — reframed; still open for patch-panel-fed devices |

**Corrections to revision 1, called out because they were stated as fact:**

- *"A rack with an unaddressed or untyped switch is legal"* — only the first half is true.
  `NetworkSwitch.switch_type` is non-null (`inventory/models.py:2159`); untyped is impossible.
  This is why the Netgear deferral drops switches rather than creating them bare (§8).
- *"All Configured VLANs Trunk … allowed 100, 101, 200, 202, 207, 220, 221"* — the real trunk
  is an audio trunk (native 201, allowed 200/202/207) and excludes Lighting, Video and NDI
  (§3).
- *"`Unused` ports take the seeded system `Default` profile"* — in this export `Unused` means
  "nothing patched in", never "unconfigured". `Default` ends up used nowhere (§3).
- The 259/229/195 verification figures were measured against the original export and no longer
  hold; recomputed as 183/161 (§Verification).

**Escalated and resolved without changing an ADR:** the corrected export appeared to show two
addresses per switch against ADR 0016's one-per-rack-range. Confirmed to be a patching record,
not an addressing one — ADR 0016 stands unamended, and §8 accounts for the 20 unrecorded
addresses explicitly.

**Filed rather than solved,** as `deferred` issues: #41 DMI-DANTE hardware re-addressing, #42
two independent static addresses on one VLAN, #43 the declared DHCP range (revisit alongside
Address Regions), #44 untyped switches.

**Still open:** blocker 10 for patch-panel-fed devices, and the Netgear model.
