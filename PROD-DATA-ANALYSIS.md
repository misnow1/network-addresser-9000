# Production data analysis

Three CSVs exported from the live production spreadsheet were checked against the
addressing criteria this tool implements (`DESIGN.md`, `docs/adr/`). This document records
what the data says, whether the current system would address a site this way, and what the
gaps are.

The exports live in `prod/`, which is gitignored — production data isn't checked in — so
the figures and tables below are reproduced here in full rather than referenced by path.

**Headline:** the production data is not merely compatible with our model, it is generated
by the same formula the code implements. All **259** address assignments across **99**
equipment rows satisfy `rack_base + slot` exactly, on all three audio VLANs, with zero
deviations. Two rules production depends on are missing from the code, and one class of
production hardware can't be modeled at all. All three are now recorded as ADRs 0015–0017.

**The finding that outlived the validation, though, is one the arithmetic could never have
caught:** 34 of the sheet's 229 distinct addresses — 15% — are assigned to interfaces that
don't physically exist. Every one of them computes correctly. They are wrong about the
hardware, not the maths, and they are wrong in three unrelated-looking ways that turn out to
share one cause (§5.4).

## 1. What each file represents

### `IP Calc Lookups.csv` — the addressing scheme

Two independent tables sharing a sheet, side by side. Columns A–D are a VLAN table; columns
E–H are a rack table; column I is a `Notes` column serving **the rack table**, not the VLAN
table (see §2.4); column J (`Rack Increment`) is a single global constant, `32`.

Eight VLANs, all `/21` (`255.255.248.0`), all following the second-octet-equals-VLAN-ID
convention:

| VLAN | Function | Subnet |
|---|---|---|
| 100 | Lighting Control | `10.100.0.0/21` |
| 101 | Lighting Protocol | `10.101.0.0/21` |
| 200 | Audio Control | `10.200.0.0/21` |
| 201 | Dante Primary | `10.201.0.0/21` |
| 202 | Dante Secondary | `10.202.0.0/21` |
| 207 | AES67 | `10.207.0.0/21` |
| 220 | Video | `10.220.0.0/21` |
| 221 | NDI | `10.221.0.0/21` |

Twenty-one racks, each with a single `Address Offset` that is added to **every** VLAN's base
to give that rack's block on that VLAN. The sheet materializes only the Control and
Dante-Primary columns; Dante Secondary and the rest are implied by the same offset.

| Rack | Offset | Control base | Rack | Offset | Control base |
|---|---|---|---|---|---|
| CONTROL | 256 | `10.200.1.0` | W8LM3 | 640 | `10.200.2.128` |
| WPC1SRU | 288 | `10.200.1.32` | FOH Drive #1 | 672 | `10.200.2.160` |
| WPC2SRL | 320 | `10.200.1.64` | FOH Drive #2 | 704 | `10.200.2.192` |
| WPC3SLU | 352 | `10.200.1.96` | CDD | 736 | `10.200.2.224` |
| WPC4SLL | 384 | `10.200.1.128` | AVIO | 768 | `10.200.3.0` |
| WPM1SR | 416 | `10.200.1.160` | SPARE | 800 | `10.200.3.32` |
| WPM2SL | 448 | `10.200.1.192` | FLOATSWITCH | 832 | `10.200.3.64` |
| WPM3 | 480 | `10.200.1.224` | SHURE | 1280 | `10.200.5.0` |
| XE300-1 | 512 | `10.200.2.0` | CONSOLES | 1536 | `10.200.6.0` |
| XE300-2 | 544 | `10.200.2.32` | | | |
| W8LM1SR | 576 | `10.200.2.64` | | | |
| W8LM2SL | 608 | `10.200.2.96` | | | |

### `IP Addressing mk2.csv` — equipment assignments

102 data rows: 99 equipment rows, 2 blank spacers, and 1 unracked ad-hoc entry (§5). Each
equipment row carries a description, a rack, a slot, and up to three addresses — one each
for Audio Control, Dante Primary, and Dante Secondary. The 99 rows cover 89 distinct
`(rack, slot)` pairs; the 10-row surplus is discussed in §2.2.

### `Switch Ports.csv` — L2 port configuration

Five stacked per-model tables (description, port, VLAN, access/trunk mode, note). No
addresses anywhere — this file is purely switch port configuration, and maps onto
`NetworkSwitchType` / `NetworkSwitchTypePort` / `SwitchPortVlanProfile`. Details in §4.

