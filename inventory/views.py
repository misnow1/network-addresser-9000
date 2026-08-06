"""Read-only UI views (phase 15, ADR 0020).

Mounted at ``/`` (see ``inventory/urls.py``); ``/admin/`` is untouched and
remains the *only* place anything in this project is written. Every view
here is a plain ``GET`` — ``@require_GET`` on all five makes that structural
rather than aspirational (an ordinary Django function view otherwise
accepts ``POST`` unless told not to), and ``test_ui.py``'s writes-nothing
sweep is the test that actually proves it.

Two module-level helpers do the work that keeps the five views themselves
thin — read their docstrings first:

* ``safe_slot_address`` — the read-only mirror of
  ``_suggest_rack_slot_address``'s validate-then-catch discipline
  (``models.py:289-320``), for callers that already have a range's CIDR
  text and an ordinal rather than a ``Rack``/``rack_slot`` pair to look one
  up from.
* ``resolve_slot_spans`` — bulk-resolves ``NetworkDeviceType.slot_span``
  (ADR 0017) for every device on a page in one grouped query, because the
  property itself runs a fresh ``aggregate(Max(...))`` on *every* access
  (``models.py:2689``) and calling it once per device is an N+1 that scales
  with rack occupancy.

Access control is uniform across all five views — see each view's
decorator stack. ``login_required`` is outermost deliberately:
``permission_required(raise_exception=True)`` 403s an *anonymous* visitor
too, so if it ran first a logged-out visitor would get a 403 instead of the
login redirect this app promises. Each view declares the full set of
``view_*`` codenames it actually reads (not one token model) — in
practice every role holds every ``view_`` codename via ``sync_roles.py``,
so this is a floor rather than a narrowing today, but a hand-built user
with partial grants must not see data it lacks the codename for.
"""

import ipaddress
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import Count, Max, Prefetch, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from .models import (
    VLAN,
    NetworkDevice,
    NetworkDevicePort,
    NetworkDeviceTypePort,
    NetworkSwitch,
    NetworkSwitchAddress,
    Rack,
    RackVlanRange,
)
from .suggestions import suggest_rack_vlan_range, suggest_slot_address
from .validators import validate_ipv4_cidr


def safe_slot_address(range_cidr: str, ordinal: int) -> str | None:
    """The address ``ordinal`` would get within ``range_cidr`` — or ``None``.

    Mirrors ``_suggest_rack_slot_address``'s existing validate-then-catch
    discipline (``models.py:289-320``): validate the CIDR text, then ask
    ``suggest_slot_address`` for the address, and swallow the ways that
    can go wrong rather than letting any of them propagate into a 500 —
    or, just as important, into a wrong-but-plausible-looking address.

    Three failure modes are real, not hypothetical, on data the *write*
    path already allows onto this table: a ``RackVlanRange.address_range``
    saved via a bare ``.save()`` bypasses ``clean()`` entirely and can
    hold malformed text (``ValidationError`` from ``validate_ipv4_cidr``);
    an ordinal far enough past a block's own top can push the arithmetic
    past the top of the whole IPv4 address space, which
    ``suggest_slot_address`` reports as a plain ``ValueError`` (the
    ``ipaddress`` module's own overflow signal); and — the one an earlier
    revision missed (Codex review) — a range that's syntactically valid
    CIDR but *undersized* for the rack it's attached to (also only
    reachable by a ``clean()``-bypassing ``save()``, since
    ``Rack.clean()``/``RackVlanRange.clean()`` both enforce
    ``required_block_size(rack.slot_count)`` against every stored range)
    doesn't raise at all: ``network_address + ordinal`` is simple integer
    arithmetic with no awareness of the block's own boundary, so a
    ``/30`` on a 10-slot rack happily computes ``ordinal=9`` as an address
    four addresses past the block's actual top. That's not a crash, but
    it is a wrong answer confidently presented as the address a slot
    would get, which is worse — so containment is checked explicitly
    rather than left to ``suggest_slot_address``'s arithmetic to notice.
    A read-only page must never 500 on data the write path allowed
    (review note 3 of ``PLAN-read-only-ui.md``), and must not assert a
    false one either; all three failure modes become ``None`` here, which
    every caller renders as a blank cell.
    """
    try:
        validate_ipv4_cidr(range_cidr)
        network = ipaddress.IPv4Network(range_cidr, strict=True)
    except ValidationError:
        return None
    try:
        candidate = suggest_slot_address(range_cidr, ordinal)
    except ValueError:
        return None
    if ipaddress.IPv4Address(candidate) not in network:
        return None
    return candidate


