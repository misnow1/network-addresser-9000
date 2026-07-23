# Network Addresser 9000

Tracks IP addresses assigned to network equipment: IPv4 subnets, VLANs, switches, devices, and their rack groupings.

## Language

**Network Switch**:
A physical device that forwards traffic between ports and enforces VLAN trunk/access rules. Modeled as its own hierarchy (`Network Switch Type` → `Network Switch`), separate from Network Device — not a specialization of it.
_Avoid_: treating as a kind of Network Device

**Network Device**:
An end-point piece of equipment (amp, processor, mixer, etc.) with ports that each carry a single purpose and a single IP/DHCP assignment. Modeled as its own hierarchy (`Network Device Type` → `Network Device`), separate from Network Switch.
_Avoid_: treating as a kind of Network Switch

**VLAN**:
A top-level object combining an 802.1Q VLAN ID with its IPv4 addressing (subnet/CIDR, default gateway, DHCP range). A VLAN and its IPv4 network are the same row — the system has no notion of one without the other.
_Avoid_: "IPv4 Network" as an entity distinct from VLAN (it's a set of properties on VLAN, not a separate table)

**Rack**:
A physical container with a fixed slot count and a reserved IPv4 address range per VLAN, used to compute static addresses for the equipment installed in it. A Rack has no "purpose" field in the data model — a "spare rack" (e.g. a rack of spare amps) is just an ordinary Rack whose slots happen to hold spare equipment.
_Avoid_: treating "spare rack" as a distinct type from Rack

**Spare Pool**:
Devices/switches not yet assigned to any Rack (`rack` is null). These arrive DHCP-configured from the factory and are tracked by little more than serial number and hostname until they're racked and statically addressed.
_Avoid_: confusing with "spare rack" — a spare rack is a real Rack (see Rack); the spare pool is equipment with no rack at all

## Roles

**Viewer**:
Can see all data, cannot add or remove anything.

**Editor**:
Can view and add objects, cannot remove them.

**Admin**:
Can view, add, and remove objects. Remove implies add — there is no role that can remove but not add.