## 2. Validation results

### 2.1 The addressing formula matches exactly

Every equipment row was recomputed from first principles as
`VLAN_base + rack_offset + slot` and compared against the sheet:

```
address assignments checked : 259
matching base + slot        : 259
mismatches                  : 0
```

That 259 is every assignment **as written**, including the duplicated rows of §2.2. Collapsing
those to one device per slot gives **229 distinct** assignments across the 89 occupied slots —
the figure that matters when checking an import, since 30 of the 259 are the same address
written more than once. `PLAN-prod-import.md`'s verification section works from 229.

This is `suggest_slot_address()` (`inventory/suggestions.py:73`) — `base + slot` — with the
rack base being `suggest_rack_vlan_range()`'s block. The lookups table is independently
consistent too: `VLAN base + Address Offset == stated rack base` holds for all 21 racks on
both the Control and Dante-Primary columns, no rack blocks overlap, and every block falls
inside its `/21`.

The host portion is identical across all three VLANs on every row — `10.200.2.161` /
`10.201.2.161` / `10.202.2.161`. That is a *guaranteed* property of the sheet's design (one
offset, applied to every VLAN base) and only a coincidental one in ours; see gap 6.1.

### 2.2 The duplicated rows are sheet residue

Eight `(rack, slot)` pairs carry more than one row, all with identical addresses:

| Rack | Slot | Rows | Addresses |
|---|---|---|---|
| FOH Drive #1 | 2 | LM44 ×2 | `.2.162` on all three VLANs |
| FOH Drive #2 | 2 | LM44 ×2 | `.2.194` |
| WPM1SR | 3 | IK81 ×3 | `.1.163` |
| WPM1SR | 4 | PLM20K ×2 | `.1.164` |
| WPM2SL | 3 | IK81 ×3 | `.1.195` |
| WPM2SL | 4 | PLM20K ×2 | `.1.196` |
| SPARE | 1 | IK81 ×2 | `.3.33` |
| SPARE | 2 | IK42 ×2 | `.3.34` |