def resolve_slot_spans(devices: Iterable[NetworkDevice]) -> dict[int, int]:
    """Bulk-resolve ``NetworkDeviceType.slot_span`` (ADR 0017) for every
    device type represented in ``devices``, as ``{device_type_id: span}``.

    ``NetworkDeviceType.slot_span`` is deliberately computed, not stored
    (``models.py:2673-2690``'s docstring explains why), but that means it
    runs its own ``aggregate(Max(...))`` query on *every* access — even
    with ``device_type`` prefetched onto each device, the property itself
    still hits the database once per call. Calling it once per device on a
    rack elevation is an N+1 that scales with occupancy (plan review note
    2), which the query-budget test below catches by asserting *equal*
    query counts for a 2-device and a 50-device rack rather than recording
    a single after-the-fact number that would just bless whatever the
    implementation happened to do.

    This evaluates the exact same rule as the property — ``max(slot_offset)
    + 1`` across a type's ports, or ``1`` for a type with no offset ports —
    but against every type on the page in one ``GROUP BY`` query. The
    property stays the single source of the *rule*; this is a bulk
    evaluation of it, not a second definition, and
    ``test_ui.py`` asserts the two agree for every type in its fixtures so
    they can never quietly drift apart.
    """
    device_type_ids = {device.device_type_id for device in devices}
    if not device_type_ids:
        return {}
    aggregated = (
        NetworkDeviceTypePort.objects.filter(device_type_id__in=device_type_ids)
        .values("device_type_id")
        .annotate(max_offset=Max("slot_offset"))
    )
    spans = {row["device_type_id"]: row["max_offset"] + 1 for row in aggregated}
    # A type with zero type ports produces no row at all above (the
    # aggregate is an inner join through the reverse FK) — default it to
    # span 1 explicitly rather than leaving it absent and pushing that
    # default onto every caller.
    for device_type_id in device_type_ids:
        spans.setdefault(device_type_id, 1)
    return spans


# ---------------------------------------------------------------------------
# Rack elevation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotAddress:
    """One address rendered into an elevation cell or the device-detail
    port table — a cell holds a *list* of these, not a single value
    (review note 1): ``NetworkDevicePort`` is unique on ``(device,
    description)`` and ``(device, ordinal)``, not on ``(device, vlan,
    slot_offset)``, so nothing stops two ports from sharing one cell (the
    ``#27`` bridged-jack shape the roadmap still carries as unsolved).
    """

    value: str  # the address, or the literal string "DHCP"
    description: str  # the port's purpose, e.g. "Control"
    derived: bool  # True for a slot_offset > 0 port (ADR 0017) — read-only


@dataclass(frozen=True)
class ElevationCell:
    """One (ordinal, VLAN) intersection in the rack elevation grid.

    ``state`` is one of:

    * ``"empty"``   — no occupant at all; ``would_be_address`` is what
      ``safe_slot_address`` returns for this ordinal on this VLAN's range
      (``None`` renders blank, not a crash).
    * ``"occupied"`` — the occupant has one or more addresses here.
    * ``"absent"``  — the occupant has *no* port/address on this VLAN at
      all, at any offset. Renders as an em-dash: absence, not missing
      data (``PROD-DATA-ANALYSIS.md`` §5.4's 34-of-229 phantom-interface
      addresses are exactly the failure this stops from recurring).
    * ``"blank"``   — the occupant has a port on this VLAN, but not at
      *this* ordinal's offset (only reachable for a multi-offset device
      with ports on the same VLAN at some but not all of its offsets).
      Renders nothing: it isn't absence (the device does use this VLAN)
      and it isn't an address (not at this row), so neither an em-dash
      nor a value would be honest here.
    * ``"conflict"`` — more than one occupant claims this ordinal (see
      ``ElevationRow.conflicts``). Carries no addresses of its own — with
      two occupants disputing the slot, there is no way to say which
      one's data belongs in this cell, so the cell says nothing rather
      than guessing.
    """

    state: str
    addresses: list[SlotAddress] = field(default_factory=list)
    would_be_address: str | None = None


