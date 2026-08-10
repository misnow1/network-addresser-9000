# Network Addresser 9000

Initial design document, refined through a domain-modeling session. Canonical terminology now lives in [`CONTEXT.md`](./CONTEXT.md); decisions with real trade-offs behind them are recorded as ADRs in [`docs/adr/`](./docs/adr/). This document is the requirements narrative — where it once left something ambiguous, it now says what was decided and links to the record.

## Purpose

This repo contains a backend service and web-based frontend for tracking IP addresses assigned to various pieces of network equipment. The app tracks VLANs (which carry their own IPv4 addressing — see [`CONTEXT.md`](./CONTEXT.md)), network switches, network devices, and logical groupings of devices such as racks.

## Development

- **Backend**: Python, Django. Django's own migrations satisfy the "auto-generated schema upgrade/downgrade" requirement; its admin, auth, and audit-history ecosystem is why it was chosen over a bare SQLAlchemy/Alembic setup. See [ADR 0005](./docs/adr/0005-django-backend-framework.md).
- **Database**: MariaDB. See [ADR 0006](./docs/adr/0006-mariadb-database-engine.md).
- **Frontend**: Django admin (customized) for **mutation**, and a purpose-built **read-only** UI at `/` for reading — rack elevations, the address map, device and spare-pool views, and the audit trail. The production import supplied the real usage this split was waiting on. Every mutation in the read-only UI is a deep link into the admin, so validation, removal confirmations and the audit actor keep exactly one home. See [ADR 0020](./docs/adr/0020-read-only-purpose-built-ui.md).
- **Auth**: Django's built-in local accounts, not SSO — this may be revisited if the tool ever needs to integrate with an organizational identity provider.
- Code should be written to be easily testable and understood by humans. Python code should have complete docstrings and type hints. Test units should be supplied for as much code as is practically possible.

## Deployment

Self-hosted on the company network / VPN-only for the time being — no public internet exposure, so TLS termination is not yet a concern. If the tool needs external exposure later, that will most likely be handled by a reverse proxy in front of it, and this decision will be revisited then.

The application should be deployable using a containerized framework such as Docker, ideally with a docker-compose file that brings up the app and MariaDB together.

## Network Topology

At the top level are VLANs, each combining an 802.1Q VLAN ID with an IPv4 subnet expressed in CIDR notation (see the **VLAN** entry in `CONTEXT.md` — a VLAN and its IPv4 addressing are the same object, not two linked ones). Each Rack has a reserved address range **per VLAN**, manually assigned by an admin (the system suggests the next free block) rather than derived from a formula — see [ADR 0001](./docs/adr/0001-manual-rack-address-ranges.md). For example:

* VLAN 200:
  * Name: Control
  * VLAN ID: 200
  * IPv4 Subnet: 10.200.0.0/21
  * Default Gateway: 10.200.0.1 (suggested; stored and overridable)
  * DHCP Range: 10.200.0.2 - 10.200.0.199 (a manually-entered start/end address pair, not a CIDR block — must fall within the subnet and exclude the network, gateway, and broadcast addresses; see [ADR 0011](./docs/adr/0011-dhcp-range-as-address-range.md))
* VLAN 201
  * Name: Dante Primary
  * VLAN ID: 201
  * IPv4 Subnet: 10.201.0.0/21

