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
A top-level object combining an 802.1Q VLAN ID with its IPv4 addressing (subnet/CIDR, default gateway, DHCP range as a start/end address pair), plus an optional **Department**. A VLAN and its IPv4 network are the same row — the system has no notion of one without the other. The subnet is optional: a VLAN with no subnet is **L2-only** — usable as a Switch Port VLAN Profile's native/allowed VLAN, or a device port's VLAN (DHCP only) — but it can never have a gateway, DHCP range, rack range, or static address. The system-seeded VLAN 1 ("Default VLAN") is one such VLAN and carries no department. See ADR 0012, ADR 0021.
_Avoid_: "IPv4 Network" as an entity distinct from VLAN (it's a set of properties on VLAN, not a separate table); assuming every VLAN has a subnet; assuming every VLAN has a department

**Department**:
An operator-facing organizational label a VLAN may optionally carry — "Audio", "Lighting", "Video" — free-standing rather than enumerated, so an operator can add one without a code change or a redeploy. Descriptive only: no code branches on a department's value, and it does not scope allocation — a rack range, suggestion or stored address is computed identically regardless of department. No department is ever system-seeded; a fresh database has none until an operator creates one. See ADR 0021.
_Avoid_: confusing this with a VLAN's *role* (Control, Dante Primary, Dante Secondary — designed in ADR 0021, built in phase 21) — department is operator vocabulary nothing reads, role is code vocabulary the addressing modes branch on; reading the existence of this field as license to scope rack allocation by it (ADR 0021 says explicitly it does not)

**Rack**:
An abstract grouping of equipment with a fixed slot count and a reserved IPv4 address range per VLAN, used to compute static addresses for the equipment installed in it. A slot is an *addressing ordinal* (base address + slot number) — not a physical rack-unit position; physical RU height/placement is deliberately not modeled. **A Rack need not correspond to physical hardware at all**: `AVIO`, `CONSOLES` and `SHURE` are groupings of related equipment rather than enclosures, and they address exactly as an amp rack does. Rack is this system's address pool, and the only thing that allocates addresses — see ADR 0019, which declined a separate pool concept and records why the ordinal is what guarantees a device's addresses stay consistent across VLANs. The ordinal is *suggested* (lowest free run of the occupant's `slot_span`) but freely overridable, per ADR 0001 and ADR 0003. A Rack has no "purpose" field in the data model — a "spare rack" (e.g. a rack of spare amps) is just an ordinary Rack whose slots happen to hold spare equipment, and a rack created from a Rack Template is likewise just an ordinary Rack the moment it exists (see Rack Template). **An empty Rack is a reservation of its address block** — the way to hold offset space back from the first-fit suggester — and is likewise an ordinary Rack, with a `slot_count` that honestly states how many ordinals its block can address (ADR 0019; the boundary with ADR 0015's "`slot_count` stays honest" rule is drawn there).
_Avoid_: treating "spare rack" as a distinct type from Rack; treating a slot number as a physical rack-unit position; assuming a Rack is a physical enclosure; reaching for a separate "address pool" concept (ADR 0019)

**Rack Template**:
A named, reusable set of VLANs (plus an optional default slot count) that seeds a new Rack's `RackVlanRange` rows in one step at creation time, reusing the same next-free-block suggestion manual range entry already uses. Seed-once, like Type Port materialization (ADR 0010) — **not** live-referenced like a Switch Port VLAN Profile (ADR 0012): editing a template after a rack exists has no effect on that rack, and the rack keeps no reference back to the template. See ADR 0014.
_Avoid_: calling this a "rack type" or "rack profile" — "Type" already means a purpose profile of a *hardware model* (see Type Profile) and "profile" already means the *live-referenced* Switch Port VLAN Profile; neither matches this seed-once concept. Also avoid assuming a rack remembers which template created it — it doesn't (see Rack).

**Spare Pool**:
Devices/switches not yet assigned to any Rack (`rack` is null). These arrive DHCP-configured from the factory and are tracked by little more than serial number and hostname until they're racked and statically addressed.
_Avoid_: confusing with "spare rack" — a spare rack is a real Rack (see Rack); the spare pool is equipment with no rack at all

**Type Profile**:
A Network Switch/Device Type is a *purpose profile* built on a hardware model, not the bare hardware model itself — the same physical hardware can have several profiles when what its ports are used for differs (e.g. a Cisco SG350-10MP wired for a drive rack vs. the same switch wired for an amp rack). Identified by `(Manufacturer, Model, Name)`, where `Name` is a required, non-blank profile label ("Default" for a model with only one profile). See ADR 0010.
_Avoid_: treating `(Manufacturer, Model)` as a Type's whole identity, or assuming one Type = one hardware model

**Network Switch Type Port / Network Device Type Port**:
A port definition template owned by a Type Profile — physical port type, and (for switches) VLAN mode/purpose. Copied exactly once ("materialized") into a real Network Switch Port/Network Device Port when an instance of that type is first created; never re-synced afterward. Device creation chooses DHCP or static for the whole device at that moment (defaulting to static; always DHCP if unracked or on an L2-only VLAN) — see ADR 0013. A profile's type ports lock once the profile has any instance — change a profile's port layout by creating a new named profile instead. See ADR 0010.

A Network Device Type Port also carries a **Slot Offset** (default 0), copied onto its materialized Network Device Port — the mechanism for hardware whose firmware derives one port's address from another's and refuses to let it be changed (e.g. a DiGiCo console's audio engine, always its control address + 1). A device whose type declares a non-zero offset occupies an **ordinal range** — `rack_slot` through `rack_slot + max(slot_offset)` across its type's ports — not a single slot; every ordinary device still occupies just its own `rack_slot`, since that range collapses to one ordinal when every port is at offset 0. An offset port's address is derived and read-only, not independently editable. See ADR 0017 for the mechanism and its narrow scope (not general multi-part-hardware support — the test is whether the hardware itself computes and locks the second address).
_Avoid_: assuming an edit to a Type Port after instances exist affects those instances; assuming an instance's type can be changed instead of recreating it with a different Type; using Slot Offset for hardware whose parts are independently, manually addressed (that's two ordinary devices in two slots — see ADR 0017's scope-boundary section — linked as a Device Companion pair if one genuinely can't exist without the other)

**Device Companion**:
A Network Device that cannot exist without another one — a Yamaha DM7C/DM3 console's Device Control Interface. A Network Device Type declares a `companion_type`; creating an instance of that Type also creates its companion device, in the same transaction, and the companion can never be created on its own. Removing the host removes the companion; removing the companion by itself is refused. The pair moves as a unit — the companion's rack/slot are managed from its host — but their **addresses are entirely independent**, operator-set, and in no fixed relationship: production has one console's interface an address *below* it and another's *above*. Optionality is not expressed here but by Type Profile (a model with no companion interface is a differently-named profile). See ADR 0018.
_Avoid_: reaching for Slot Offset to express this (that's derived, mandatorily-consecutive addressing — ADR 0017); assuming a companion's address bears any relation to its host's, or that the two are adjacent or even in a fixed direction; treating separately-bought optional hardware (a DM7-EX extender, a Dante add-in card) as a companion — those are ordinary independent devices

**Switch Port VLAN Profile**:
A reusable, named bundle of switch port L2 config (Port Mode, Native VLAN, Allowed VLANs, Allow All VLANs) that a Network Switch Type Port / Network Switch Port points at. Unlike a Type Port's other materialized fields, a Switch Port VLAN Profile is referenced **live** — the port stores the profile's id, not a copy of its contents, so editing a profile's allowed VLANs reaches every port using it immediately. Its Port Mode/Native VLAN lock once any real Network Switch Port uses it; Allowed VLANs/Allow All VLANs stay editable even then. A system "Default" profile (Native VLAN = the seeded, subnet-less "Default VLAN") is the fallback for any Type Port with none selected, and can never be edited or deleted. See ADR 0012.
_Avoid_: assuming a profile is snapshot-copied onto a port the way a Type Port is — it's a live reference, not a seed-once copy; confusing this with a Type Profile (Network Switch/Device Type), which is a different, unrelated use of the word "profile" in this codebase

## Roles

**Viewer**:
Can see all data, cannot add or remove anything. Provisioned `is_staff=False` and reads
through the purpose-built UI at `/` — never the Django admin, which refuses non-staff users
entirely. See ADR 0020.

**Editor**:
Can view and add objects, cannot remove them. Provisioned `is_staff=True`, because every
mutation is a deep link into the Django admin (ADR 0020) and reaching one requires admin
access.

**Admin**:
Can view, add, and remove objects. Remove implies add — there is no role that can remove but
not add. Provisioned `is_staff=True`, for the same reason as Editor. **"Admin" here is the name
of this group, not Django's own superuser flag** — `sync_roles.py` only ever grants permissions
on this app's own models (plus one `auditlog` codename), never `auth.change_user` or any other
permission on Django's own `User`/`Group` models, so a member of this group cannot manage
other users' accounts (e.g. change another user's password) unless they are separately made a
Django superuser. See README.md's "Setting up accounts".