@dataclass(frozen=True)
class TetherInfo:
    """The other half of an ADR 0018 companion pair, for the dashed-tether
    badge — deliberately not a positional line drawn across rows. A pair's
    two rack slots can be arbitrarily far apart (production has both a
    below and an above example on consoles from the same manufacturer),
    and this project has no JavaScript to draw a connector across an
    unbounded row distance. An in-page anchor link plus this pk is the
    encoding instead, and it is checkable exactly where a drawn line
    wouldn't be: by pk, not by pixel position.

    ``ordinal`` is ``None`` when this half of the pair is unracked —
    ADR 0018's companion link is an existence/lifecycle relationship, true
    whether or not either half currently has a rack slot (a host and
    companion materialize together, racked or not, and the spare pool is
    an entirely legitimate state for both). A ``None`` ordinal means the
    badge has no in-page row to anchor to (device-detail's spare-pool
    case), not that the relationship itself is somehow absent.
    """

    pk: int
    label: str
    ordinal: int | None


@dataclass(frozen=True)
class Occupant:
    """The device or switch that owns one or more elevation rows."""

    kind: str  # "switch" | "device"
    label: str
    admin_url: str
    detail_url: str | None  # inventory:device for a device; None for a switch (no Stage A view yet)
    row_kind: str  # "start" | "continuation" — this row's position within the occupant's span
    span: int  # 1 for a switch or an ordinary device; > 1 only for an ADR 0017 offset device
    is_span_end: bool  # True on the last ordinal of a multi-ordinal span
    tether: TetherInfo | None = None

    @property
    def bracketed(self) -> bool:
        return self.span > 1


@dataclass(frozen=True)
class ConflictOccupant:
    """One claimant in an ``ElevationRow.conflicts`` list — a minimal
    label + admin link, not a full ``Occupant``: with the ordinal
    genuinely disputed, there's no single span/tether/cell state that
    could honestly describe "this" occupant of the row, so a conflict
    entry carries only enough to identify and link to each claimant.
    """

    kind: str  # "switch" | "device"
    label: str
    admin_url: str


@dataclass(frozen=True)
class ElevationRow:
    ordinal: int
    occupant: Occupant | None
    cells: list[ElevationCell]
    add_url: str | None = None  # only set when occupant is None
    # Non-empty only when more than one switch/device claims this ordinal
    # — see _build_occupancy's docstring for how that becomes reachable
    # despite the DB's own unique-slot constraints.
    conflicts: list[ConflictOccupant] = field(default_factory=list)


@dataclass(frozen=True)
class _OccupancyEntry:
    kind: str  # "switch" | "device"
    obj: NetworkSwitch | NetworkDevice
    start: int
    row_kind: str  # "start" | "continuation"


def _build_occupancy(
    switches: Iterable[NetworkSwitch], devices: Iterable[NetworkDevice], spans: dict[int, int]
) -> dict[int, list[_OccupancyEntry]]:
    """``{ordinal: [_OccupancyEntry, ...]}`` covering every ordinal a
    switch or device claims — not just each occupant's own ``rack_slot``
    (review note 1). A switch always claims exactly its own slot; a
    device claims ``rack_slot .. rack_slot + span - 1`` where ``span``
    comes from ``spans`` (``resolve_slot_spans``'s output), never from a
    per-device ``slot_span`` property access.

    A **list** per ordinal, not a single entry (Codex review round 2,
    finding 4) — the DB's ``unique(rack, rack_slot)`` constraint only
    guarantees no two rows share a *starting* ordinal; it says nothing
    about a spanning device's continuation ordinals (ADR 0017), whose
    overlap is checked only in ``clean()``, not by the schema (see
    ``RackSlotAssignmentMixin``'s own "Known gap" docstring and
    ``ROADMAP.md``'s "rack slot occupancy has no DB-level overlap
    guarantee" item). A direct ``objects.create()`` — which never calls
    ``full_clean()`` — can therefore leave two occupants claiming one
    ordinal. Overwriting one entry with the other in this dict would
    silently drop an occupant from a page whose entire point is showing
    what's actually racked; appending instead means ``_build_elevation_
    rows`` can detect the collision and surface it rather than hide it.
    """
    occupancy: dict[int, list[_OccupancyEntry]] = defaultdict(list)
    for switch in switches:
        if switch.rack_slot is None:
            continue
        occupancy[switch.rack_slot].append(
            _OccupancyEntry(kind="switch", obj=switch, start=switch.rack_slot, row_kind="start")
        )
    for device in devices:
        if device.rack_slot is None:
            continue
        span = spans.get(device.device_type_id, 1)
        for offset in range(span):
            occupancy[device.rack_slot + offset].append(
                _OccupancyEntry(
                    kind="device",
                    obj=device,
                    start=device.rack_slot,
                    row_kind="start" if offset == 0 else "continuation",
                )
            )
    return dict(occupancy)


