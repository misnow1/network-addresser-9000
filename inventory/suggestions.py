"""Pure IPv4 address-suggestion helpers for inventory models.

These functions take already-known network primitives (CIDR strings, slot
counts) and return suggested values. They perform no DB queries and raise
no ``ValidationError``s — callers own translating an absent (``None``)
result into whatever handling makes sense for their model.
"""

import ipaddress
from collections.abc import Iterable, Iterator


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


def required_block_size(slot_count: int) -> int:
    """Minimum address count a rack-VLAN-range block needs for ``slot_count`` slots.

    Slot N maps to ``network_address + N`` for N in 1..slot_count (see
    ``suggest_slot_address``); the block's own network address (index 0)
    and its top address (index size-1) are both left unassigned — the
    latter so the top slot doesn't end up looking like that block's
    broadcast address, per DESIGN.md's guidance to avoid handing devices
    addresses that read as reserved. So the block needs slots 1..slot_count
    to sit strictly below its top index: ``slot_count + 2`` addresses.

    That arithmetic is floored at 32 (a ``/27``), production's uniform
    per-rack increment regardless of occupancy (ADR 0015) — racks holding
    as few as one or two devices still get a full ``/27`` in production, and
    without the floor this tool can reproduce almost none of the existing
    addressing (see ADR 0015 for the replay numbers). The floor lives here,
    at the one place all three call sites (the suggester, hand-entered-range
    validation, and the ``slot_count``-growth guard) inherit it from, rather
    than in each of them separately.
    """
    return max(slot_count + 2, 32)


def prefix_length_for_capacity(slot_count: int) -> int:
    """Smallest IPv4 prefix length whose block satisfies ``required_block_size``."""
    needed = required_block_size(slot_count)
    host_bits = max(needed - 1, 0).bit_length()
    return 32 - host_bits


def iter_free_offsets(
    subnet: str, prefixlen: int, used_ranges: list[str], dhcp_range: tuple[str, str] | None = None
) -> Iterator[int]:
    """Ascending, lazy offsets (from ``subnet``'s own network address) at
    which a ``prefixlen``-sized block overlaps neither ``used_ranges`` nor
    ``dhcp_range``.

    This is the one candidate walk both ``suggest_rack_vlan_range()`` and
    ``suggest_aligned_offset()`` share, so the two provably agree about
    what "free" means. It **never materialises the candidate space** —
    yields are computed one at a time via a plain ``range()`` over block
    indices, which Python itself never expands into a list. That matters
    because ``validators.py``'s ``validate_ipv4_cidr`` permits any legal
    IPv4 CIDR, including a `/0` — a `/27`-sized search over a `/0` subnet
    has 134,217,728 candidates, and an eager "collect them all, then
    filter" implementation would be a catastrophic regression on a path
    that is O(1) today (ADR 0025 plan, review note 1).

    Nothing (an empty generator, not an error) when ``prefixlen`` needs
    more addresses than ``subnet`` has — mirrors ``suggest_rack_vlan_
    range()``'s pre-ADR-0025 ``prefixlen < network.prefixlen`` guard
    exactly, just expressed as "yields nothing" instead of "returns None"
    so ``next(..., None)`` reproduces the old return value unchanged.
    """
    network = ipaddress.IPv4Network(subnet, strict=True)
    if prefixlen < network.prefixlen:
        return
    block_size = 1 << (32 - prefixlen)
    used = [ipaddress.IPv4Network(r, strict=True) for r in used_ranges]
    num_blocks = network.num_addresses // block_size
    for block_index in range(num_blocks):
        offset = block_index * block_size
        candidate = ipaddress.IPv4Network((int(network.network_address) + offset, prefixlen), strict=True)
        if any(candidate.overlaps(block) for block in used):
            continue
        if dhcp_range is not None and dhcp_range_overlaps_cidr(dhcp_range[0], dhcp_range[1], str(candidate)):
            continue
        yield offset


def suggest_rack_vlan_range(
    subnet: str, slot_count: int, used_ranges: list[str], dhcp_range: tuple[str, str] | None = None
) -> str | None:
    """Next free block sized for ``slot_count``, within ``subnet``.

    ``None`` if ``slot_count`` needs more addresses than ``subnet`` has, or
    every same-sized block within ``subnet`` overlaps something in
    ``used_ranges`` or ``dhcp_range``.

    ``dhcp_range`` (a ``(start, end)`` address pair) is checked separately
    from ``used_ranges`` (CIDR blocks) since it isn't itself CIDR-shaped —
    see ``dhcp_range_overlaps_cidr``.

    Behaviour-identical to the pre-ADR-0025 implementation — including its
    ``prefixlen < network.prefixlen`` guard, its ``dhcp_range`` handling
    and its ``None`` returns — but now expressed as "the first offset
    ``iter_free_offsets()`` yields," so this and ``suggest_aligned_
    offset()`` are provably searching for the same thing (ADR 0025).
    """
    prefixlen = prefix_length_for_capacity(slot_count)
    offset = next(iter_free_offsets(subnet, prefixlen, used_ranges, dhcp_range), None)
    if offset is None:
        return None
    return range_at_offset(subnet, offset, slot_count)