Confirmed as residue from reshaping cycles the sheet has been through, not hardware. Each
group collapses to a single `NetworkDevice`. The 24 apparent duplicate-IP assignments this
produced are the same artifact and disappear with it — **production has no bridged-interface
(#27) problem.**

### 2.3 Nothing lands in the bottom `/24`

The lowest rack offset is 256, so `10.x.0.0`–`10.x.0.255` is untouched on every VLAN,
consistent with a DHCP pool living there (ADR 0002 / ADR 0011). The gateway convention
(`.1`, `suggest_default_gateway()`) is unobstructed.

### 2.4 Zero `.255` addresses, and the sheet's note is about racks

No address in the file ends in `.255`. The sheet annotates exactly two rows "Contains an
octet of all 1's, avoid .255", and those two rows are `WPM3` (block `10.200.1.224/27`) and
`CDD` (block `10.200.2.224/27`) — precisely the two racks whose `/27` block top is `x.255`.

That places the note on the **rack** column, not the VLAN column it sits beside: neither
NDI (`10.221.0.0/21`) nor anything else in the VLAN table has an all-ones octet. It's a
human remembering something `required_block_size()` already handles, by reserving each
block's top index — which, in a block that fits inside one `/24`, is the only address in it
that can end `.255`.

That guarantee turns out to be narrower than it reads, and the limit wasn't documented
anywhere before this review: a block spanning several `/24`s contains interior `.255`
addresses that are not its top index and *are* assignable to slots. It takes a rack of 255+
slots to get such a block, and nothing bounds `slot_count` above, so the hole is real but
unreachable in practice. Recorded in ADR 0015 rather than fixed — it's orthogonal to that
ADR's floor and no such rack exists here.

### 2.5 Slot occupancy

Eighteen of 21 racks hold equipment; `CONTROL`, `CDD`, and `SHURE` are empty. Occupancy
ranges from 2 to 19 slots. Collapsing the duplicate rows (§2.2), the 89 distinct slots hold
**23 addressed switches and 66 devices**.

Two gaps worth noting, with different explanations:

- **`XE300-1` and `XE300-2` start at slot 3**, slots 1–2 unoccupied where every other rack
  puts its switches. This is almost certainly not missing data: those racks hold 2× IK42
  each, fitting the ports file's "Unmanaged Switch" table (§5.2 notes that `XE300-1`'s two
  amps have no Dante card, so that rack leaves the table's two Dante ports unused), and an
  unmanaged switch
  has no IP — so it can't appear in an addressing sheet. See `PLAN-prod-import.md` §6a.
- **`CONSOLES` starts at slot 2 with slot 1 empty**, and slots 2–3 carry addresses with blank
  descriptions whose three-VLAN signature matches Primary/Redundant Switch. That one does
  look like lost data.

Rack-by-rack composition, and how it maps onto the five switch port tables, is tabulated in
`PLAN-prod-import.md` §6a — that mapping turns out to be largely derivable from amp and
processor counts rather than needing to be supplied.

## 3. Would the current system address a site this way?

| Rule | Verdict |
|---|---|
| Device address = rack block base + slot | **Yes, exactly.** 259/259. |
| Rack blocks packed sequentially within a VLAN subnet | **Yes.** First-fit over aligned blocks reproduces production's packing. |
| `/21` VLAN sizing, second octet = VLAN ID | **Yes.** ADR 0002; all 8 VLANs conform. |
| Bottom `/24` left for DHCP | **Yes.** ADR 0011's DHCP range pushes the first block to `10.x.1.0`. |
| No address ends `.255` | **Yes, structurally** — for any block of `/24` or smaller, which is every realistic rack. See §2.4. |
| Uniform 32-address block per rack | **Only with ADR 0015's `/27` floor.** Without it, honest slot counts give `/30`/`/29` blocks. |
| Same host octet across all VLANs | **Coincidentally only.** Not enforced; see gap 6.1. |
| DiGiCo console control + engine addressing | **No — refused outright** before ADR 0017. |
| Switch addressed on several VLANs | **Yes, but entirely by hand** before ADR 0016. |

Replaying the 21 production racks through the suggester at their real occupancy, with DHCP
in the bottom `/24`:

- **Current code:** 1 of 21 rack bases reproduced.
- **With ADR 0015's `/27` floor:** 19 of 21, automatically.
- **With the floor, plus `SHURE` and `CONSOLES` entered by hand:** 21 of 21.

Those two racks sit behind deliberate reservations — `FLOATSWITCH` at 832 is followed by
`SHURE` at 1280 (a 14-increment gap) and `CONSOLES` at 1536 (a further 8). A first-fit
suggester never leaves a gap, so manual entry is correct here and is exactly what ADR 0001
provides for.

## 4. The switch ports file

Five port tables. The `(Manufacturer, Model, Name)` type-profile identity (ADR 0010) handles
them, including the two that are the same hardware wired differently — but the sheet names
none of the profiles:

| Table | Layout | Blocker |
|---|---|---|
| Cisco SG300-10MP (1st) | 3× Control access, 3× Dante-P access, 4× trunk | needs a profile name |
| Cisco SG350-10 | 2× Control, 2× Dante-P, 4× trunk, 1 unused, 1 fibre trunk | needs a profile name |
| Netgear Managed Switch | 2× Dante-P, 4 unused, 4× trunk | **model unknown**; the field is non-blank |
| Unmanaged Switch | 2× Control, 2× Dante-P, 6× trunk | **no manufacturer or model**; also assigns per-port VLANs, which unmanaged hardware cannot do — this table documents intended patching, not switch config |
| Cisco SG300-10MP (2nd) | 4× Dante-P access, 1× Control access, 5× trunk | needs a name distinct from the 1st |

`Convertable to Fiber` maps cleanly onto the `1gbe_combo` `port_type` choice.

**Trunk ports.** The file leaves the VLAN column blank on every trunk row. The actual
running config resolves it:

```
interface gigabitethernet10
 description "Trunk Panel"
 switchport trunk native vlan 201
 switchport trunk allowed vlan add 100-101,200,202,207,220-221
 switchport default-vlan tagged
```

That is an **explicit list of every configured VLAN** — native 201 plus the other seven —
not `all_vlans_allowed`. So the trunk rows map to one new `SwitchPortVlanProfile` (trunk,
native VLAN 201, `allowed_vlans` = the remaining seven), *not* to the seeded system
`Default` profile (native VLAN 1, `all_vlans_allowed=True`).

This is ADR 0012's live-reference decision earning its keep against real config: adding a
future site VLAN to that single profile reaches every trunk port immediately, which is the
exact behaviour ADR 0012 departed from the materialization pattern to get.

Two minor representation gaps this file exposes:

- **`switchport default-vlan tagged` has no equivalent.** `SwitchPortVlanProfile` has no
  native-VLAN-tagged flag. It changes generated switch config, so it matters for anyone
  using this tool to write configs.
- **No administratively-down port state.** `port_mode` is trunk-or-access, so the file's
  blank-mode `Unused` ports still need *some* profile. The seeded system `Default` is the
  least-wrong choice; a dedicated `Unused` profile would be cosmetic.

**On the rack↔switch-type mapping:** no file states it — the addressing sheet says only
"Primary Switch"/"Redundant Switch", and the ports file has no rack column — but it is
largely *derivable*. Each table's amp or processor port count matches exactly one rack
family's composition (3-amp tables to the `WPC*` racks with 3× IK42, the "LM26 1/2 Dante"
table to the `W8LM*` racks, and so on). `PLAN-prod-import.md` §6a tabulates all five with the
evidence for each. What genuinely can't be derived is the four switch profiles missing from
the file altogether — see §5.