def _conflict_occupant(entry: _OccupancyEntry) -> ConflictOccupant:
    if entry.kind == "switch":
        switch = entry.obj
        assert isinstance(switch, NetworkSwitch)
        return ConflictOccupant(
            kind="switch",
            label=str(switch),
            admin_url=reverse("admin:inventory_networkswitch_change", args=[switch.pk]),
        )
    device = entry.obj
    assert isinstance(device, NetworkDevice)
    return ConflictOccupant(
        kind="device",
        label=str(device),
        admin_url=reverse("admin:inventory_networkdevice_change", args=[device.pk]),
    )


def _device_port_index(
    device: NetworkDevice,
) -> tuple[dict[tuple[int, int], list[NetworkDevicePort]], set[int]]:
    """``({(vlan_id, slot_offset): [ports]}, {vlan_id with any port})`` for
    one device's already-prefetched ports — computed once per device, not
    once per cell, since a spanning device's ports are consulted once per
    row it occupies.
    """
    by_offset_vlan: dict[tuple[int, int], list[NetworkDevicePort]] = defaultdict(list)
    vlans_with_port: set[int] = set()
    for port in device.ports.all():
        by_offset_vlan[(port.vlan_id, port.slot_offset)].append(port)
        vlans_with_port.add(port.vlan_id)
    return by_offset_vlan, vlans_with_port


def _tether_for(device: NetworkDevice) -> TetherInfo | None:
    """This device's companion-pair badge (ADR 0018), if it is either half
    of one — the host (has a ``companion``) or the companion itself (has
    ``host_id`` set). ``None`` for a device with no companion relationship
    at all, which is the ordinary case for most types.

    Rendered whenever either half of the pair *exists*, regardless of
    whether it currently has a rack slot (Codex review round 2, finding
    6) — ADR 0018's companion link is existence and lifecycle, not
    addressing: a host materializes its companion in the same transaction
    whether or not it's racked (``_materialize_companion()``), so an
    unracked host in the spare pool has a real, existing companion just
    as much as a racked one does. An earlier revision required
    ``rack_slot is not None`` on the partner before returning anything,
    which hid the relationship entirely for any unracked pair —
    contradicting the ADR's own point that the link doesn't come and go
    with placement. ``TetherInfo.ordinal`` is ``None`` in that case; it's
    the caller's job to render "spare pool" instead of a slot number.
    """
    try:
        companion = device.companion
    except ObjectDoesNotExist:
        companion = None
    if companion is not None:
        return TetherInfo(pk=companion.pk, label=str(companion), ordinal=companion.rack_slot)
    host = device.host
    if host is not None:
        return TetherInfo(pk=host.pk, label=str(host), ordinal=host.rack_slot)
    return None


def _empty_cell(column: RackVlanRange, ordinal: int) -> ElevationCell:
    return ElevationCell(state="empty", would_be_address=safe_slot_address(column.address_range, ordinal))


def _switch_row(columns: list[RackVlanRange], switch: NetworkSwitch) -> tuple[Occupant, list[ElevationCell]]:
    """A switch's single occupied row — always ``span=1``: switches have no
    offset concept (``NetworkSwitchAddress`` carries no ``slot_offset``),
    so a switch never has anything for a bracket or a continuation row to
    express.
    """
    addresses_by_vlan = {address.vlan_id: address for address in switch.addresses.all()}
    cells = []
    for column in columns:
        address = addresses_by_vlan.get(column.vlan_id)
        if address is None or address.address is None:
            # The DB CheckConstraint networkswitchaddress_address_required
            # guarantees a real row is never null here; the isinstance-style
            # guard is only to satisfy mypy about GenericIPAddressField's
            # nullable type.
            cells.append(ElevationCell(state="absent"))
        else:
            slot_address = SlotAddress(value=address.address, description=str(column.vlan), derived=False)
            cells.append(ElevationCell(state="occupied", addresses=[slot_address]))
    occupant = Occupant(
        kind="switch",
        label=str(switch),
        admin_url=reverse("admin:inventory_networkswitch_change", args=[switch.pk]),
        detail_url=None,
        row_kind="start",
        span=1,
        is_span_end=True,
        tether=None,
    )
    return occupant, cells


