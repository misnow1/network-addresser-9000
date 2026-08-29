"""Conformance check for ADR 0027 — the ordinal is the unit.

Reports whether every static racked device port satisfies

    address == range_base + rack_slot + slot_offset

and lists every port that does not, with the offset its address implies.

Run against the **live estate** through the app container, not `.env` — they
are different databases:

    docker exec -i network-addresser-9000-app-1 \
        python manage.py shell < docs/plans/measure-adr-0027.py

Exit is always 0; this is a report, not a gate. Migration ``0023`` carries its
own assertion, because that one is load-bearing (see PLAN-adr-0027.md,
decision 5).
"""

import ipaddress

from inventory.models import NetworkDevice, NetworkDevicePort, RackVlanRange

print("=== estate ===")
print(
    "devices:",
    NetworkDevice.objects.count(),
    "| racked:",
    NetworkDevice.objects.filter(rack__isnull=False).count(),
    "| device ports:",
    NetworkDevicePort.objects.count(),
)

conforming = 0
divergent = []
unscoped = 0

ports = NetworkDevicePort.objects.filter(is_dhcp=False, device__rack__isnull=False).select_related(
    "device", "device__rack", "vlan", "source_type_port"
)

for port in ports:
    device = port.device
    if port.address is None:
        continue
    rack_range = RackVlanRange.objects.filter(rack=device.rack, vlan=port.vlan).first()
    if rack_range is None:
        # No range for this VLAN on this rack — the address is outside ADR
        # 0027's scope rather than divergent from it.
        unscoped += 1
        continue
    base = ipaddress.IPv4Network(rack_range.address_range, strict=True).network_address
    ordinal = int(ipaddress.IPv4Address(port.address)) - int(base)
    implied = ordinal - device.rack_slot
    if implied == port.slot_offset:
        conforming += 1
    else:
        divergent.append((device, port, ordinal, implied))

total = conforming + len(divergent)
print()
print(f"=== conformance: {conforming}/{total} ===")
if unscoped:
    print(f"({unscoped} port(s) on a VLAN the rack has no range for — out of scope)")

for device, port, ordinal, implied in divergent:
    print(
        f"  {device} | slot={device.rack_slot} | {port.vlan} | {port.address}"
        f" | ordinal={ordinal} | implied_offset={implied}"
        f" | declared={port.slot_offset} | {port.description!r}"
    )

print()
print("=== doubly-claimed ordinals ===")
clashes = 0
for rack in {d.rack for d in NetworkDevice.objects.filter(rack__isnull=False).select_related("rack")}:
    claims: dict[int, list[str]] = {}
    for device in NetworkDevice.objects.filter(rack=rack).select_related("device_type"):
        claims.setdefault(device.rack_slot, []).append(f"{device} (own slot)")
        for port in device.ports.filter(is_dhcp=False).select_related("vlan"):
            if port.address is None:
                continue
            rack_range = RackVlanRange.objects.filter(rack=rack, vlan=port.vlan).first()
            if rack_range is None:
                continue
            base = ipaddress.IPv4Network(rack_range.address_range, strict=True).network_address
            ordinal = int(ipaddress.IPv4Address(port.address)) - int(base)
            if ordinal != device.rack_slot:
                claims.setdefault(ordinal, []).append(f"{device} [{port.description}]")
    for ordinal, holders in sorted(claims.items()):
        if len(holders) > 1:
            clashes += 1
            print(f"  {rack} ordinal {ordinal}: {'; '.join(holders)}")
if not clashes:
    print("  none")

print()
print("=== pre-migration gate (for migration 0023) ===")
# Before 0023 runs, a non-conforming port is EXPECTED: the two Device Control
# ports still declare slot_offset=0, and rewriting them to the offset their
# address already implies is exactly what 0023 does. So the gate is not
# "132/132" -- that is only reachable afterwards. What 0023 needs is that every
# divergent port implies a single non-negative offset it can write, and that
# nothing collides once written.
blockers = []
for device, port, _ordinal, implied in divergent:
    if implied < 0:
        blockers.append(f"{device} [{port.description}] implies a negative offset ({implied})")
if clashes:
    blockers.append(f"{clashes} doubly-claimed ordinal(s)")

if blockers:
    print("  BLOCKED:")
    for blocker in blockers:
        print(f"    - {blocker}")
elif divergent:
    print(f"  GREEN -- {len(divergent)} port(s) awaiting 0023, all implying a positive offset:")
    for device, port, _ordinal, implied in divergent:
        print(f"    - {device} [{port.description}] -> slot_offset={implied}")
else:
    print("  GREEN -- nothing left for 0023 to rewrite (already 132/132)")