def range_offset(subnet: str, range_cidr: str) -> int:
    """A stored block's offset from ``subnet``'s own network address.

    The one definition of "offset" both the allocator and the rack-level
    divergence report (ADR 0025) use. **Strict**: raises ``ValueError`` on
    a malformed ``subnet`` or ``range_cidr`` rather than catching it —
    this module's own docstring already promises purity and no error
    handling, so tolerance (a rack range or VLAN subnet that bypassed
    ``clean()`` via a bare ``save()``) belongs in the model layer, not
    here. See ``RackVlanRange.offset`` for the tolerant wrapper.

    Does **not** check that ``range_cidr`` actually lies within ``subnet``
    — that containment check already exists at
    ``RackVlanRange._validate_range()`` and is repeated by ``RackVlanRange.
    offset`` for exactly the same reason: a value outside its own VLAN's
    subnet is malformed in a way this pure arithmetic has no opinion
    about, and the model layer is where "malformed" already gets decided.
    """
    network = ipaddress.IPv4Network(subnet, strict=True)
    range_network = ipaddress.IPv4Network(range_cidr, strict=True)
    return int(range_network.network_address) - int(network.network_address)


def range_at_offset(subnet: str, offset: int, slot_count: int) -> str:
    """The inverse of ``range_offset()``: the CIDR block sized for
    ``slot_count`` sitting ``offset`` addresses above ``subnet``'s network
    address.

    Callers are trusted to have already confirmed ``offset`` is a valid
    block boundary within ``subnet`` (``suggest_aligned_offset()`` and
    ``iter_free_offsets()`` only ever produce such offsets) — this is the
    inverse of a value ``range_offset()`` would itself produce, not a
    fresh validation of an arbitrary integer.
    """
    network = ipaddress.IPv4Network(subnet, strict=True)
    prefixlen = prefix_length_for_capacity(slot_count)
    candidate = ipaddress.IPv4Network((int(network.network_address) + offset, prefixlen), strict=True)
    return str(candidate)


def suggest_aligned_offset(
    vlans: list[tuple[str, list[str], tuple[str, str] | None]], slot_count: int
) -> int | None:
    """The lowest offset free on **every** VLAN in ``vlans`` — each a
    ``(subnet, used_ranges, dhcp_range)`` triple, the same shape ``suggest_
    rack_vlan_range()``'s own arguments already take, just batched.

    Implemented as an ascending merge over each VLAN's own ``iter_free_
    offsets()``: advance whichever iterator(s) are lagging behind the
    current maximum, and return the moment every iterator agrees. This
    stops at the first joint hit — it never walks a whole subnet, even
    though the offsets it's comparing come from VLANs of different sizes
    (ADR 0025's "one offset across VLANs of different sizes" claim rests
    on ``network_address + k * block_size`` always landing on a valid
    CIDR boundary on every VLAN, since each is aligned to its own prefix
    and the block is never larger than the subnet).

    ``None`` when no such offset exists — including when ``vlans`` is
    empty, or any single VLAN's subnet is smaller than the block
    ``slot_count`` needs (that VLAN's ``iter_free_offsets()`` yields
    nothing at all, so the merge can never converge).
    """
    if not vlans:
        return None
    prefixlen = prefix_length_for_capacity(slot_count)
    iterators = [
        iter_free_offsets(subnet, prefixlen, used_ranges, dhcp_range)
        for subnet, used_ranges, dhcp_range in vlans
    ]
    try:
        current = [next(it) for it in iterators]
    except StopIteration:
        return None
    while True:
        target = max(current)
        if all(offset == target for offset in current):
            return target
        for index, iterator in enumerate(iterators):
            while current[index] < target:
                try:
                    current[index] = next(iterator)
                except StopIteration:
                    return None


def suggest_slot_address(range_cidr: str, slot: int) -> str:
    """Suggested address for ``slot`` within ``range_cidr``: base + slot."""
    network = ipaddress.IPv4Network(range_cidr, strict=True)
    return str(network.network_address + slot)


def lowest_free_run(occupied: Iterable[tuple[int, int]], span: int, slot_count: int) -> int | None:
    """Lowest 1-based start of ``span`` consecutive free ordinals in 1..slot_count.

    ``None`` if ``span`` is non-positive, bigger than ``slot_count`` itself,
    or every run that size is blocked by ``occupied``. Tolerates unsorted
    and overlapping ``occupied`` input on purpose — a caller unioning two
    equipment tables plus an in-flight candidate range should not have to
    normalise first.
    """
    if span < 1 or slot_count < span:
        return None
    cursor = 1
    for start, end in sorted(occupied):
        if end < cursor:
            continue  # already behind the cursor
        if start - cursor >= span:
            return cursor  # the gap before this range fits
        cursor = max(cursor, end + 1)
    return cursor if cursor + span - 1 <= slot_count else None


def ranges_overlap(a: str, b: str) -> bool:
    """Whether two IPv4 CIDR ranges overlap at all."""
    return ipaddress.IPv4Network(a, strict=True).overlaps(ipaddress.IPv4Network(b, strict=True))


def dhcp_range_overlaps_cidr(start: str, end: str, cidr: str) -> bool:
    """Whether IPv4 address range ``[start, end]`` overlaps CIDR block ``cidr``.

    ``start``/``end`` are normalized if reversed — callers are expected to
    have already enforced ``start < end`` (e.g. ``VLAN.clean()``), but that
    isn't a DB-level guarantee (a string-based ordering CheckConstraint can't
    express IPv4 ordering correctly), so a reversed pair reaching this
    function is treated as the same range rather than silently producing a
    wrong overlap answer.
    """
    start_addr = ipaddress.IPv4Address(start)
    end_addr = ipaddress.IPv4Address(end)
    if start_addr > end_addr:
        start_addr, end_addr = end_addr, start_addr
    network = ipaddress.IPv4Network(cidr, strict=True)
    return start_addr <= network.broadcast_address and end_addr >= network.network_address