def _device_row(
    columns: list[RackVlanRange], ordinal: int, entry: _OccupancyEntry, span: int
) -> tuple[Occupant, list[ElevationCell]]:
    """One of a device's occupied rows — ``entry.row_kind`` says whether
    this is the device's own ``rack_slot`` ("start") or one of the
    ordinals its ADR 0017 offset ports claim beyond it ("continuation").
    Every VLAN column is resolved independently per row: a port whose
    ``slot_offset`` equals this row's offset from the device's start
    renders its address; a VLAN the device has no port on at all renders
    an em-dash; a VLAN the device *does* use, just not at this row's
    offset, renders blank (neither absence nor an address is honest there
    — see ``ElevationCell``).
    """
    device = entry.obj
    assert isinstance(device, NetworkDevice)
    offset = ordinal - entry.start
    by_offset_vlan, vlans_with_port = _device_port_index(device)
    cells = []
    for column in columns:
        ports = by_offset_vlan.get((column.vlan_id, offset), [])
        if ports:
            addresses = [
                SlotAddress(
                    value="DHCP" if port.is_dhcp else (port.address or ""),
                    description=port.description,
                    derived=port.slot_offset > 0,
                )
                for port in ports
            ]
            cells.append(ElevationCell(state="occupied", addresses=addresses))
        elif column.vlan_id in vlans_with_port:
            cells.append(ElevationCell(state="blank"))
        else:
            cells.append(ElevationCell(state="absent"))

    occupant = Occupant(
        kind="device",
        label=str(device),
        admin_url=reverse("admin:inventory_networkdevice_change", args=[device.pk]),
        detail_url=reverse("inventory:device", args=[device.pk]),
        row_kind=entry.row_kind,
        span=span,
        is_span_end=(offset == span - 1),
        tether=_tether_for(device),
    )
    return occupant, cells


def _build_elevation_rows(
    rack: Rack,
    columns: list[RackVlanRange],
    switches: list[NetworkSwitch],
    devices: list[NetworkDevice],
    spans: dict[int, int],
) -> list[ElevationRow]:
    """One ``ElevationRow`` per ordinal ``1..rack.slot_count``, built from
    an occupancy map that already accounts for multi-ordinal spans (ADR
    0017) — see ``_build_occupancy``.
    """
    occupancy = _build_occupancy(switches, devices, spans)
    rows = []
    for ordinal in range(1, rack.slot_count + 1):
        entries = occupancy.get(ordinal, [])
        if not entries:
            cells = [_empty_cell(column, ordinal) for column in columns]
            add_url = (
                f"{reverse('admin:inventory_networkdevice_add')}?"
                f"{urlencode({'rack': rack.pk, 'rack_slot': ordinal})}"
            )
            rows.append(ElevationRow(ordinal=ordinal, occupant=None, cells=cells, add_url=add_url))
            continue

        if len(entries) > 1:
            # More than one occupant claims this ordinal — a state the
            # write path's clean()-time checks are supposed to prevent but
            # can't guarantee at the DB level (see _build_occupancy).
            # Surface every claimant rather than silently keeping only one
            # (Codex review round 2, finding 4): no cell state here can
            # honestly attribute an address to a disputed slot, so every
            # column renders "conflict" and carries nothing.
            conflicts = [_conflict_occupant(entry) for entry in entries]
            cells = [ElevationCell(state="conflict") for _ in columns]
            rows.append(ElevationRow(ordinal=ordinal, occupant=None, cells=cells, conflicts=conflicts))
            continue

        entry = entries[0]
        if entry.kind == "switch":
            switch = entry.obj
            assert isinstance(switch, NetworkSwitch)
            occupant, cells = _switch_row(columns, switch)
        else:
            device = entry.obj
            assert isinstance(device, NetworkDevice)
            span = spans.get(device.device_type_id, 1)
            occupant, cells = _device_row(columns, ordinal, entry, span)
        rows.append(ElevationRow(ordinal=ordinal, occupant=occupant, cells=cells))
    return rows