## 5. Production data defects

Independent of any tooling decision, these need resolving in the source data:

- **The unracked ad-hoc row.** `Used by BEJ for his gear` has no rack and no slot,
  `10.200.3.192` in the Control column, and `10.201.4.192` in the **Dante Secondary**
  column despite being a Dante *Primary* subnet address. Both addresses fall inside no
  rack's block (`FLOATSWITCH` ends at `10.200.3.95`; offsets 960 and 1216 are unallocated).
  Two independent errors in one row.
- **The `SD12-96-1-Control` row conflates two devices.** Its `10.201.6.7` / `10.202.6.7`
  belong to the DMI-DANTE card, not the console — an SD12 has no built-in Dante interface.
  The row's own `Used as DMI-DANTE2 Addresses?` annotation is the field note that flagged it.
  `SD12-96-2` showing Control only is the same issue from the other side: its card either
  isn't fitted or wasn't recorded.
- **`-Control` / `-Engine` row pairs are a workaround, not data.** Under ADR 0017 each SD12
  becomes one device spanning two ordinals. The addresses don't move — `.6.7`/`.6.8` and
  `.6.9`/`.6.10` are exactly what the offset formula produces — so this is a representation
  change with no re-addressing.
- **`CONSOLES` slots 2 and 3 hold addresses with blank descriptions**, and slot 1 is empty
  while every other rack puts its primary switch there.
- **`Rack Increment` carries a stray `0`** on the `CDD` row where every other populated cell
  reads `32`, and `CDD`→`AVIO` is a normal +32. Spreadsheet residue.
- **Eleven Control addresses are allocated to interfaces that don't exist** — see §5.1.
- **Three racks hold no equipment** (`CONTROL`, `CDD`, `SHURE`). Legal — a rack with ranges
  and no occupants is fine — but worth confirming they're reservations rather than
  leftovers. `SHURE` in particular sits behind a deliberate 14-increment gap.
- **SD9 / SD11 engine status is unconfirmed**, and it isn't cosmetic. If they follow the
  SD-series control+engine pattern, SD9 at slot 11 needs an engine at `.6.12`, which SD11
  already holds — that would mean reworking the `CONSOLES` allocation rather than importing
  it.

### 5.1 Lab.Gruppen devices have no Control interface — 11 spurious addresses

Two apparent contradictions between the addressing sheet and the ports sheet turned out to be
the same fact, and the ports sheet is the one telling the truth:

- The Netgear table has **no Control-VLAN ports at all** — 2× Dante Primary access, 4 unused,
  4 trunk — yet all six `LM26`s in the `W8LM*` racks carry Control addresses.
- The SG350 table marks port 2 **"Not used by Lab amp"**, yet all three `PLM20K`s carry
  Control addresses.

**Every Lab.Gruppen product behaves the same way**: control traffic rides on *both* Dante
ports and addresses, whether the device is in Switched/Bridged or Redundant mode. There is no
dedicated control interface and no control network involvement at all — only the Dante
address(es) are used. The Control addresses in the sheet were allocated for convenience, not
because anything consumes them.