* Rack 1
  * Name: WPC SR Upper
  * Address Range (VLAN 200): 10.200.1.0/27
  * Address Range (VLAN 201): 10.201.1.0/27
  * (A second rack could equally be assigned 10.200.1.32/27, 10.200.1.64/27, etc. — ranges pack sequentially, they don't require one rack per third octet.)

* Cisco SG300-10MP
  * Hostname: mps-sg300-wpc-sr-upper
  * Manufacturer: Cisco
  * Model: SG300-10MP
  * Type Name (profile): Default
  * Serial Number: tbd
  * Port Count: 8 1GbE Copper + 2 1GbE Combo
  * Rack: 1
  * Rack Slot: 1
  * Address (VLAN 200): 10.200.1.1/21 — defaults to rack base + slot number, stored and editable but override is strongly discouraged in the UI (see [ADR 0003](./docs/adr/0003-device-addresses-stored-not-immutable.md))
  * Address (VLAN 201): 10.201.1.1/21
  * DHCP Server Enabled/Disabled
  * Port Configuration:
    * 1: ...

* Martin Audio IK-42
  * Hostname: tbd
  * Manufacturer: Martin Audio
  * Model: IK-42
  * Type Name (profile): with Dante Card
  * Serial Number: tbd
  * Rack: Rack 1
  * Rack Slot: 2
  * Ports
    * Dante Primary (VLAN 201)
      * Address: 10.201.1.2/21
      * Switch: mps-sg300-wpc-sr-upper
      * Switch Port: 1
      * Assigned VLAN: 201
    * Dante Secondary (VLAN 202)
      * ...
    * Control (VLAN 200)
      * ...

## Objects

The list of objects that the system will need to track includes, but is not limited to:

* VLAN (see `CONTEXT.md`)
    * VLAN ID
    * Base Address / Prefix — optional; a VLAN with no subnet is **L2-only** and cannot have a Default Gateway, DHCP Range, rack address range, or static address (see [ADR 0012](./docs/adr/0012-switch-port-vlan-profiles.md))
    * Default Gateway
    * DHCP Range (start/end address pair)
* Rack: an abstract grouping of devices with an address range from which device addresses are computed. Has no "purpose" field — a rack of spare equipment is just an ordinary Rack (see `CONTEXT.md`). A rack's ranges can be entered by hand or seeded in one step from a **Rack Template** (see "Rack Templates" below); either way, `RackVlanRange` rows created this way are ordinary, indistinguishable rows.
    * Slot Count — the number of addressable slots. A slot is an addressing ordinal (base address + slot number), not a physical rack-unit position; physical RU height/placement is deliberately not modeled.
    * IPv4 Address Range (per VLAN)
A Network Switch/Device Type is a **purpose profile** built on a hardware model, not the bare hardware model itself: the same physical switch or device commonly needs several different profiles, because what each port is *used for* varies even though the hardware doesn't. For example, a Cisco SG350-10MP wired up for a drive rack (different VLAN per port) needs a different profile than the identical switch wired for an amp rack; a Martin Audio IK-42 with a Dante card needs a different profile (extra ports) than the same model without one. A Type is therefore identified by `(Manufacturer, Model, Name)`, where `Name` is a **required, non-blank** profile label (e.g. "For Drive Rack", "with Dante Card", "Default" for a model with only one profile) — this keeps the type selector unambiguous for an audience that may not have deep VLAN/subnetting knowledge.

A Type's port list is fixed once any instance (switch/device) exists — editing or removing a Type Port at that point would silently leave existing instances holding a stale copy. To change a profile's port layout, create a new named profile instead.

* Network Switch Type
    * Manufacturer
    * Model
    * Name (profile label, required — see above)
    * Port Count (must equal the number of Network Switch Type Ports defined for this profile)
* Network Switch Type Port
    * Port Number
    * Port Description
    * Port Type — a structured choice, not free text: 10/100M RJ45, 1GbE RJ45 (copper), 1GbE SFP, 1GbE Combo (RJ45/SFP), 2.5GbE RJ45, 10GbE RJ45, 10GbE SFP+, 25GbE SFP28, Other/Unknown
    * Switch Port VLAN Profile (see "Switch Port VLAN Profiles" below) — required; defaults to the system "Default" profile if none is selected
* Network Switch (an instance of a Network Switch Type)
    * ...
    * Serial Number
    * The switch's Type is fixed at creation and cannot be changed afterward. Re-typing a switch (e.g. moving it to a different profile) means removing and recreating it, not editing the Type field.
* Network Switch Port: automatically populated from the switch's Type and its Type Ports, as a **one-time copy** made when the switch is first created — not kept in sync with later Type edits (which can't happen anyway, since the Type locks once the switch exists). Unlike every other materialized field here, the **Switch Port VLAN Profile is copied as a live reference, not a snapshot** (see below and [ADR 0012](./docs/adr/0012-switch-port-vlan-profiles.md)) — editing a profile's allowed VLANs changes every port using it immediately, including already-materialized ones. **A different profile can be selected** for an instantiated port unless a device is already connected to it, but the **physical Port Type is locked** — it's a hardware fact copied from the Type, not something that varies per switch. Conflict detection (such as incorrect VLAN assignment if a device is already connected) should be flagged, but this can be deferred to the custom UI.
* Network Device Type (amp, processor, mixer, etc.) — also a purpose profile; see above. e.g. "Martin Audio IK-42 — with Dante Card" vs "— without Dante Card", or "Shure ULXD4Q — Split Mode" vs "— Redundant Mode".
    * Manufacturer
    * Model
    * Name (profile label, required)
    * Ports: a list of "Network Device Type Port"