@login_required
@require_GET
@permission_required(
    [
        # The four base models the elevation is built from...
        "inventory.view_rack",
        "inventory.view_vlan",
        "inventory.view_networkswitch",
        "inventory.view_networkdevice",
        # ...plus the child rows whose own field values render into every
        # cell, which an earlier revision omitted (Codex review round 2,
        # finding 2 — "each view declares the full set of codenames it
        # actually reads" was the plan's own rule, applied incompletely):
        # RackVlanRange.address_range (the column headers), switch and
        # device port addresses.
        "inventory.view_rackvlanrange",
        "inventory.view_networkswitchaddress",
        "inventory.view_networkdeviceport",
    ],
    raise_exception=True,
)
def rack_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """The rack elevation — the centrepiece view. Rows are ordinals
    ``1..rack.slot_count``; columns are the rack's VLANs
    (``rack.vlan_ranges`` ordered by ``vlan__vlan_id``). Indexed by
    ordinal, not by device, which is what gives a spanning device (ADR
    0017) somewhere to put its second address — a one-row-per-device grid
    can't express that.

    Four encodings, each guarding a specific documented failure — see
    ``ElevationCell``/``Occupant``/``TetherInfo`` for the detail of each:
    a solid bracket for an ADR 0017 span, a dashed tether for an ADR 0018
    companion pair (deliberately unlike the bracket — a companion's
    address bears no relation to its host's), an em-dash for "no port on
    this VLAN" (absence, not missing data), and a greyed would-be address
    on a genuinely empty ordinal.

    Query budget: every relation this view needs is prefetched once,
    up front, and ``resolve_slot_spans`` resolves every device's span in
    one grouped query rather than touching ``NetworkDeviceType.slot_span``
    per device — see that function's docstring. The result is a query
    count independent of how many switches/devices the rack holds, which
    ``test_ui.py`` locks in by asserting equal counts for a 2-device and a
    50-device rack.
    """
    vlan_range_qs = RackVlanRange.objects.select_related("vlan").order_by("vlan__vlan_id")
    switch_qs = NetworkSwitch.objects.select_related("switch_type").prefetch_related(
        Prefetch("addresses", queryset=NetworkSwitchAddress.objects.select_related("vlan"))
    )
    device_qs = NetworkDevice.objects.select_related("device_type", "host").prefetch_related(
        Prefetch("ports", queryset=NetworkDevicePort.objects.select_related("vlan")),
        "companion",
    )
    rack = get_object_or_404(
        Rack.objects.prefetch_related(
            Prefetch("vlan_ranges", queryset=vlan_range_qs),
            Prefetch("switches", queryset=switch_qs),
            Prefetch("devices", queryset=device_qs),
        ),
        pk=pk,
    )
    columns = list(rack.vlan_ranges.all())
    switches = list(rack.switches.all())
    devices = list(rack.devices.all())
    spans = resolve_slot_spans(devices)
    rows = _build_elevation_rows(rack, columns, switches, devices, spans)
    return render(request, "inventory/rack_detail.html", {"rack": rack, "columns": columns, "rows": rows})


# ---------------------------------------------------------------------------
# Address map
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VlanMapSegment:
    rack: Rack
    address_range: str
    left_pct: float
    width_pct: float


@dataclass(frozen=True)
class AddressEntry:
    address: str
    owner_label: str
    owner_url: str
    derived: bool


def _conventional_dhcp_block(network: ipaddress.IPv4Network) -> str:
    """The bottom /24 of ``network`` — DESIGN.md's "static rack allocation
    is conventionally kept out of the bottom /24" convention — or the
    whole subnet, when it's smaller than one /24 to begin with (matching
    ``bottom_24_width_pct``'s own ``min(256, network.num_addresses)``).

    Any network address for a prefix length <= 24 is automatically
    24-bit-aligned: alignment for prefix length P requires the address be
    a multiple of ``2**(32-P)``, and for P<=24 that exponent is >= 8, so
    it's always also a multiple of ``2**8`` — a coarser alignment implies
    every finer one down to /24. This always produces a syntactically
    valid, properly-aligned CIDR block, never a ``strict=True`` failure.
    """
    if network.prefixlen <= 24:
        return str(ipaddress.IPv4Network((network.network_address, 24)))
    return str(network)


def _vlan_addresses_in_use(vlan: VLAN) -> list[AddressEntry]:
    """Every static address on ``vlan`` — switch addresses and device
    ports alike — sorted numerically (not lexically: ``"10.200.9.0"`` >
    ``"10.200.10.0"`` as strings) and linking each to its owner: a rack
    elevation for a switch (no shaped switch view exists yet — Stage B),
    the device-detail page for a device.
    """
    entries = []
    for address in NetworkSwitchAddress.objects.filter(vlan=vlan).select_related("switch"):
        if address.switch.rack_id is None or address.address is None:
            # Both guaranteed non-null by DB constraints (a static switch
            # address only ever exists on a racked switch, and
            # networkswitchaddress_address_required); guarded anyway, both
            # for defensiveness and to satisfy mypy about the nullable
            # field types.
            continue
        entries.append(
            AddressEntry(
                address=address.address,
                owner_label=str(address.switch),
                owner_url=reverse("inventory:rack", args=[address.switch.rack_id]),
                derived=False,
            )
        )
    for port in NetworkDevicePort.objects.filter(vlan=vlan, address__isnull=False).select_related("device"):
        entries.append(
            AddressEntry(
                address=port.address or "",
                owner_label=str(port.device),
                owner_url=reverse("inventory:device", args=[port.device_id]),
                derived=port.slot_offset > 0,
            )
        )

    def _sort_key(entry: AddressEntry) -> tuple[int, int]:
        try:
            return (0, int(ipaddress.IPv4Address(entry.address)))
        except ValueError:
            return (1, 0)  # malformed stored value; sorts last rather than crashing the page

    entries.sort(key=_sort_key)
    return entries