That covers the LM-series processors as well as the amps: **Lab.Gruppen** (that stylization)
is the manufacturer of all of them. Lab.Gruppen acquired the Lake processing technology from
Dolby years ago, so "Lake" is the DSP inside the unit rather than the company — referring to
an LM44 as "a Lake" is shop shorthand. `DESIGN.md:142` named "Lake" as the manufacturer and is
corrected in this change, which also records the no-control-interface rule for the whole
product line where the device examples live.

The sheet's other shorthand is resolved too: `PLM20K` means the **Lab.Gruppen PLM20000Q** power
amp with redundant Dante interfaces — not the newer PLM20K44, which the shorthand would equally
have fitted. All three instances are the same model, so it's one device type. "Redundant" here
is the port shape the data already showed (both Dante addresses populated, one per VLAN) and
becomes the type's profile label, following `DESIGN.md:109`'s `Model — Name` convention.

So the port tables are complete and correct, and **`DESIGN.md:142-144` was right all along** —
it gives `LM44`/`LM26` Dante Primary + Secondary and no Control port. An earlier draft of this
document flagged that as a possible `DESIGN.md` staleness; it isn't. The production sheet is
what's carrying extra data.

Eleven device instances are affected, and all eleven of their Control addresses should be
omitted on import:

| Device | Rack | Slot | Control (spurious) | Dante Primary | Dante Secondary |
|---|---|---|---|---|---|
| LM44 | FOH Drive #1 | 2 | `10.200.2.162` | `10.201.2.162` | `10.202.2.162` |
| LM44 | FOH Drive #2 | 2 | `10.200.2.194` | `10.201.2.194` | `10.202.2.194` |
| LM26 | W8LM1SR | 2 | `10.200.2.66` | `10.201.2.66` | `10.202.2.66` |
| LM26 | W8LM1SR | 3 | `10.200.2.67` | `10.201.2.67` | `10.202.2.67` |
| LM26 | W8LM2SL | 2 | `10.200.2.98` | `10.201.2.98` | `10.202.2.98` |
| LM26 | W8LM2SL | 3 | `10.200.2.99` | `10.201.2.99` | `10.202.2.99` |
| LM26 | W8LM3 | 2 | `10.200.2.130` | `10.201.2.130` | `10.202.2.130` |
| LM26 | W8LM3 | 3 | `10.200.2.131` | `10.201.2.131` | `10.202.2.131` |
| PLM20K | WPM1SR | 4 | `10.200.1.164` | `10.201.1.164` | `10.202.1.164` |
| PLM20K | WPM2SL | 4 | `10.200.1.196` | `10.201.1.196` | `10.202.1.196` |
| PLM20K | WPM3 | 4 | `10.200.1.228` | `10.201.1.228` | `10.202.1.228` |

All eleven have both Dante addresses populated — one port per VLAN, i.e. **Redundant mode**.
That matters: Switched/Bridged mode would put both ports on Dante Primary sharing one address,
which is the #27 bridged-interface shape ADR 0013 refuses. Production runs these Redundant, so
they import cleanly. Nothing here needs ADR 0017's offsets either — two ports, two VLANs, one
address each.

Note that the racks still need their Control ranges: the `W8LM*` and `WPM*` racks all hold
switches, which *do* carry Control addresses. Only the 11 device-level allocations go away.

**This is the tool earning its keep, not just recording data.** A spreadsheet computes an
address per row per column and has no way to express "this model has no control interface," so
the shortcut was invisible and free. In the app that fact lives in exactly one place — the
device type's port list — and materialization then *cannot* allocate a Control address to a
device whose type has no Control port. The 11 addresses aren't merely cleaned up on import;
the class of error becomes unrepresentable.

### 5.2 Two IK-42s have no Dante card — 4 more spurious addresses

The mirror image of §5.1, found from the opposite direction. The Dante card is an order-time
option on every current-generation Martin Audio amp, and **two of the IK-42s don't have one:
`XE300-1` slots 3 and 4.** Addresses were allocated across all three VLANs anyway, for
consistency with every other amp row.

| Device | Rack | Slot | Control | Dante Primary (spurious) | Dante Secondary (spurious) |
|---|---|---|---|---|---|
| IK42 | XE300-1 | 3 | `10.200.2.3` | `10.201.2.3` | `10.202.2.3` |
| IK42 | XE300-1 | 4 | `10.200.2.4` | `10.201.2.4` | `10.202.2.4` |

