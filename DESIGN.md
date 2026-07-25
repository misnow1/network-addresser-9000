# Network Addresser 9000

Initial design document, refined through a domain-modeling session. Canonical terminology now lives in [`CONTEXT.md`](./CONTEXT.md); decisions with real trade-offs behind them are recorded as ADRs in [`docs/adr/`](./docs/adr/). This document is the requirements narrative — where it once left something ambiguous, it now says what was decided and links to the record.

## Purpose

This repo contains a backend service and web-based frontend for tracking IP addresses assigned to various pieces of network equipment. The app tracks VLANs (which carry their own IPv4 addressing — see [`CONTEXT.md`](./CONTEXT.md)), network switches, network devices, and logical groupings of devices such as racks.

## Development

- **Backend**: Python, Django. Django's own migrations satisfy the "auto-generated schema upgrade/downgrade" requirement; its admin, auth, and audit-history ecosystem is why it was chosen over a bare SQLAlchemy/Alembic setup. See [ADR 0005](./docs/adr/0005-django-backend-framework.md).
- **Database**: MariaDB. See [ADR 0006](./docs/adr/0006-mariadb-database-engine.md).
- **Frontend**: Django admin (customized) for now, with a purpose-built UI deferred until real usage shows which views (rack layout, address-utilization dashboards, etc.) are worth building by hand.
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
  * DHCP Range: 10.200.0.0/24 (suggested as the bottom `/24` of the `/21`; stored and overridable — see [ADR 0002](./docs/adr/0002-network-sizing-dhcp-convention.md))
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
    * Base Address / Prefix
    * Default Gateway
    * DHCP Range
* Rack: an abstract grouping of devices with an address range from which device addresses are computed. Has no "purpose" field — a rack of spare equipment is just an ordinary Rack (see `CONTEXT.md`).
    * Slot Count
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
    * Port Mode (Trunk, Access, etc.)
    * Primary (untagged) VLAN
    * Allowed VLANs
* Network Switch (an instance of a Network Switch Type)
    * ...
    * Serial Number
    * The switch's Type is fixed at creation and cannot be changed afterward. Re-typing a switch (e.g. moving it to a different profile) means removing and recreating it, not editing the Type field.
* Network Switch Port: automatically populated from the switch's Type and its Type Ports, as a **one-time copy** made when the switch is first created — not kept in sync with later Type edits (which can't happen anyway, since the Type locks once the switch exists). **VLAN assignment is editable** in contrast to device port instances (see below), but the **physical Port Type is locked** — it's a hardware fact copied from the Type, not something that varies per switch. Conflict detection (such as incorrect VLAN assignment if a device is already connected) should be flagged, but this can be deferred to the custom UI.
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
* Network Device (an instance of a Network Device Type). Network Switch and Network Device are separate hierarchies, not a shared type — see `CONTEXT.md`. An unracked Network Device/Switch (`rack` is null) is in the **Spare Pool**: DHCP-configured, tracked by little more than serial number and hostname until it's racked.
    * ...
    * Serial Number
    * The device's Type is fixed at creation and cannot be changed afterward, for the same reason as Network Switch above — e.g. adding a Dante card to an amp means removing and recreating that device entry, not editing it in place (expected to be very rare).
* Network Device Port: description/purpose/VLAN/Port Type automatically populated (one-time copy, as with Network Switch Port above) when the device is created, from its Type's Network Device Type Ports. **Description, VLAN, and physical Port Type are immutable. Only the IP address/DHCP setting (and which switch port it's connected to) are editable.** Ports start out DHCP-configured when created; an operator gives one a static address afterward. A port's identity is `(Device, Description)` — Port Number, when present at all, is neither required nor unique.
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

* Lake LM44 or Lake LM26
    * Primary: Dante Primary
    * Secondary: Dante Secondary

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

Every port modeled above (including Shure **Redundant** Mode's Primary/Secondary, each on its own single VLAN) fits the "one physical port, one VLAN, one address" shape the current design supports. **Switched Mode is the one exception**, for both Shure and the generic 2-port case above: two separate physical jacks are bridged together in the hardware and share a single logical Dante interface — meaning one IP address across both jacks, not one address each. The current Network Device Type Port / Network Device Port model has no way to say "these two physical ports are one addressable interface"; each Type Port materializes into its own independently-addressable instance port. Modeling Switched Mode correctly would mean introducing a logical-interface concept distinct from the physical port/jack — deferred to a later phase (see [ADR 0010](./docs/adr/0010-port-profiles-and-materialization.md)). Until then, a Switched-Mode device's two ports can be tracked, but the system won't stop someone from giving them two different addresses, which wouldn't reflect the real hardware.

**Generic Computer**

A computer may have an arbitrary number of ports on arbitrary VLANs. This is a case where ports and VLANs should *probably* be mutable but that doesn't fit the current design constraints and may need to be punted to a future roadmap item.

**Other Device Types**

There are other device types, such as video devices, that have not been considered yet.

## Address Computation

By convention:
* We use RFC1918 addresses in the 10.0.0.0/8 range
* The VLAN ID is the second octet of the address
* VLANs default to a `/21`, giving eight `/24`-sized blocks; the bottom `/24` is suggested as the DHCP range and the rest is available for static rack allocation. This is a default suggestion, not an enforced rule — see [ADR 0002](./docs/adr/0002-network-sizing-dhcp-convention.md).
* The default gateway address is suggested as the lowest host address in the VLAN's subnet (`.1`), stored and overridable.
* The broadcast address is the highest address in the subnet.
* Rack address ranges are manually assigned per VLAN (system-suggests the next free block of the right size) rather than computed from the rack number — see [ADR 0001](./docs/adr/0001-manual-rack-address-ranges.md).
* Within a rack's range, a device's static address defaults to the rack's base address plus the device's rack slot number. This default is stored per device, not recomputed on the fly, and overriding it is strongly discouraged but not disallowed — needed to eventually support a device-replacement workflow (swapping a spare into an already-addressed slot), which is not yet designed. See [ADR 0003](./docs/adr/0003-device-addresses-stored-not-immutable.md).

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