@login_required
@require_GET
@permission_required(
    [
        "inventory.view_vlan",
        "inventory.view_rack",
        "inventory.view_networkswitch",
        "inventory.view_networkdevice",
        # The colored-segment labels are RackVlanRange.address_range, and
        # the "addresses in use" table below the map is switch/device
        # port field values (see rack_detail's comment — same finding).
        "inventory.view_rackvlanrange",
        "inventory.view_networkswitchaddress",
        "inventory.view_networkdeviceport",
    ],
    raise_exception=True,
)
def vlan_map(request: HttpRequest, pk: int) -> HttpResponse:
    """The shape of one VLAN's subnet: which blocks are taken, by which
    rack, and where the next one would land.

    An L2-only VLAN (``subnet == ""``, ADR 0012 — the seeded VLAN 1 is one)
    renders an explicit "no tracked addressing" state rather than reaching
    ``suggest_rack_vlan_range``, which constructs ``IPv4Network("")`` and
    raises (review note 3). A malformed non-blank ``subnet`` — reachable
    only by a ``clean()``-bypassing write — gets the same treatment for the
    same reason, one level up.
    """
    vlan = get_object_or_404(VLAN, pk=pk)
    if not vlan.subnet:
        return render(request, "inventory/vlan_map.html", {"vlan": vlan, "unavailable_reason": "l2_only"})
    try:
        validate_ipv4_cidr(vlan.subnet)
        network = ipaddress.IPv4Network(vlan.subnet, strict=True)
    except ValidationError:
        return render(request, "inventory/vlan_map.html", {"vlan": vlan, "unavailable_reason": "malformed"})

    rack_ranges = list(vlan.rack_ranges.select_related("rack").all())
    segments = []
    used_ranges = []
    for rack_range in rack_ranges:
        try:
            validate_ipv4_cidr(rack_range.address_range)
        except ValidationError:
            continue  # that range's own malformed value; nothing to place on the map
        range_network = ipaddress.IPv4Network(rack_range.address_range, strict=True)
        offset = int(range_network.network_address) - int(network.network_address)
        segments.append(
            VlanMapSegment(
                rack=rack_range.rack,
                address_range=rack_range.address_range,
                left_pct=offset / network.num_addresses * 100,
                width_pct=range_network.num_addresses / network.num_addresses * 100,
            )
        )
        used_ranges.append(rack_range.address_range)
    segments.sort(key=lambda segment: segment.left_pct)

    bottom_24_width_pct = min(256, network.num_addresses) / network.num_addresses * 100

    dhcp_range = None
    if vlan.dhcp_range_start and vlan.dhcp_range_end:
        try:
            ipaddress.IPv4Address(vlan.dhcp_range_start)
            ipaddress.IPv4Address(vlan.dhcp_range_end)
        except ValueError:
            pass  # VLAN's own malformed range; its own admin page reports that
        else:
            dhcp_range = (vlan.dhcp_range_start, vlan.dhcp_range_end)

    # slot_count=1 forces ADR 0015's /27 floor (required_block_size(1) ==
    # 32) as the reference size — ADR 0019 made offset space reservable
    # with an empty Rack, so this suggestion is a backstop against nobody
    # having reserved a gap, not the only guard against landing in one.
    #
    # The conventional bottom /24 is added to the exclusion set below in
    # addition to used_ranges/dhcp_range (Codex review round 2, finding
    # 5) — an earlier revision let this banner recommend an address
    # inside the exact region the map above hatches "unavailable by
    # convention," which is the page contradicting itself. This is a
    # display-only widening of what *this backstop banner* avoids;
    # suggest_rack_vlan_range's real call site for an actual blank
    # RackVlanRange — RackVlanRange.clean() — is untouched and still
    # allocates against only the stored DHCP range and sibling ranges,
    # exactly as ADR 0002/0019 specify. If this VLAN's real DHCP
    # configuration doesn't occupy its bottom /24, the admin's own
    # suggester may legitimately offer something this banner excludes —
    # accepted as the cost of the banner staying honest about the
    # convention this same page has already asserted by hatching it.
    conventional_dhcp_block = _conventional_dhcp_block(network)
    next_block = suggest_rack_vlan_range(vlan.subnet, 1, [*used_ranges, conventional_dhcp_block], dhcp_range)

    context = {
        "vlan": vlan,
        "network": network,
        "segments": segments,
        "bottom_24_width_pct": bottom_24_width_pct,
        "next_block": next_block,
        "addresses": _vlan_addresses_in_use(vlan),
    }
    return render(request, "inventory/vlan_map.html", context)