Four addresses to omit on import. `XE300-2`'s two IK-42s (slots 3 and 4, `…2.35` / `…2.36`) do
have cards and keep all three.

`DESIGN.md:131-136` already models both variants — "Martin Audio IK-42 with Dante Card"
(Control + Dante Primary + Dante Secondary) against "without Dante Card" (Control only) — so
this needs no new modeling, just the right type per instance. It does mean `XE300-1` and
`XE300-2` hold **different device types** despite being indistinguishable in the sheet.

### 5.3 The 19 AVIO devices are single-port — 19 more spurious addresses

The largest instance of the same error. Every device in the `AVIO` rack is a single-RJ45 Dante
adapter — one interface, on Dante Primary. The sheet gives all 19 a Control address as well.

| Family | Count | Slots |
|---|---|---|
| `mps-avio-amph-output-1..4` | 4 | 1, 2, 3, 15 |
| `mps-avio-avio-output-1/2` | 2 | 4, 5 |
| `mps-avio-avio-input-1/2` | 2 | 6, 7 |
| `mps-avio-avio-aes-io-1/2` | 2 | 8, 9 |
| `mps-avio-avio-usb-io-1/2` | 2 | 10, 11 |
| `mps-avio-na2-dline-1..3` | 3 | 12, 13, 14 |
| `mps-avio-radial-tx`, `-rx-1..3` | 4 | 16, 17, 18, 19 |

All 19 Control addresses (`10.200.3.1`–`10.200.3.19`) are dropped on import; the Dante Primary
addresses (`10.201.3.1`–`10.201.3.19`) are correct and kept. Their device types are the
simplest in the whole dataset — one port each, Dante Primary, 1×1GbE copper — which narrows the
tier-3 blocker in `PLAN-prod-import.md` §7 to identity strings only, since the port shape is now
known.

### 5.4 The three together: 34 addresses, one root cause

| Finding | Addresses | Cause |
|---|---|---|
| §5.1 Lab.Gruppen Control | 11 | control rides both Dante ports; no control interface exists |
| §5.2 `XE300-1` IK-42 Dante | 4 | no Dante card fitted |
| §5.3 AVIO Control | 19 | single-port adapters |
| **Total** | **34** | |

**34 of the sheet's 229 distinct assignments — 15% — are allocated to interfaces that do not
physically exist.** Not one of them is an arithmetic error: every single one satisfies
`base + slot` perfectly, which is exactly why §2.1's clean validation result could never have
caught them. They are all the same mistake in three costumes — a port list built by hand,
column by column, with no way for the sheet to know which ports a model actually has.

This is the sharpest available argument for what the tool is for. A spreadsheet computes a value
per row per column; asserting "this model has no control interface" isn't something it can
express, so allocating the address anyway is free and invisible. In the app that fact lives in
exactly one place — the device type's port list — and materialization then *cannot* produce an
address for a port the type doesn't declare. The 34 addresses aren't merely cleaned up during
import; the mechanism that generated them stops existing.

It is also what motivates the addressing-mode refactor now parked in `ROADMAP.md`'s "Later"
section: a device type whose ports are chosen from a short list of real hardware shapes
(Control yes/no, plus Dante none/redundant/switched/single) is harder to get wrong than one
assembled a port at a time, and all 34 of these would have been unrepresentable under it.

### Switches with no port configuration recorded

Four switch profiles are deployed but absent from the ports file entirely — detailed in
`PLAN-prod-import.md` §6. Briefly: no redundant-switch layout exists anywhere (7 are
deployed, and their amp ports should carry Dante Secondary where every table shows Dante
Primary); the three `FLOATSWITCH` TP-Link SG108Es have no table; `Spare SG300-26` has no
table; and `CONSOLES` slots 2–3 are blank-description rows whose three-VLAN signature matches
Primary/Redundant Switch exactly, above an empty slot 1.

## 6. Remaining gap, not addressed by any ADR

### 6.1 Cross-VLAN host alignment is coincidental, not enforced

The sheet's single `Address Offset` per rack *guarantees* that a device's Control, Dante
Primary, and Dante Secondary addresses share a host portion. We allocate per `(rack, VLAN)`
by independent first-fit, so that alignment holds only while every VLAN has identical
sibling ranges, identical DHCP geometry, and identical creation order. One VLAN with a DHCP
range its neighbours lack, one rack deleted and recreated, or one hand-entered range on a
single VLAN, and the host octets silently diverge. Nothing detects it.