* Network Device Type Port (a port definition for the port(s) that an instance of this device will always have)
    * Port Number (optional - most devices have fixed numbers of ports with fixed purpose that aren't numbered)
    * Port Description (required — this is the port's identity/purpose, e.g. "Dante Primary"; not optional the way Port Number is)
    * Port VLAN (subnet) — required, one VLAN per port (see the deferred limitation below for the one case this doesn't cover)
    * Port Type — same structured choice list as Network Switch Type Port, above
    * Slot Offset (`PositiveIntegerField`, default 0) — for hardware whose own firmware derives this port's address from another port's and refuses to let anyone change it (e.g. a DiGiCo console's audio engine, always its control address + 1); everything else leaves this at 0. A VLAN carrying a non-zero-offset port must also carry an offset-0 port on that same VLAN — the offset-0 port is what the offset is measured from. See [ADR 0017](./docs/adr/0017-derived-same-vlan-addresses.md); this is a narrow mechanism, not a general multi-part-hardware feature — see the ADR's scope-boundary section.
* Network Device (an instance of a Network Device Type). Network Switch and Network Device are separate hierarchies, not a shared type — see `CONTEXT.md`. An unracked Network Device/Switch (`rack` is null) is in the **Spare Pool**: DHCP-configured, tracked by little more than serial number and hostname until it's racked. A device whose Type declares a non-zero Slot Offset occupies an **ordinal range**, not a single slot: `rack_slot` through `rack_slot + max(Slot Offset)` across its Type's ports (`slot_span`, computed from the Type, never stored) — every other device still occupies exactly its own `rack_slot`, since `slot_span` is 1 whenever every port is at offset 0.
    * ...
    * Serial Number
    * The device's Type is fixed at creation and cannot be changed afterward, for the same reason as Network Switch above — e.g. adding a Dante card to an amp means removing and recreating that device entry, not editing it in place (expected to be very rare).
* Network Device Port: description/purpose/VLAN/Port Type/Slot Offset automatically populated (one-time copy, as with Network Switch Port above) when the device is created, from its Type's Network Device Type Ports. **Description, VLAN, physical Port Type, and Slot Offset are immutable.** The IP address/DHCP setting (and which switch port it's connected to) are editable — **except** a port's IP address at a non-zero Slot Offset, which is derived from the offset-0 port on the same VLAN and locked (ADR 0017): editing the offset-0 port's address recomputes every offset sibling's address to match (`control address + offset`), and taking the offset-0 port to DHCP takes its offset siblings to DHCP with it. Device creation offers a DHCP-or-static choice, defaulting to static: a racked device's ports get a computed rack-range-base + rack-slot (+ Slot Offset, for an offset port) address per VLAN, the same way a Network Switch's address is suggested; an unracked device (spare pool) always materializes DHCP regardless of the choice, and a port on an L2-only VLAN always materializes DHCP too (see ADR 0013). An operator can flip an offset-0 port's addressing either way afterward, and its offset siblings follow automatically. A port's identity is `(Device, Description)` — Port Number, when present at all, is neither required nor unique.
    * IPv4 Address -OR- DHCP
    * IPv4 Default Gateway -OR- NULL (if DHCP) — always derived live from the port's VLAN's Default Gateway, not stored on the port itself

### Concrete Device Examples

**Power Amps**

* Martin Audio IK-42 with Dante Card
    * Control: Control Network
    * Primary: Dante Primary
    * Secondary: Dante Secondary
* Martin Audio IK-42 without Dante Card
    * Control: Control Network

A caveat: the Dante card can be added and removed from an amp but that is *very* rare and I would say the easier option there is to destroy and recreate the device entry rather than allowing it to be edited/changed.

**Processors**

* Lab.Gruppen LM44 or LM26
    * Primary: Dante Primary
    * Secondary: Dante Secondary

Deliberately no Control port, and this holds for **every** Lab.Gruppen product (the LM-series processors above, and amps such as the PLM20000Q / PLM20K44): control traffic rides on *both* Dante ports and addresses, in Switched/Bridged and Redundant mode alike. There is no dedicated control interface to address. Note the manufacturer — Lab.Gruppen acquired the Lake processing technology from Dolby years ago, so "Lake" names the DSP inside the unit, not the company; referring to an LM44 as "a Lake" is shop shorthand.

**Shure Wireless Mic Receivers**

* Shure ULXD4Q or ULXD4D Switched Mode (both Dante ports are bridged together)
    * Primary: Dante Primary (Shure Control will ride on the Dante Primary network)
    * Secondary: Dante Primary (Shure Control will ride on the Dante Primary network)
* Shure ULXD4Q or ULXD4D Redundant Mode
    * Primary: Dante Primary (Shure Control will ride on the Dante Primary network)
    * Secondary: Dante Secondary
* Shure ULXD4Q or ULXD4D Split Mode
    * Primary: Shure Control
    * Secondary: Dante Primary

**Generic Dante Devices**

* 1-port device (AVIO, etc.)
    * Dante Primary
* 2-port device Switched Mode (both ports are bridged together)
    * Primary: Dante Primary
    * Secondary: Dante Primary
* 2-port device Redundant Mode
    * Primary: Dante Primary
    * Secondary: Dante Secondary

**Deferred: Bridged Multi-Port Interfaces**

Every port modeled above (including Shure **Redundant** Mode's Primary/Secondary, each on its own single VLAN) fits the "one physical port, one VLAN, one address" shape the current design supports. **Switched Mode is the one exception**, for both Shure and the generic 2-port case above: two separate physical jacks are bridged together in the hardware and share a single logical Dante interface — meaning one IP address across both jacks, not one address each. The current Network Device Type Port / Network Device Port model has no way to say "these two physical ports are one addressable interface"; each Type Port materializes into its own independently-addressable instance port. Modeling Switched Mode correctly would mean introducing a logical-interface concept distinct from the physical port/jack — deferred to a later phase (see [ADR 0010](./docs/adr/0010-port-profiles-and-materialization.md), tracked as #27). Static-by-default materialization ([ADR 0013](./docs/adr/0013-device-port-addressing-at-creation.md)) refuses to create a Switched-Mode-shaped device at all rather than silently giving its two bridged jacks two different addresses; DHCP remains available for these devices.

**Generic Computer**

A computer may have an arbitrary number of ports on arbitrary VLANs. This is a case where ports and VLANs should *probably* be mutable but that doesn't fit the current design constraints and may need to be punted to a future roadmap item.

**Other Device Types**

There are other device types, such as video devices, that have not been considered yet.

## Switch Configuration

This section expands on the switch type, switch, switch port type, and switch port configurations described above.

### VLAN Profiles

TODO: Document VLAN profiles for various purposes (Dante, AES67, NDI, etc). This is mostly outside the scope of the tool but is very useful for writing switch configurations.

### Switch Port VLAN Profiles

Many devices have multiple ports that have the same general purpose but different numbers and descriptions. Rather than configuring each port's VLAN mode by hand, a port points at a reusable **Switch Port VLAN Profile** — a named bundle of Port Mode / Native VLAN / Allowed VLANs / Allow All VLANs. Unlike a Type Port's other materialized fields (a one-time copy, per [ADR 0010](./docs/adr/0010-port-profiles-and-materialization.md)), a Switch Port VLAN Profile is referenced **live**: editing a profile's allowed VLANs changes every port that uses it immediately, including ports on switches that already exist. See [ADR 0012](./docs/adr/0012-switch-port-vlan-profiles.md) for why this is a deliberate departure from the rest of the port-profile/materialization pattern.

For example, consider the following profiles:

* Audio Control
    * Port Mode: Trunk
    * Native VLAN: 200
    * Allowed VLANs: none (VLAN 200 is implied)
* Dante Primary
    * Port Mode: Trunk
    * Native VLAN: 201
    * Allowed VLANs: none (VLAN 201 is implied)
* Dante Secondary
    * Port Mode: Trunk
    * Native VLAN: 202
    * Allowed VLANs: none (VLAN 202 is implied)
* Audio Trunk Port
    * Port Mode: Trunk
    * Native VLAN: 201
    * Allowed VLANs: 200, 202 (VLAN 201 is implied)
* System Trunk Port
    * Port Mode: Trunk
    * Native VLAN: 1 (the system "Default VLAN" — see below)
    * Allow all VLANs: true

A switch definition might then become:

* Fancy Switch 1
    * ...
    * Ports: 2
        * Port 1
            * Number: 1
            * Name: Fancy Name
            * Profile: Dante Primary
        * Port 2
            * Number: 2
            * Name: Fancy Second Name
            * Profile: Audio Trunk Port

Once a profile has any real Network Switch Port using it, its **Port Mode and Native VLAN lock** — the same "create a new named profile instead" rule Type Profiles use ([ADR 0010](./docs/adr/0010-port-profiles-and-materialization.md)). **Allowed VLANs and Allow All VLANs stay editable** even then: adding a tagged VLAN to a trunk that's already deployed is the profile's whole reason to exist. A profile referenced only by Type Ports (no real switch yet) is still fully editable. A profile's Name is never locked. The system "Default" profile (see below) locks all three permanently and can never be deleted.

### Switch Port Profile VLAN Selection

There must be a default port profile in the system, permanently defined as:
* Name: Default
* Native VLAN: 1
* Allowed VLANs: (null)
* All VLANs Allowed: true
* Port Mode: Trunk

VLAN 1 is represented by a real, system-seeded VLAN named "Default VLAN" with no IPv4 subnet — an **L2-only VLAN**: it can be used as a port's native or allowed VLAN, but it can never have a default gateway, a DHCP range, a rack address range, or a static address, since there is no subnet to validate any of those against. Any VLAN's subnet may be left blank for the same reason, not just VLAN 1's.

A switch port profile is required for every switch type port. If none is selected, then the default (above) is used.

When creating a profile, the default switch port mode should be Trunk.

A switch/device Type Port stores a **profile assignment**, not a copy of the profile's VLAN configuration. When a switch is created, its ports are materialized with the *same profile reference* their Type Ports had at that moment (a one-time copy of the assignment, per ADR 0010's usual pattern) — but from then on, the port's VLAN config is whatever the assigned profile currently says, live, not a frozen snapshot.

Editing a profile's Allowed VLANs or Allow All VLANs is always allowed, even once it's in use. Editing its Port Mode or Native VLAN is blocked once any real Network Switch Port uses it — create a new named profile instead.

The user can select a different profile for an instantiated port unless a device is already connected — disconnect it first, swap profiles, then reconnect.

There is no manual configuration for switch ports at creation or after — all changes to a port's Native VLAN, etc. must be done by selecting a different profile (or, for allowed VLANs, editing the assigned profile directly). This is to prevent potential footguns.


## Address Computation

By convention:
* We use RFC1918 addresses in the 10.0.0.0/8 range
* The VLAN ID is the second octet of the address
* VLANs default to a `/21`, giving eight `/24`-sized blocks; there is no longer an automatic DHCP-range suggestion, but static rack allocation is conventionally kept out of the bottom `/24` when a DHCP range is manually entered there. See [ADR 0002](./docs/adr/0002-network-sizing-dhcp-convention.md) for the `/21` sizing convention and [ADR 0011](./docs/adr/0011-dhcp-range-as-address-range.md) for the DHCP range itself: a manually-entered start/end address pair (not a CIDR block), which must fall entirely within the subnet and exclude the network address, the default gateway, and the broadcast address.
* The default gateway address is suggested as the lowest host address in the VLAN's subnet (`.1`), stored and overridable.
* The broadcast address is the highest address in the subnet.
* Rack address ranges are manually assigned per VLAN (system-suggests the next free block of the right size) rather than computed from the rack number — see [ADR 0001](./docs/adr/0001-manual-rack-address-ranges.md). A rack's ranges may also be seeded for several VLANs at once at creation time from a **Rack Template** (see "Rack Templates" below); this still goes through the same next-free-block suggestion, not a separate formula.
* Within a rack's range, a device port's static address defaults to the rack's base address plus the device's rack slot number, plus the port's Slot Offset (0 for every port except a hardware-derived one — see Slot Offset above). This default is stored per port, not recomputed on the fly, and overriding it is strongly discouraged but not disallowed for an offset-0 port — needed to eventually support a device-replacement workflow (swapping a spare into an already-addressed slot), which is not yet designed. See [ADR 0003](./docs/adr/0003-device-addresses-stored-not-immutable.md). A non-zero-offset port is the one narrow exception: its address is *not* independently editable — it's derived from the offset-0 port on its VLAN and recomputed whenever that address changes, because the hardware itself guarantees the relationship and offers no way to override it. See [ADR 0017](./docs/adr/0017-derived-same-vlan-addresses.md) for what this costs ADR 0003, precisely.
* A VLAN's subnet may be left blank, making it L2-only: usable as a Switch Port VLAN Profile's Native or Allowed VLAN, or as a Network Device Type Port's VLAN (DHCP only), but never addressed. The system-seeded VLAN 1 ("Default VLAN") is one such VLAN; a site may leave any other VLAN subnet-less the same way. See [ADR 0012](./docs/adr/0012-switch-port-vlan-profiles.md).

## Rack Templates

We create a lot of racks that are the same *kind* of rack — an "Audio Rack" always gets the audio VLANs, a "Video Rack" always gets the video VLANs — but every rack's address ranges have always been built by hand, one VLAN at a time. A **Rack Template** is a named, reusable set of VLANs (plus an optional default slot count) that allocates a `RackVlanRange` for each listed VLAN in one step when a new rack is created from it. For example:

* Audio Rack: VLANs 200, 201, 202
* Video Rack: all currently-defined video VLANs, listed explicitly
* Lighting Rack: all currently-defined lighting VLANs, listed explicitly
* Infra Rack: every currently-defined VLAN except the system Default VLAN, listed explicitly

("all video VLANs" etc. above means the VLANs that exist and are listed in the template at the time someone edits it — there is no dynamic "all VLANs of this kind" flag, since this project has no VLAN category/tag concept. See [ADR 0014](./docs/adr/0014-rack-templates-seed-once-vlan-sets.md).)

**A Rack Template carries the purpose; the Rack it creates does not.** This is deliberate, not an oversight: Rack still has no "purpose" field (see `CONTEXT.md`), and a rack created from the "Audio Rack" template is, the moment it's created, an ordinary Rack with ordinary `RackVlanRange` rows — indistinguishable from a rack whose ranges were entered by hand one at a time. The rack keeps no reference back to the template it came from.

This is a **seed-once** relationship, the same pattern ADR 0010 uses for Type Port materialization, not the live-reference pattern ADR 0012 uses for Switch Port VLAN Profiles: editing a template's VLAN list after a rack has been created from it has no effect on that rack. See ADR 0014 for why this direction was chosen over the live-reference alternative.

Applying a template doesn't introduce a new allocation formula — it constructs a blank `RackVlanRange` per listed VLAN and lets the same next-free-block suggestion logic that already backs manual range entry fill each one in (ADR 0001), all within one request. If any listed VLAN can't be allocated a big-enough block, the whole rack-creation request fails and rolls back — no rack, no partial ranges. A template can be combined with manually-entered ranges in the same rack-creation submission; naming the same VLAN in both is a validation error, not silent precedence either way.

### Deferred: Populated Rack Templates and Hostname Templating

A larger version of this idea was also discussed: templates that lay out *which equipment* goes in which slot, materializing real switches and devices (not just address ranges) when a rack is created — plus generating each materialized device's hostname from a pattern. Both are deferred, tracked as [#30](https://github.com/misnow1/network-addresser-9000/issues/30) and [#31](https://github.com/misnow1/network-addresser-9000/issues/31) respectively, and are a materially bigger feature than the VLAN-set templates above — not simply a bigger version of them.

**The motivating example** — several racks share this shape:

**Martin Audio IK42 rack**

* Slot count: 19
* Slots:
    1. Primary Switch
    2. Secondary Switch
    3. IK42: mid/hi 1
    4. IK42: mid/hi 2
    5. IK42: subs

*Note: the slot numbers are addressing ordinals, not physical rack-unit positions — the IK42 amps are 2U devices, so their physical position in the rack differs from their addressing slot number. This is true of Rack's `Slot Count` generally (see "Objects" above), not just of populated templates.*

An example generated hostname might be `mps-{rack}-{label}-{slot}` (using the restricted placeholder set recommended below, not a real templating engine). Following the same rule as static addresses (ADR 0003), a generated name would be computed once at materialization time and stored — not automatically re-derived if the device is later moved.

**What's missing to build this, concretely:**

* Slot entries would need to be FKs to `NetworkSwitchType`/`NetworkDeviceType`, not free text. That makes a populated template a new *dependent* of Type, requiring `PROTECT` on Type deletion (ADR 0007) — the same pattern the VLAN-only template above uses for VLAN deletion (ADR 0014 decision 2), just aimed at Types instead. This protects **Types** from deletion while a template references them; it says nothing about the **template's own** deletability, which (assuming a populated template also keeps no back-reference from the Rack it creates, per ADR 0014 decision 5) would likely remain just as freely deletable as the VLAN-only case — that's a design question for #30 to settle, not something this bullet should imply is already answered.
* Materialization would need to run each device's normal port-materialization path inside one transaction, and something would need to supply the static-vs-DHCP choice (ADR 0013) for the equipment being created.
* ADR 0013 already refuses to create a Switched-Mode-shaped device (two ports sharing one VLAN). A template containing one of these device types should be caught when the template is edited, not discovered only when someone applies it to a rack.
* `{{ device_name }}` doesn't map to any existing field — a Type's `Name` is its profile label ("with Dante Card"), not a per-slot identity like "mid/hi 1"; that label would need to be added, most likely per slot.
* Generated hostnames imply uniqueness that doesn't exist today: `hostname` is optional and not unique on either Network Switch or Network Device, and `Rack.Name` isn't unique either.
* A restricted placeholder set (e.g. `{rack}`, `{label}`, `{slot}`) is recommended over a general template-string engine — evaluating arbitrary template syntax over operator-controlled input is a footgun this tool doesn't need.

An alternative worth considering instead of (or alongside) populated templates: a **"duplicate rack"** action that creates a new rack and re-materializes new devices based on an existing rack's arrangement, rather than a template that was never a real rack to begin with. This needs the same equipment-materialization machinery as a populated template either way, so it doesn't avoid the open questions above — but it sidesteps needing a stored slot-layout object distinct from an actual Rack.

## Constraints and Other Notes

Here we will list some networking and device constraints in no particular order.

* Some devices behave poorly with octets of all 1's (255) even if such addresses are not technically reserved (for broadcast, etc.). Such addresses should be avoided. For example, 10.0.0.255/21 may cause issues.
* A DDL diagram would be nifty!

# Frontend Requirements

The frontend (Django admin, for now — see Development) should provide methods for an authenticated user to:
* Add and remove VLANs
* Add and remove Racks or collections of devices
* Add or remove switch types/models
* Add or remove switches and assign them to racks or the spare pool
* Add or remove device types/models
* Add or remove devices and assign them to rack slots or the spare pool

**RBAC**: three global roles — Viewer (view only), Editor (view + add), Admin (view + add + remove). Remove implies add; there is no role that can remove but not add. See `CONTEXT.md`.

**Audit trail**: every object records who created it and when, and mutations (edits, removals) are also logged — not just creation — since address overrides and rack/slot reassignment are exactly the events this tool exists to make traceable. See [ADR 0004](./docs/adr/0004-audit-trail-covers-mutations.md).

**Removal semantics**: removing a Rack, VLAN, or a Switch/Device Type is blocked while it still has dependents — the user must move or remove each dependent first. Removing a Switch does not cascade-delete the devices plugged into it; it un-assigns them instead. A device with nothing depending on it still gets a big, scary confirmation prompt before removal, especially when other devices route traffic through it. See [ADR 0007](./docs/adr/0007-removal-blocks-containers-unassigns-leaves.md).