# ---------------------------------------------------------------------------
# Device detail
# ---------------------------------------------------------------------------


@login_required
@require_GET
@permission_required(
    [
        "inventory.view_networkdevice",
        "inventory.view_vlan",
        # This page renders the device's type name, its rack, every port
        # row, and the connected switch — an earlier revision declared
        # only the two codenames above despite reading all four of these
        # (Codex review round 2, finding 2).
        "inventory.view_networkdevicetype",
        "inventory.view_rack",
        "inventory.view_networkdeviceport",
        "inventory.view_networkswitch",
    ],
    raise_exception=True,
)
def device_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Type, rack position (linked to the elevation), and the full port
    table — description, VLAN, port type, address or DHCP, a ``derived``
    tag where ``slot_offset > 0`` (ADR 0017), ``default_gateway`` (read
    live off the port's VLAN, ``models.py:4604`` — never recomputed here),
    and the connected switch via ``NetworkDevicePort.switch``
    (``models.py:4599``). The companion tether (ADR 0018) renders if
    either half of the pair is set.
    """
    device = get_object_or_404(
        NetworkDevice.objects.select_related("device_type", "rack", "host").prefetch_related(
            Prefetch(
                "ports",
                queryset=NetworkDevicePort.objects.select_related("vlan", "switch_port__switch").order_by(
                    "ordinal"
                ),
            ),
            "companion",
        ),
        pk=pk,
    )
    context = {
        "device": device,
        "ports": list(device.ports.all()),
        "tether": _tether_for(device),
        "admin_change_url": reverse("admin:inventory_networkdevice_change", args=[device.pk]),
        "rack_url": reverse("inventory:rack", args=[device.rack_id]) if device.rack_id else None,
    }
    return render(request, "inventory/device_detail.html", context)


# ---------------------------------------------------------------------------
# Spare pool
# ---------------------------------------------------------------------------


@login_required
@require_GET
@permission_required(
    [
        "inventory.view_networkswitch",
        "inventory.view_networkdevice",
        # The type column on both tables is NetworkSwitchType/
        # NetworkDeviceType, not a field on the switch/device rows
        # themselves (Codex review round 2, finding 2).
        "inventory.view_networkswitchtype",
        "inventory.view_networkdevicetype",
    ],
    raise_exception=True,
)
def spare_pool(request: HttpRequest) -> HttpResponse:
    """Unracked equipment (``rack__isnull=True``) — CONTEXT.md's Spare Pool
    entry is the framing: factory-DHCP equipment tracked by little more
    than serial number and hostname until it's racked.
    """
    context = {
        "spare_switches": NetworkSwitch.objects.filter(rack__isnull=True)
        .select_related("switch_type")
        .order_by("hostname"),
        "spare_devices": NetworkDevice.objects.filter(rack__isnull=True)
        .select_related("device_type")
        .order_by("hostname"),
    }
    return render(request, "inventory/spare_pool.html", context)


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


@login_required
@require_GET
@permission_required(
    [
        "inventory.view_rack",
        "inventory.view_vlan",
        "inventory.view_networkswitch",
        "inventory.view_networkdevice",
    ],
    raise_exception=True,
)
def index(request: HttpRequest) -> HttpResponse:
    """Racks with occupancy counts, VLANs with utilisation, spare-pool
    counts — every tile links into one of the four shaped views, except a
    subnet-less VLAN (L2-only, ADR 0012), which is listed with no map link
    since its map route has nothing to show but the L2-only state anyway.
    """
    racks = Rack.objects.annotate(
        switch_count=Count("switches", distinct=True),
        device_count=Count("devices", distinct=True),
    ).order_by("name")
    vlans = VLAN.objects.annotate(
        switch_address_count=Count("switch_addresses", distinct=True),
        device_address_count=Count(
            "device_ports", filter=Q(device_ports__address__isnull=False), distinct=True
        ),
    ).order_by("vlan_id")
    context = {
        "racks": racks,
        "vlans": vlans,
        "spare_switch_count": NetworkSwitch.objects.filter(rack__isnull=True).count(),
        "spare_device_count": NetworkDevice.objects.filter(rack__isnull=True).count(),
    }
    return render(request, "inventory/index.html", context)