Rack Templates (ADR 0014) make divergence *less* likely by allocating all of a rack's VLANs
in one ordered pass, and ADR 0015's `/27` floor helps again by removing the per-VLAN
block-size variation that made first-fit results diverge most easily. Neither closes the
gap, and neither should be presented as if it did.

A real fix means allocating a rack's offset once across all its VLANs — a materially
different allocation model from ADR 0001's per-VLAN one. It is parked in `ROADMAP.md`'s
"Later" section with the recommended approach; the two points below are what that section
depends on.

#### What is actually aligned: the offset, not the third octet

It is tempting to describe the invariant as "every device in a rack shares a third octet",
because that is what the production data looks like. It isn't the rule. **16 of the 21 racks
don't start on a `/24` boundary** — six of them share third octet `1` — and an offset like
`WPC1SRU`'s 288 spans two octets (`1`×256 + `32`). The invariant is *the rack block's offset
from its VLAN's network address*, which happens to look octet-shaped only because `/21`
subnets and 32-address blocks keep each rack inside a single `/24`. An offset-based rule
survives a VLAN that isn't a `/21`; a third-octet rule does not.

#### The guarantee covers static addresses only

Alignment is a property of **rack VLAN ranges**, and therefore of the static addresses
computed from them. DHCP-assigned interfaces are outside it entirely: the only promises for
those are the VLAN's subnet and the DHCP pool the server is configured with.

Structurally this needs no special handling — a DHCP port has no address to align, since the
`device_port_dhcp_xor_static_address` constraint forces `address = NULL` when `is_dhcp` is
set. But it does mean a device can legitimately be *partly* aligned, and two ordinary paths
produce that: `is_dhcp` is editable per port after creation (one of the three fields that
stays mutable), and ADR 0013 decision 4 always materializes an L2-only-VLAN port as DHCP even
under a static choice. So "this rack is `.2.16x` on everything" is a statement about its
static ports, and any misalignment report must consider only those or it will flag every
mixed device as broken.

Worth recording alongside it: the DHCP range on a VLAN is **declarative**. It records what an
external server is configured to hand out (ADR 0011 made it a manual start/end pair for
exactly that reason); nothing in this tool configures or reads the real server. The tool
therefore guarantees one direction only — it will not allocate a static address inside the
declared pool — while a server whose real pool is wider than the recorded one can hand out an
address inside a rack block, and nothing here would detect it. That asymmetry is inherent to
tracking rather than controlling DHCP, not a gap this project introduced.

## 7. Deployment context

Answers to the questions this review raised, recorded because several of them change how the
data should be read and none of them are recoverable from the files.

### 7.1 Audio is the pilot, not the scope

All eight VLANs are in scope. **Audio went first as the guinea pig**; video is beginning to
adopt the same model and lighting will follow. So the five VLANs with no equipment are
*future*, not out-of-scope, and the sharp edges audio is finding now are being found on
everyone's behalf.

Two consequences worth holding onto. First, this dataset is a fraction of the eventual
inventory, so anything that works only because there are 21 racks and three VLANs should be
treated with suspicion. Second, it strengthens the case for Rack Templates carrying purpose
(ADR 0014) — "Audio Rack" and "Video Rack" seeding different VLAN sets is exactly the shape a
phased rollout produces, and `DESIGN.md` already sketches both.

**AES67** is the exception among the five: it is an audio VLAN, but no AES67 gear exists yet.
It is nominally Dante-compatible (in AES67 mode) with its own distinct sharp edges, so treat
its absence as "not yet exercised" rather than "not used".

### 7.2 The offset gaps are mnemonic, not technical

The two interior gaps — offsets 864–1279 before `SHURE`, and 1312–1535 before `CONSOLES` —
have **no technical purpose**. They exist so an address can be identified by eye in the field:
"that's an amp rack address", "that's a Shure receiver address". It is the same instinct as
putting the VLAN ID in the second octet, applied to the host part, and it doubles as
error-detection — something in the wrong region is visibly in the wrong region.

Two things follow, and they pull in opposite directions:

- **The suggester will consume these gaps.** First-fit takes the lowest free offset, and the
  aligned allocation chosen in `ROADMAP.md` doesn't change that. The next rack created lands at
  offset 864, inside the region reserved by eye for wireless. There is no "reserved but
  unallocated" concept. Today this is prevented only by offsets being chosen by hand.
