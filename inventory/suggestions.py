"""Pure IPv4 address-suggestion helpers for inventory models.

These functions take already-known network primitives (CIDR strings, slot
counts) and return suggested values. They perform no DB queries and raise
no ``ValidationError``s — callers own translating an absent (``None``)
result into whatever handling makes sense for their model.
"""

import ipaddress


def suggest_default_gateway(subnet: str) -> str | None:
    """Suggested default gateway: the lowest host address in ``subnet``.

    ``None`` for a /32 — a single-address network has no host address
    distinct from the network address itself, and ``network_address + 1``
    would overflow for the top-of-range case (255.255.255.255/32).
    """
    network = ipaddress.IPv4Network(subnet, strict=True)
    if network.num_addresses <= 1:
        return None
    return str(network.network_address + 1)


def suggest_dhcp_range(subnet: str) -> str | None:
    """Suggested DHCP range: the bottom /24 of ``subnet``.

    ``None`` if ``subnet`` is smaller than a /24 — "bottom 256 addresses"
    only equals "bottom /24" when the network is octet-aligned (ADR 0002).
    """
    network = ipaddress.IPv4Network(subnet, strict=True)
    if network.prefixlen > 24:
        return None
    bottom_24 = next(network.subnets(new_prefix=24))
    return str(bottom_24)


def required_block_size(slot_count: int) -> int:
    """Minimum address count a rack-VLAN-range block needs for ``slot_count`` slots.

    Slot N maps to ``network_address + N`` for N in 1..slot_count (see
    ``suggest_slot_address``); the block's own network address (index 0)
    and its top address (index size-1) are both left unassigned — the
    latter so the top slot doesn't end up looking like that block's
    broadcast address, per DESIGN.md's guidance to avoid handing devices
    addresses that read as reserved. So the block needs slots 1..slot_count
    to sit strictly below its top index: ``slot_count + 2`` addresses.
    """
    return slot_count + 2


def prefix_length_for_capacity(slot_count: int) -> int:
    """Smallest IPv4 prefix length whose block satisfies ``required_block_size``."""
    needed = required_block_size(slot_count)
    host_bits = max(needed - 1, 0).bit_length()
    return 32 - host_bits


def suggest_rack_vlan_range(subnet: str, slot_count: int, used_ranges: list[str]) -> str | None:
    """Next free block sized for ``slot_count``, within ``subnet``.

    ``None`` if ``slot_count`` needs more addresses than ``subnet`` has, or
    every same-sized block within ``subnet`` overlaps something in
    ``used_ranges``.
    """
    network = ipaddress.IPv4Network(subnet, strict=True)
    prefixlen = prefix_length_for_capacity(slot_count)
    if prefixlen < network.prefixlen:
        return None
    used = [ipaddress.IPv4Network(r, strict=True) for r in used_ranges]
    for candidate in network.subnets(new_prefix=prefixlen):
        if not any(candidate.overlaps(block) for block in used):
            return str(candidate)
    return None


def suggest_slot_address(range_cidr: str, slot: int) -> str:
    """Suggested address for ``slot`` within ``range_cidr``: base + slot."""
    network = ipaddress.IPv4Network(range_cidr, strict=True)
    return str(network.network_address + slot)


def ranges_overlap(a: str, b: str) -> bool:
    """Whether two IPv4 CIDR ranges overlap at all."""
    return ipaddress.IPv4Network(a, strict=True).overlaps(ipaddress.IPv4Network(b, strict=True))
