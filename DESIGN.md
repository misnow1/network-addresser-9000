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
* Network Switch Type
    * Manufacturer
    * Model
    * Port count and type
* Network Switch (an instance of a Network Switch Type)
    * ...
    * Serial Number
* Network Switch Port
    * Port Number
    * Port Description
    * Port Type (10/100M, 1GbE, 1GbE Combo, 10GbE SFP+, etc.)
    * Port Mode (Trunk, Access, etc.)
    * Primary (untagged) VLAN
    * Allowed VLANs
* Network Device Type (amp, processor, mixer, etc.)
    * Manufacturer
    * Model
    * Port count and type
* Network Device (an instance of a Network Device Type). Network Switch and Network Device are separate hierarchies, not a shared type — see `CONTEXT.md`. An unracked Network Device/Switch (`rack` is null) is in the **Spare Pool**: DHCP-configured, tracked by little more than serial number and hostname until it's racked.
    * ...
    * Serial Number
* Network Device Port
    * Purpose (Selected VLAN)
    * IPv4 Address -OR- DHCP
    * IPv4 Default Gateway -OR- NULL (if DHCP)

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