- **Choosing offsets by hand is itself the problem.** It is error-prone, and overlapping rack
  ranges are easy to create by accident — which is precisely what this tool exists to stop.

So the mnemonic property and the automation are in direct tension, and resolving it needs
somewhere to declare the regions. That is recorded as a design item in `ROADMAP.md`'s "Later"
section (**address regions**), because the obvious fix — several declared ranges per VLAN — is
more tractable than it first appears and would deliver the mnemonic grouping *and* remove the
hand-calculation that causes the overlaps.

### 7.3 The second-octet convention is mnemonic too, and has a known ceiling

VLAN IDs validate 1–4094; an octet holds 0–255, so the convention cannot survive a VLAN above
255. The sketched extension is to use a different `/21` inside the same second octet — VLAN
1200 as `10.200.8.0/21`, adjacent to VLAN 200's `10.200.0.0/21`. Nothing in the tool enforces
the convention either way, so this works today; it is recorded so the ceiling isn't discovered
by surprise. Aligned allocation is unaffected, since offsets are measured from each VLAN's own
network address.

### 7.4 The connection graph is derivable, not lost

No device-to-switch-port link is recorded anywhere in the export (§4), but **the wiring is
identical across instances of each device type** — every IK-42 in a WPC rack is patched the
same way — so the graph can be reconstructed from a per-type rule plus slot position, rather
than entered 66 devices at a time. The ports file already hints at it: ports labelled "Amp 1
Control" / "Amp 1 Dante" against racks holding three IK-42s at slots 3/4/5.

This is a lookup table someone can dictate, and it is the difference between the import
delivering addresses only and delivering the topology as well.

### 7.5 The spare pool should be used, and currently isn't

Nothing in production is unracked; `SPARE` is an ordinary rack holding statically addressed
equipment. The spare pool as `CONTEXT.md` defines it — unracked, DHCP-configured, tracked by
serial and hostname — is a real intent that current practice doesn't yet follow. Recorded as
an intended-use gap rather than a data defect: the model already supports it, so this is a
process change more than a code one.

### 7.6 Hostnames should be generated

25 of 89 slots carry a real hostname; the rest hold model names like `IK42`. The production
convention is visible in the ones that exist (`mps-tlsg108e-01`, `mps-avio-na2-dline-1`, and
`DESIGN.md`'s `mps-sg300-wpc-sr-upper`). The intent is for the tool to compute these rather
than have them typed — that is issue #31, wanted but not urgent, and the existing hostnames are
the best available input for its pattern.

### 7.7 The drive-rack switches are the DHCP servers

By convention the switches in the drive racks (`FOH Drive #1`, `FOH Drive #2`) serve DHCP for
their networks. Nothing in the export records it, but `NetworkSwitch.dhcp_server_enabled`
exists for exactly this, so the import can set it on those two switches.

## 8. Outcome

| Finding | Disposition |
|---|---|
| `base + slot` matches production exactly | No action — the model is right |
| Uniform 32-address rack blocks | [ADR 0015](docs/adr/0015-minimum-rack-block-size.md) — `/27` floor |
| Switch addresses entirely hand-entered | [ADR 0016](docs/adr/0016-switch-address-materialization.md) |
| DiGiCo console control + engine addressing | [ADR 0017](docs/adr/0017-derived-same-vlan-addresses.md) |
| `.255` avoidance only holds up to `/24` blocks | §2.4 — newly documented in ADR 0015, unreachable in practice, not fixed |
| Cross-VLAN alignment is coincidental | §6.1 — approach chosen: allocate the rack's offset once, across all its VLANs. Suggest it, don't enforce it. Parked in `ROADMAP.md` "Later" |
| `default-vlan tagged`, unused-port state | §4 — noted, minor |
| **34 addresses (15%) assigned to interfaces that don't exist** | §5.1–5.4 — all resolved; omitted on import, and the device type's port list makes the whole class unrepresentable |
| Device-type addressing modes (would have prevented all 34) | §5.4 — parked in `ROADMAP.md` "Later"; needs a VLAN role concept, and two modes are blocked by #27 |
| Four deployed switch profiles absent from the ports file | §5 — needs running configs; `PLAN-prod-import.md` §6 |
| Data defects, missing type identities | §5, §4 — source-data and import work; see `PLAN-prod-import.md` |
