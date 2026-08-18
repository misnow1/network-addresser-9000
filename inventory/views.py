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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Literal
from urllib.parse import urlencode

from auditlog.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Max, Model, Prefetch, Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from .models import (
    VLAN,
    Department,
    NetworkDevice,
    NetworkDevicePort,
    NetworkDeviceType,
    NetworkDeviceTypePort,
    NetworkSwitch,
    NetworkSwitchAddress,
    NetworkSwitchPort,
    NetworkSwitchType,
    Owner,
    Rack,
    RackTemplate,
    RackVlanRange,
    SwitchPortVlanProfile,
    switch_port_profile_summary,
)
from .suggestions import suggest_rack_vlan_range, suggest_slot_address
from .validators import validate_ipv4_cidr

User = get_user_model()


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
    revision missed (Codex review round 2) — a range that's syntactically
    valid CIDR but *undersized* for the rack it's attached to (also only
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

    Containment alone still isn't the whole rule, though (Codex review
    round 3, finding 3): ``required_block_size``'s own docstring is
    explicit that "the block's own network address (index 0) and its top
    address (index size-1) are both left unassigned" — reserved, not
    available to any slot. An in-range-but-reserved ordinal (``ordinal=3``
    on that same undersized ``/30``, whose top index *is* 3) would pass a
    bare containment check while still being an address no slot is
    actually entitled to. Rather than re-deriving that same reservation
    arithmetic a second way and risking it drifting from
    ``required_block_size``'s, the candidate is checked directly against
    the two addresses the docstring names: the network's own base address
    and its ``broadcast_address`` (which, for any ``IPv4Network``, *is*
    the top index).

    A read-only page must never 500 on data the write path allowed
    (review note 3 of ``PLAN-read-only-ui.md``), and must not assert a
    false one either; every failure mode above becomes ``None`` here,
    which every caller renders as a blank cell.
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
    candidate_address = ipaddress.IPv4Address(candidate)
    if candidate_address not in network:
        return None
    if candidate_address in (network.network_address, network.broadcast_address):
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

    ``taken_by`` is an orthogonal axis, not a sixth state (issue #60,
    PLAN-consumed-slot-addresses.md decision 1): the slot really is empty
    — no occupant claims it — it's the *address* ``would_be_address``
    names that is already held by something else in the rack, static and
    on this same VLAN. Populated only when ``state == "empty"``; sorted
    for deterministic rendering, and a list rather than a single label
    because device-port and switch-address uniqueness are separate
    constraints and a bypassed write can leave both a switch and a device
    holding one address (decision 2).
    """

    state: str
    addresses: list[SlotAddress] = field(default_factory=list)
    would_be_address: str | None = None
    taken_by: list[str] = field(default_factory=list)


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
    #: #54 — read straight off the model property (no query of its own,
    #: see NetworkSwitch/NetworkDevice.hostname_diverges); carried onto
    #: Occupant rather than read from the template so the marker needs no
    #: extra select_related beyond what this view already declares.
    hostname_diverges: bool = False

    @property
    def bracketed(self) -> bool:
        return self.span > 1


@dataclass(frozen=True)
class ConflictOccupant:
    """One claimant in an ``ElevationRow.conflicts`` list — a minimal
    label + admin link, not a full ``Occupant``: with the ordinal
    genuinely disputed, there's no single span/cell state that
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

    @property
    def has_taken_address(self) -> bool:
        """Whether any of this row's cells carry a ``taken_by`` marker
        (issue #60) — derived from ``self.cells`` rather than stored, so
        there's no second walk of the grid to keep in sync with the cells
        that were actually built.
        """
        return any(cell.taken_by for cell in self.cells)


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


def _build_taken_address_map(
    switches: Iterable[NetworkSwitch], devices: Iterable[NetworkDevice]
) -> dict[tuple[int, str], list[str]]:
    """``{(vlan_id, address): [holder labels]}`` for every static address
    already held by this rack's own switches and devices (issue #60,
    PLAN-consumed-slot-addresses.md decisions 3/4/6) — so an empty
    ordinal's ``would_be_address`` can be checked against what's actually
    taken, not just what's stored at that ordinal's own offset.

    Built entirely from ``switches``/``devices`` as already prefetched by
    ``rack_detail()`` (``switches__addresses__vlan``,
    ``devices__ports__vlan``) — this issues no query of its own, and
    ``port.source_type_port`` is deliberately never touched: that relation
    is prefetched in ``device_detail()``, a different view, and reading it
    here would be an N+1.

    Every static address counts, not just ``OPERATOR``-sourced ones
    (decision 3): an ordinary ``SLOT`` port's address stays editable after
    creation (ADR 0003), so a hand-moved ordinary address consumes an
    ordinal's would-be address exactly the same way an operator-set one
    does. A DHCP port's ``address`` is ``None`` and is naturally excluded.

    Labels come from the enclosing device/switch — ``str(switch)``/
    ``str(device)``, the same values already used for ``Occupant.label``
    in ``_switch_row``/``_device_row`` — already-loaded scalars, not a
    fresh lookup. Scope is this rack's own occupants only (decision 6): an
    address held by equipment in another rack is not consulted.
    """
    taken: dict[tuple[int, str], list[str]] = defaultdict(list)
    for switch in switches:
        label = str(switch)
        for address in switch.addresses.all():
            if address.address is not None:
                taken[(address.vlan_id, address.address)].append(label)
    for device in devices:
        label = str(device)
        for port in device.ports.all():
            if port.address is not None:
                taken[(port.vlan_id, port.address)].append(label)
    return dict(taken)


def _empty_cell(
    column: RackVlanRange, ordinal: int, taken: dict[tuple[int, str], list[str]]
) -> ElevationCell:
    would_be_address = safe_slot_address(column.address_range, ordinal)
    taken_by: list[str] = []
    if would_be_address is not None:
        taken_by = sorted(taken.get((column.vlan_id, would_be_address), []))
    return ElevationCell(state="empty", would_be_address=would_be_address, taken_by=taken_by)


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
        hostname_diverges=switch.hostname_diverges,
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
        hostname_diverges=device.hostname_diverges,
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
    taken = _build_taken_address_map(switches, devices)
    rows = []
    for ordinal in range(1, rack.slot_count + 1):
        entries = occupancy.get(ordinal, [])
        if not entries:
            cells = [_empty_cell(column, ordinal, taken) for column in columns]
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
        # ...and NetworkDeviceTypePort, read directly by
        # resolve_slot_spans() (its slot_offset column drives both the
        # bracket span and every cell's occupancy) rather than merely
        # implied by view_networkdevice — an earlier revision missed this
        # one too (Codex review round 3, finding 2).
        "inventory.view_networkdevicetypeport",
        # ADR 0023 — this page now renders the rack's owner/location_slug.
        # A previous Codex review caught this view under-declaring its
        # codenames; the rule stands — declare the full set actually read.
        "inventory.view_owner",
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

    Three encodings, each guarding a specific documented failure — see
    ``ElevationCell``/``Occupant`` for the detail of each:
    a solid bracket for an ADR 0017 span, an em-dash for "no port on
    this VLAN" (absence, not missing data), and a greyed would-be address
    on a genuinely empty ordinal.

    Query budget: every relation this view needs is prefetched once,
    up front, and ``resolve_slot_spans`` resolves every device's span in
    one grouped query rather than touching ``NetworkDeviceType.slot_span``
    per device — see that function's docstring. The result is a query
    count independent of how many switches/devices the rack holds, which
    ``test_ui.py`` locks in by asserting equal counts for a 2-device and a
    50-device rack.

    This is already the canonical ``Rack`` page (Stage B decision 13): the
    generic parity route for ``Rack`` redirects here rather than rendering
    a second field list — ``RackAdmin``'s own ``list_display``
    (``name``/``slot_count``) plus its VLAN-range inline are both already
    on screen, so nothing needed adding except the audit-history panel
    (Stage B), which renders at the bottom when the viewer holds
    ``auditlog.view_logentry``.
    """
    vlan_range_qs = RackVlanRange.objects.select_related("vlan").order_by("vlan__vlan_id")
    # owner (and rack, below) join switch_type/device_type here for
    # hostname_diverges (#54, ADR 0023 phase 18 PR 4) — the registry's own
    # select_related hints never reach this shaped view, so its query
    # budget has to declare them itself or the marker is an N+1 across the
    # rack's occupants.
    switch_qs = NetworkSwitch.objects.select_related("switch_type", "owner", "rack").prefetch_related(
        Prefetch("addresses", queryset=NetworkSwitchAddress.objects.select_related("vlan"))
    )
    device_qs = NetworkDevice.objects.select_related("device_type", "owner", "rack").prefetch_related(
        Prefetch("ports", queryset=NetworkDevicePort.objects.select_related("vlan")),
    )
    rack = get_object_or_404(
        Rack.objects.select_related("owner").prefetch_related(
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
    audit_entries, audit_content_type_pk = _object_audit_panel_context(rack, request.user)
    context = {
        "rack": rack,
        "columns": columns,
        "rows": rows,
        "audit_entries": audit_entries,
        "audit_content_type_pk": audit_content_type_pk,
    }
    return render(request, "inventory/rack_detail.html", context)


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


@dataclass(frozen=True)
class DhcpMapSegment:
    """The hatched region of the address map — this VLAN's own *stored*
    ``dhcp_range_start``/``dhcp_range_end``, not an assumed convention.

    ADR 0002's "bottom /24 is DHCP" is a sizing-time suggestion that ADR
    0011 explicitly retired: ``dhcp_range`` stopped being a CIDR block,
    stopped being auto-suggested, and "no replacement convention was
    wanted" (ADR 0011's own words). Hatching the bottom /24 regardless of
    what's actually stored — an earlier revision's behaviour — asserts a
    convention the domain deliberately stopped enforcing three ADRs ago
    (Codex review round 3, finding 4). ``None`` (no segment at all) for a
    VLAN with no stored DHCP range, which is a legitimate, common state.
    """

    start: str
    end: str
    left_pct: float
    width_pct: float


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
        # The header now names this VLAN's department (ADR 0021) —
        # _linked_text_for only links it with this codename held, but the
        # name itself renders regardless, so this is required for the same
        # reason as the other entries above: it's data the page shows.
        "inventory.view_department",
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

    The hatched region is this VLAN's own stored ``dhcp_range_start``/
    ``dhcp_range_end`` — nothing at all if neither is set — not an
    assumed bottom /24 (Codex review round 3, finding 4; see
    ``DhcpMapSegment``). The next-free-block banner reaches
    ``suggest_rack_vlan_range`` with exactly the same inputs
    ``RackVlanRange.clean()`` itself would use for a blank range on this
    VLAN — sibling ranges and the stored DHCP range, nothing synthesized
    — so this banner and the admin's own allocator can never disagree.
    An earlier revision widened the banner's exclusions to agree with an
    assumed-convention hatch instead; that made the *banner* wrong
    (recommending a block the real allocator wouldn't give you) to paper
    over a hatch that was asserting a convention ADR 0011 already retired
    the auto-suggestion for. Fixing the hatch made that widening
    unnecessary, so it's reverted here too.
    """
    vlan = get_object_or_404(VLAN.objects.select_related("department"), pk=pk)
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

    dhcp_range = None
    dhcp_segment = None
    if vlan.dhcp_range_start and vlan.dhcp_range_end:
        try:
            start_address = ipaddress.IPv4Address(vlan.dhcp_range_start)
            end_address = ipaddress.IPv4Address(vlan.dhcp_range_end)
        except ValueError:
            pass  # VLAN's own malformed range; its own admin page reports that
        else:
            # Normalized the same way dhcp_range_overlaps_cidr() is —
            # VLAN.clean() enforces start < end on an ordinary write, but
            # that's not a DB-level guarantee, so a clean()-bypassing
            # write could leave the pair reversed.
            if start_address > end_address:
                start_address, end_address = end_address, start_address
            dhcp_range = (str(start_address), str(end_address))
            if start_address in network and end_address in network:
                offset = int(start_address) - int(network.network_address)
                width = int(end_address) - int(start_address) + 1
                dhcp_segment = DhcpMapSegment(
                    start=str(start_address),
                    end=str(end_address),
                    left_pct=offset / network.num_addresses * 100,
                    width_pct=width / network.num_addresses * 100,
                )
            # else: stored range no longer fits this VLAN's subnet (an
            # edited subnet, reached via a clean()-bypassing write) —
            # nothing sensible to place on the map; VLAN.clean() would
            # ordinarily catch this on save, so it's not re-reported here.

    # slot_count=1 forces ADR 0015's /27 floor (required_block_size(1) ==
    # 32) as the reference size — ADR 0019 made offset space reservable
    # with an empty Rack, so this suggestion is a backstop against nobody
    # having reserved a gap, not the only guard against landing in one.
    # Exactly the same inputs RackVlanRange.clean() itself would use for
    # a blank range on this VLAN (sibling ranges, stored DHCP range) —
    # nothing synthesized — so this banner can never recommend something
    # the real allocator wouldn't actually give you.
    next_block = suggest_rack_vlan_range(vlan.subnet, 1, used_ranges, dhcp_range)

    context = {
        "vlan": vlan,
        "network": network,
        "segments": segments,
        "dhcp_segment": dhcp_segment,
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
        # Stage B's port table renders the specific connected switch
        # *port* (``port.switch_port``), not just the switch it belongs
        # to — the same class of gap the comment above already names once:
        # a field this page reads that its own codename list didn't yet
        # declare (Codex review, Stage B pass).
        "inventory.view_networkswitchport",
        # ADR 0022 — a port's derived hostname reads its
        # ``source_type_port``, which is a Network Device Type Port.
        "inventory.view_networkdevicetypeport",
        # ADR 0023 — this page now renders the device's owner.
        "inventory.view_owner",
    ],
    raise_exception=True,
)
def device_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Type, rack position (linked to the elevation), and the full port
    table — port number, description, VLAN, port type, the numeric
    ``slot_offset`` (Stage B — the admin's inline shows *by how much* an
    address is derived, ADR 0017, not just *that* it is), address or DHCP,
    a ``derived`` tag where ``slot_offset > 0``, its derived hostname where
    it has one (ADR 0022), ``default_gateway`` (read live off the port's
    VLAN, ``models.py:4640`` — never recomputed here), and the connected
    switch **port** (Stage B — not just the switch, per
    ``NetworkDevicePort.switch_port``) via ``switch_port__switch``. Also a
    "Fitted to …" line when this device is itself a card (``host``) and a
    "Cards fitted" list when other devices are fitted to this one
    (``installed_cards``) — ADR 0022 PR 3.

    This is the *canonical* device page (Stage B decision 13): the generic
    parity route for ``NetworkDevice`` redirects here rather than rendering
    a second, competing field list, so any field the admin shows that this
    page doesn't render yet is a real gap, not a duplicate source of truth.
    An audit-history panel (Stage B) renders at the bottom when the viewer
    holds ``auditlog.view_logentry``.

    ``canonical_detail_view`` (``REGISTRY["networkdevice"]``) redirects the
    generic parity route here, so the registry's own ``select_related``/
    ``prefetch_related`` hints never apply to this page — ``host`` and
    ``installed_cards`` have to be declared in *this* queryset instead, or
    "Fitted to" costs a query and "Cards fitted" is an N+1 (ADR 0022 PR 3
    review note 8).
    """
    device = get_object_or_404(
        NetworkDevice.objects.select_related("device_type", "rack", "host", "owner").prefetch_related(
            Prefetch(
                "ports",
                # source_type_port (ADR 0022) — port.hostname reads it on
                # every row; without it here, rendering the port table is
                # an N+1 across every port.
                queryset=NetworkDevicePort.objects.select_related(
                    "vlan", "switch_port__switch", "source_type_port"
                ).order_by("ordinal"),
            ),
            Prefetch(
                "installed_cards",
                queryset=NetworkDevice.objects.select_related("device_type", "rack").order_by("hostname"),
            ),
        ),
        pk=pk,
    )
    audit_entries, audit_content_type_pk = _object_audit_panel_context(device, request.user)
    context = {
        "device": device,
        "ports": list(device.ports.all()),
        "installed_cards": list(device.installed_cards.all()),
        "admin_change_url": reverse("admin:inventory_networkdevice_change", args=[device.pk]),
        "rack_url": reverse("inventory:rack", args=[device.rack_id]) if device.rack_id else None,
        "audit_entries": audit_entries,
        "audit_content_type_pk": audit_content_type_pk,
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
        # owner joins switch_type/device_type here for hostname_diverges
        # (#54, ADR 0023 phase 18 PR 4) — explicitly decided to carry the
        # marker on this page too: spare-pool equipment has no rack, but
        # it can still have an owner and a Type hostname_slug, so
        # divergence is still a meaningful question here (rack is never
        # read for these rows — a null FK short-circuits with no query).
        "spare_switches": NetworkSwitch.objects.filter(rack__isnull=True)
        .select_related("switch_type", "owner")
        .order_by("hostname"),
        "spare_devices": NetworkDevice.objects.filter(rack__isnull=True)
        .select_related("device_type", "owner")
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

    Also the discovery surface for the Stage B parity pages (review note
    8): an "All records" panel lists every ``/models/<slug>/`` list, each
    shown only if the viewer holds that entry's own ``list_permissions``
    — a partial-privilege user simply sees fewer tiles, the same
    floor-not-narrowing posture every other view here takes. This keeps
    the nav bar itself short (one more static link, "Audit", is all it
    gains) rather than growing it with every registry entry.
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
    all_records = [spec for spec in REGISTRY.values() if request.user.has_perms(spec.list_permissions)]
    context = {
        "racks": racks,
        "vlans": vlans,
        "spare_switch_count": NetworkSwitch.objects.filter(rack__isnull=True).count(),
        "spare_device_count": NetworkDevice.objects.filter(rack__isnull=True).count(),
        "all_records": all_records,
    }
    return render(request, "inventory/index.html", context)


# ---------------------------------------------------------------------------
# Read-parity — the registry (phase 15, ADR 0020, Stage B)
# ---------------------------------------------------------------------------
#
# ADR 0020 decision 5: a Viewer who cannot reach the admin and cannot see,
# say, a switch port's VLAN profile in this UI has lost access to data
# CONTEXT.md promises them ("Can see all data"). Parity is measured against
# the admin's own changelists/forms, not against a bare `_meta.fields` walk
# — the admin shows several values that are not fields at all (computed
# columns like `profile_summary`/`default_gateway`) and two M2M
# memberships as form widgets rather than inlines
# (`SwitchPortVlanProfile.allowed_vlans`, `RackTemplate.vlans`), both of
# which a naive walk would silently drop. The parity inventory in
# `PLAN-read-only-ui.md`'s Stage B section is the checklist this registry
# was built against, entry by entry.
#
# Two generic views (`model_list`/`model_detail`) driven by this registry,
# not eighteen hand-written ones (decision 8) — this is the whole contract
# for what a model's page shows; nothing about it is decided in a
# template. Templates only know the five `render` kinds below, never a
# model name.


@dataclass(frozen=True)
class FieldSpec:
    """One column (on a list page) or one field row (on a detail page).

    ``accessor`` is either a dotted attribute path resolved against the
    row object (``"native_vlan.vlan_id"`` — the first ``None`` hop short-
    circuits to ``None``, the same defensive posture as ``safe_slot_
    address``: a read-only page must never 500 on data the write path
    allowed), or a plain function of the object for a value that isn't an
    attribute at all (``switch_port_profile_summary`` — a computed column,
    not a field). A callable accessor must not touch the database beyond
    what the owning ``ModelSpec``'s ``select_related``/``prefetch_related``
    already declares — it runs once per row, so anything it queries itself
    is an N+1.
    """

    label: str
    accessor: str | Callable[[Any], Any]
    render: Literal["text", "choice", "boolean", "relation", "m2m"] = "text"


@dataclass(frozen=True)
class InlineSpec:
    """One admin inline, reproduced as a sub-table on the parity detail
    page — e.g. ``NetworkSwitchPortInline`` becomes the "Ports" panel on
    ``/models/networkswitch/<pk>/``.
    """

    label: str  # panel heading, e.g. "Ports"
    accessor: str  # reverse relation name on the parent, e.g. "ports"
    columns: tuple[FieldSpec, ...]
    ordering: tuple[str, ...]
    permissions: tuple[str, ...]  # the inline model's own view_ codename(s)


@dataclass(frozen=True)
class ModelSpec:
    """One registered model's whole read-only-UI page contract."""

    slug: str
    model: type[Model]
    label: str
    label_plural: str
    list_columns: tuple[FieldSpec, ...]
    detail_fields: tuple[FieldSpec, ...]
    inlines: tuple[InlineSpec, ...] = ()
    #: The name of a Stage A shaped view (e.g. ``"inventory:rack"``) that
    #: is canonical for this model — set only for Rack and NetworkDevice
    #: (decision 13). When set, `model_detail` redirects there rather than
    #: rendering `detail_fields`/`inlines`, so there is never a second page
    #: claiming to be the record of the same object.
    canonical_detail_view: str | None = None
    ordering: tuple[str, ...] = ()
    list_select_related: tuple[str, ...] = ()
    list_prefetch_related: tuple[str, ...] = ()
    detail_select_related: tuple[str, ...] = ()
    detail_prefetch_related: tuple[str, ...] = ()
    list_permissions: tuple[str, ...] = ()
    detail_permissions: tuple[str, ...] = ()


def _switch_port_profile_summary_accessor(port: NetworkSwitchPort) -> str:
    # A tiny wrapper, not switch_port_profile_summary itself, so the
    # FieldSpec's callable-accessor type (Callable[[Any], Any]) doesn't
    # have to widen to accept the model's own more specific signature.
    return switch_port_profile_summary(port)


REGISTRY: dict[str, ModelSpec] = {
    "vlan": ModelSpec(
        slug="vlan",
        model=VLAN,
        label="VLAN",
        label_plural="VLANs",
        list_columns=(
            FieldSpec("Name", "name"),
            FieldSpec("VLAN ID", "vlan_id"),
            FieldSpec("Subnet", "subnet"),
            FieldSpec("Default gateway", "default_gateway"),
            FieldSpec("DHCP start", "dhcp_range_start"),
            FieldSpec("DHCP end", "dhcp_range_end"),
            FieldSpec("Department", "department", render="relation"),
        ),
        detail_fields=(
            FieldSpec("Name", "name"),
            FieldSpec("VLAN ID", "vlan_id"),
            FieldSpec("Subnet", "subnet"),
            FieldSpec("Default gateway", "default_gateway"),
            FieldSpec("DHCP start", "dhcp_range_start"),
            FieldSpec("DHCP end", "dhcp_range_end"),
            FieldSpec("Department", "department", render="relation"),
        ),
        ordering=("vlan_id",),
        list_select_related=("department",),
        detail_select_related=("department",),
        list_permissions=("inventory.view_vlan", "inventory.view_department"),
        detail_permissions=("inventory.view_vlan", "inventory.view_department"),
    ),
    "department": ModelSpec(
        slug="department",
        model=Department,
        label="Department",
        label_plural="Departments",
        list_columns=(FieldSpec("Name", "name"), FieldSpec("Description", "description")),
        detail_fields=(FieldSpec("Name", "name"), FieldSpec("Description", "description")),
        inlines=(
            InlineSpec(
                label="VLANs",
                accessor="vlans",
                columns=(
                    FieldSpec("Name", "name"),
                    FieldSpec("VLAN ID", "vlan_id"),
                    FieldSpec("Subnet", "subnet"),
                ),
                ordering=("vlan_id",),
                # This registry's first inline with no admin counterpart —
                # deliberate, not drift (ADR 0021 decision 6). VLANAdmin
                # gets a `department` list_filter instead of an inline; the
                # read-only UI has no filtering at all, so this inline is
                # that filter's equivalent. The parity being kept is of
                # *capability* (can a viewer find a department's VLANs?),
                # not of markup.
                permissions=("inventory.view_vlan",),
            ),
        ),
        ordering=("name",),
        detail_prefetch_related=("vlans",),
        list_permissions=("inventory.view_department",),
        detail_permissions=("inventory.view_department", "inventory.view_vlan"),
    ),
    "owner": ModelSpec(
        slug="owner",
        model=Owner,
        label="Owner",
        label_plural="Owners",
        list_columns=(FieldSpec("Name", "name"), FieldSpec("Slug", "slug")),
        detail_fields=(FieldSpec("Name", "name"), FieldSpec("Slug", "slug")),
        inlines=(
            InlineSpec(
                label="Racks",
                accessor="racks",
                columns=(
                    FieldSpec("Name", "name"),
                    FieldSpec("Slot count", "slot_count"),
                ),
                ordering=("name",),
                permissions=("inventory.view_rack",),
            ),
            InlineSpec(
                label="Network Switches",
                accessor="switches",
                columns=(
                    FieldSpec("Hostname", "hostname"),
                    FieldSpec("Type", "switch_type", render="relation"),
                ),
                ordering=("hostname",),
                permissions=("inventory.view_networkswitch",),
            ),
            InlineSpec(
                label="Network Devices",
                accessor="devices",
                columns=(
                    FieldSpec("Hostname", "hostname"),
                    FieldSpec("Type", "device_type", render="relation"),
                ),
                ordering=("hostname",),
                permissions=("inventory.view_networkdevice",),
            ),
        ),
        ordering=("name",),
        detail_prefetch_related=("racks", "switches__switch_type", "devices__device_type"),
        list_permissions=("inventory.view_owner",),
        # InlineSpec.permissions is declared but read nowhere — _render_inline()
        # does no permission check and model_detail() renders every inline
        # unconditionally (review note 4). Enforcement in this registry is
        # registry_permission_required against the spec's own detail_
        # permissions, so every inline's codename folds in here, exactly as
        # "department" does for its own single inline above.
        detail_permissions=(
            "inventory.view_owner",
            "inventory.view_rack",
            "inventory.view_networkswitch",
            "inventory.view_networkdevice",
        ),
    ),
    "switchportvlanprofile": ModelSpec(
        slug="switchportvlanprofile",
        model=SwitchPortVlanProfile,
        label="Switch Port VLAN Profile",
        label_plural="Switch Port VLAN Profiles",
        list_columns=(
            FieldSpec("Name", "name"),
            FieldSpec("Port mode", "port_mode", render="choice"),
            FieldSpec("Native VLAN", "native_vlan", render="relation"),
            FieldSpec("All VLANs allowed", "all_vlans_allowed", render="boolean"),
            FieldSpec("Allowed VLANs", "allowed_vlans", render="m2m"),
            FieldSpec("System profile", "is_system", render="boolean"),
        ),
        detail_fields=(
            FieldSpec("Name", "name"),
            FieldSpec("Port mode", "port_mode", render="choice"),
            FieldSpec("Native VLAN", "native_vlan", render="relation"),
            FieldSpec("All VLANs allowed", "all_vlans_allowed", render="boolean"),
            FieldSpec("Allowed VLANs", "allowed_vlans", render="m2m"),
            FieldSpec("System profile", "is_system", render="boolean"),
        ),
        ordering=("name",),
        list_select_related=("native_vlan",),
        list_prefetch_related=("allowed_vlans",),
        detail_select_related=("native_vlan",),
        detail_prefetch_related=("allowed_vlans",),
        list_permissions=("inventory.view_switchportvlanprofile", "inventory.view_vlan"),
        detail_permissions=("inventory.view_switchportvlanprofile", "inventory.view_vlan"),
    ),
    "racktemplate": ModelSpec(
        slug="racktemplate",
        model=RackTemplate,
        label="Rack Template",
        label_plural="Rack Templates",
        list_columns=(
            FieldSpec("Name", "name"),
            FieldSpec("Slot count", "slot_count"),
            FieldSpec("VLANs", "vlans", render="m2m"),
        ),
        detail_fields=(
            FieldSpec("Name", "name"),
            FieldSpec("Slot count", "slot_count"),
            FieldSpec("VLANs", "vlans", render="m2m"),
        ),
        ordering=("name",),
        list_prefetch_related=("vlans",),
        detail_prefetch_related=("vlans",),
        list_permissions=("inventory.view_racktemplate", "inventory.view_vlan"),
        detail_permissions=("inventory.view_racktemplate", "inventory.view_vlan"),
    ),
    "rack": ModelSpec(
        slug="rack",
        model=Rack,
        label="Rack",
        label_plural="Racks",
        list_columns=(
            FieldSpec("Name", "name"),
            FieldSpec("Slot count", "slot_count"),
            FieldSpec("Owner", "owner", render="relation"),
            FieldSpec("Location", "location_slug"),
        ),
        # Never actually rendered — canonical_detail_view redirects before
        # detail_fields/inlines are consulted (decision 13) — declared
        # anyway so this entry documents everything RackAdmin shows, per
        # the parity inventory.
        detail_fields=(
            FieldSpec("Name", "name"),
            FieldSpec("Slot count", "slot_count"),
            FieldSpec("Owner", "owner", render="relation"),
            FieldSpec("Location", "location_slug"),
        ),
        inlines=(
            InlineSpec(
                label="VLAN ranges",
                accessor="vlan_ranges",
                columns=(
                    FieldSpec("VLAN", "vlan", render="relation"),
                    FieldSpec("Address range", "address_range"),
                ),
                ordering=("vlan__vlan_id",),
                permissions=("inventory.view_rackvlanrange",),
            ),
        ),
        canonical_detail_view="inventory:rack",
        ordering=("name",),
        list_select_related=("owner",),
        # The codename for the new relation column goes in list_permissions
        # — the established pattern (networkdevice.list_permissions already
        # carries view_networkdevicetype/view_rack for its relation columns).
        list_permissions=("inventory.view_rack", "inventory.view_owner"),
        # Minimal on purpose: model_detail redirects to rack_detail before
        # rendering anything, and rack_detail declares its own — larger —
        # codename set. Requiring that larger set here too would 403 the
        # redirect itself for a user rack_detail would otherwise happily
        # serve, and would report the wrong codename in a 403.
        detail_permissions=("inventory.view_rack",),
    ),
    "networkswitchtype": ModelSpec(
        slug="networkswitchtype",
        model=NetworkSwitchType,
        label="Network Switch Type",
        label_plural="Network Switch Types",
        list_columns=(
            FieldSpec("Manufacturer", "manufacturer"),
            FieldSpec("Model", "model"),
            FieldSpec("Name", "name"),
            FieldSpec("Port count", "port_count"),
            FieldSpec("Hostname slug", "hostname_slug"),
        ),
        detail_fields=(
            FieldSpec("Manufacturer", "manufacturer"),
            FieldSpec("Model", "model"),
            FieldSpec("Name", "name"),
            FieldSpec("Port count", "port_count"),
            FieldSpec("Hostname slug", "hostname_slug"),
        ),
        inlines=(
            InlineSpec(
                label="Type ports",
                accessor="type_ports",
                columns=(
                    FieldSpec("Port number", "port_number"),
                    FieldSpec("Description", "description"),
                    FieldSpec("Port type", "port_type", render="choice"),
                    FieldSpec("Profile", "profile", render="relation"),
                ),
                ordering=("port_number",),
                permissions=("inventory.view_networkswitchtypeport",),
            ),
        ),
        ordering=("manufacturer", "model", "name"),
        list_permissions=("inventory.view_networkswitchtype",),
        detail_permissions=(
            "inventory.view_networkswitchtype",
            "inventory.view_networkswitchtypeport",
            "inventory.view_switchportvlanprofile",
        ),
        detail_prefetch_related=("type_ports__profile",),
    ),
    "networkswitch": ModelSpec(
        slug="networkswitch",
        model=NetworkSwitch,
        label="Network Switch",
        label_plural="Network Switches",
        list_columns=(
            FieldSpec("Hostname", "hostname"),
            FieldSpec("Type", "switch_type", render="relation"),
            FieldSpec("Serial number", "serial_number"),
            FieldSpec("Rack", "rack", render="relation"),
            FieldSpec("Rack slot", "rack_slot"),
            FieldSpec("DHCP server", "dhcp_server_enabled", render="boolean"),
            FieldSpec("Owner", "owner", render="relation"),
            FieldSpec("Hostname purpose", "hostname_purpose"),
            FieldSpec("Hostname sequence", "hostname_sequence"),
            # #54 — NetworkSwitch has no shaped view of its own (unlike
            # Rack/NetworkDevice's canonical_detail_view), so this registry
            # entry is the only place the marker can reach it at all. Costs
            # no extra query: list_select_related already covers every
            # relation hostname_diverges reads.
            FieldSpec("Diverges", "hostname_diverges", render="boolean"),
        ),
        detail_fields=(
            FieldSpec("Hostname", "hostname"),
            FieldSpec("Type", "switch_type", render="relation"),
            FieldSpec("Serial number", "serial_number"),
            FieldSpec("Rack", "rack", render="relation"),
            FieldSpec("Rack slot", "rack_slot"),
            FieldSpec("DHCP server", "dhcp_server_enabled", render="boolean"),
            FieldSpec("Owner", "owner", render="relation"),
            FieldSpec("Hostname purpose", "hostname_purpose"),
            FieldSpec("Hostname sequence", "hostname_sequence"),
            FieldSpec("Diverges", "hostname_diverges", render="boolean"),
        ),
        # NetworkSwitch has no shaped page of its own yet — this is the
        # first genuinely generic detail page in the registry.
        inlines=(
            InlineSpec(
                label="Addresses",
                accessor="addresses",
                columns=(
                    FieldSpec("VLAN", "vlan", render="relation"),
                    FieldSpec("Address", "address"),
                ),
                ordering=("vlan__vlan_id",),
                permissions=("inventory.view_networkswitchaddress",),
            ),
            InlineSpec(
                label="Ports",
                accessor="ports",
                columns=(
                    FieldSpec("Port number", "port_number"),
                    FieldSpec("Port type", "port_type", render="choice"),
                    FieldSpec("Description", "description"),
                    FieldSpec("Profile", "profile", render="relation"),
                    # Computed, not a field — admin.py:766's
                    # profile_summary(), reused verbatim rather than
                    # reimplemented (see switch_port_profile_summary()).
                    FieldSpec("Profile config", _switch_port_profile_summary_accessor),
                ),
                ordering=("port_number",),
                permissions=("inventory.view_networkswitchport",),
            ),
        ),
        ordering=("hostname",),
        list_select_related=("switch_type", "rack", "owner"),
        detail_select_related=("switch_type", "rack", "owner"),
        # profile_summary() reads profile.native_vlan and
        # profile.allowed_vlans per port — without these, an N+1 across a
        # switch's ports (admin.py:752-765 carries the identical hint for
        # the same reason).
        detail_prefetch_related=(
            "addresses__vlan",
            "ports__profile__native_vlan",
            "ports__profile__allowed_vlans",
        ),
        list_permissions=(
            "inventory.view_networkswitch",
            "inventory.view_networkswitchtype",
            "inventory.view_rack",
            "inventory.view_owner",
        ),
        # NetworkSwitch has no shaped page — unlike rack/networkdevice's
        # "minimal on purpose" detail_permissions, this page really does
        # render through the registry, so view_owner belongs here too.
        detail_permissions=(
            "inventory.view_networkswitch",
            "inventory.view_networkswitchtype",
            "inventory.view_rack",
            "inventory.view_owner",
            "inventory.view_networkswitchaddress",
            "inventory.view_vlan",
            "inventory.view_networkswitchport",
            "inventory.view_switchportvlanprofile",
        ),
    ),
    "networkdevicetype": ModelSpec(
        slug="networkdevicetype",
        model=NetworkDeviceType,
        label="Network Device Type",
        label_plural="Network Device Types",
        list_columns=(
            FieldSpec("Manufacturer", "manufacturer"),
            FieldSpec("Model", "model"),
            FieldSpec("Name", "name"),
            FieldSpec("Port count", "port_count"),
            FieldSpec("Add-in card", "is_add_in_card", render="boolean"),
            FieldSpec("Hostname slug", "hostname_slug"),
        ),
        detail_fields=(
            FieldSpec("Manufacturer", "manufacturer"),
            FieldSpec("Model", "model"),
            FieldSpec("Name", "name"),
            FieldSpec("Port count", "port_count"),
            FieldSpec("Add-in card", "is_add_in_card", render="boolean"),
            FieldSpec("Hostname slug", "hostname_slug"),
        ),
        inlines=(
            InlineSpec(
                label="Type ports",
                accessor="type_ports",
                columns=(
                    FieldSpec("Port number", "port_number"),
                    FieldSpec("Description", "description"),
                    FieldSpec("Port type", "port_type", render="choice"),
                    FieldSpec("VLAN", "vlan", render="relation"),
                    FieldSpec("Slot offset", "slot_offset"),
                    FieldSpec("Address source", "address_source", render="choice"),
                    FieldSpec("Hostname suffix", "hostname_suffix"),
                ),
                ordering=("ordinal",),
                permissions=("inventory.view_networkdevicetypeport",),
            ),
        ),
        ordering=("manufacturer", "model", "name"),
        detail_prefetch_related=("type_ports__vlan",),
        list_permissions=("inventory.view_networkdevicetype",),
        detail_permissions=(
            "inventory.view_networkdevicetype",
            "inventory.view_networkdevicetypeport",
            "inventory.view_vlan",
        ),
    ),
    "networkdevice": ModelSpec(
        slug="networkdevice",
        model=NetworkDevice,
        label="Network Device",
        label_plural="Network Devices",
        list_columns=(
            FieldSpec("Hostname", "hostname"),
            FieldSpec("Type", "device_type", render="relation"),
            FieldSpec("Serial number", "serial_number"),
            FieldSpec("Rack", "rack", render="relation"),
            FieldSpec("Rack slot", "rack_slot"),
            FieldSpec("Host", "host", render="relation"),
            FieldSpec("Owner", "owner", render="relation"),
            # #54 — costs no extra query: list_select_related already
            # covers every relation hostname_diverges reads. NetworkDevice
            # has its own canonical page (device_detail.html) with its own
            # marker; this is what the /models/networkdevice/ *list* page
            # (not the detail redirect) shows instead.
            FieldSpec("Diverges", "hostname_diverges", render="boolean"),
        ),
        # Never actually rendered — see the Rack entry's identical note.
        detail_fields=(
            FieldSpec("Hostname", "hostname"),
            FieldSpec("Type", "device_type", render="relation"),
            FieldSpec("Serial number", "serial_number"),
            FieldSpec("Rack", "rack", render="relation"),
            FieldSpec("Rack slot", "rack_slot"),
            FieldSpec("Host", "host", render="relation"),
            FieldSpec("Owner", "owner", render="relation"),
            FieldSpec("Diverges", "hostname_diverges", render="boolean"),
        ),
        inlines=(
            InlineSpec(
                label="Ports",
                accessor="ports",
                columns=(
                    FieldSpec("Description", "description"),
                    FieldSpec("Port number", "port_number"),
                    FieldSpec("Port type", "port_type", render="choice"),
                    FieldSpec("VLAN", "vlan", render="relation"),
                    FieldSpec("Slot offset", "slot_offset"),
                    FieldSpec("DHCP", "is_dhcp", render="boolean"),
                    FieldSpec("Address", "address"),
                    FieldSpec("Default gateway", "default_gateway"),
                    FieldSpec("Switch port", "switch_port", render="relation"),
                ),
                ordering=("ordinal",),
                permissions=("inventory.view_networkdeviceport",),
            ),
        ),
        canonical_detail_view="inventory:device",
        ordering=("hostname",),
        list_select_related=("device_type", "rack", "host", "owner"),
        list_permissions=(
            "inventory.view_networkdevice",
            "inventory.view_networkdevicetype",
            "inventory.view_rack",
            "inventory.view_owner",
        ),
        # Minimal — see the Rack entry's identical note; device_detail
        # declares its own larger codename set once the redirect lands.
        detail_permissions=("inventory.view_networkdevice",),
    ),
}

_SPEC_BY_MODEL: dict[type[Model], ModelSpec] = {spec.model: spec for spec in REGISTRY.values()}


def _resolve_dotted(obj: Any, accessor: str) -> Any:
    """Walks a dotted attribute path against ``obj``, returning ``None`` the
    moment any hop is ``None`` — the same defensive posture as
    ``safe_slot_address``: a read-only page must never 500 on data the
    write path allowed.
    """
    value = obj
    for part in accessor.split("."):
        if value is None:
            return None
        value = getattr(value, part, None)
    return value


def _resolve_field_value(obj: Any, field_spec: FieldSpec) -> Any:
    if callable(field_spec.accessor):
        return field_spec.accessor(obj)
    return _resolve_dotted(obj, field_spec.accessor)


def _resolve_choice_display(obj: Any, accessor: str) -> Any:
    """``get_<field>_display()`` for a (possibly dotted) choice field —
    resolved against the accessor's *parent* object, since the display
    method lives on whichever model actually declares the field.
    """
    if "." in accessor:
        parent_path, field_name = accessor.rsplit(".", 1)
        parent = _resolve_dotted(obj, parent_path)
    else:
        parent = obj
        field_name = accessor
    if parent is None:
        return None
    display = getattr(parent, f"get_{field_name}_display", None)
    return display() if display is not None else None


def _text_or_dash(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


@dataclass(frozen=True)
class LinkedText:
    """One rendered value — plain text, or text with a link — the smallest
    unit every render kind below decomposes into. A ``relation``/``m2m``
    field is one or more of these; every other kind is exactly one.
    Templates render this uniformly (``<a>`` if ``url`` else plain text),
    which is what keeps "nothing about a model's page is decided in a
    template" true even for links.
    """

    text: str
    url: str | None = None


def _linked_text_for(value: Model, user: Any) -> LinkedText:
    """A single related object, rendered as ``relation`` would: linked to
    its own canonical detail URL if its model is in the registry *and* the
    viewer holds that model's own ``view_`` codename — otherwise plain
    text (the same partial-privilege posture Stage A's views already take:
    a missing codename degrades the page rather than 403ing it wholesale).
    """
    text = str(value)
    spec = _SPEC_BY_MODEL.get(type(value))
    if spec is None:
        return LinkedText(text)
    codename = f"{value._meta.app_label}.view_{value._meta.model_name}"
    if not user.has_perm(codename):
        return LinkedText(text)
    return LinkedText(text, reverse("inventory:model_detail", args=[spec.slug, value.pk]))


def _render_cell(obj: Any, field_spec: FieldSpec, user: Any) -> tuple[LinkedText, ...]:
    """The fixed render vocabulary (Stage B): every ``FieldSpec`` renders to
    one or more ``LinkedText`` items via exactly one of these five kinds —
    the whole reason a template never needs to know which model it's
    looking at.
    """
    if field_spec.render == "choice":
        display = _resolve_choice_display(obj, field_spec.accessor)  # type: ignore[arg-type]
        return (LinkedText(_text_or_dash(display)),)
    value = _resolve_field_value(obj, field_spec)
    if field_spec.render == "text":
        return (LinkedText(_text_or_dash(value)),)
    if field_spec.render == "boolean":
        return (LinkedText("Yes" if value else "No"),)
    if field_spec.render == "relation":
        if value is None:
            return (LinkedText("—"),)
        return (_linked_text_for(value, user),)
    if field_spec.render == "m2m":
        items = [_linked_text_for(item, user) for item in value.all()] if value is not None else []
        return tuple(items) if items else (LinkedText("—"),)
    raise AssertionError(f"unknown render kind {field_spec.render!r}")  # pragma: no cover


@dataclass(frozen=True)
class RenderedField:
    label: str
    items: tuple[LinkedText, ...]


@dataclass(frozen=True)
class RenderedInline:
    label: str
    column_labels: tuple[str, ...]
    rows: list[tuple[tuple[LinkedText, ...], ...]]


@dataclass(frozen=True)
class ModelListRow:
    pk: int
    cells: tuple[tuple[LinkedText, ...], ...]


def _sorted_by_ordering(rows: list[Any], ordering: tuple[str, ...]) -> list[Any]:
    """Sorts already-fetched rows in Python by an ORM-style ordering tuple
    (``"vlan__vlan_id"``, ``"port_number"`` — all ascending; nothing in
    this registry needs a descending inline order).

    Deliberately *not* ``queryset.order_by(*ordering)``: ``obj``'s query
    already carries ``detail_prefetch_related``, and calling
    ``.order_by()`` on a prefetched related manager's ``.all()`` returns a
    *new* queryset that can no longer see the prefetch cache — Django then
    re-fetches from the database, once per row, the moment any field
    reached through that fresh queryset needs a relation the prefetch was
    supposed to have already loaded. That's an N+1 that query-count tests
    alone would only catch by accident; sorting the already-prefetched
    Python objects instead costs nothing extra. A stable sort applied
    field-by-field in reverse order is the standard trick for a correct
    multi-key sort.
    """
    for ordering_field in reversed(ordering):

        def _key(row: Any, ordering_field: str = ordering_field) -> Any:
            return _resolve_dotted(row, ordering_field.replace("__", "."))

        rows = sorted(rows, key=_key)
    return rows


def _render_inline(obj: Model, inline_spec: InlineSpec, user: Any) -> RenderedInline:
    rows_qs = list(getattr(obj, inline_spec.accessor).all())
    if inline_spec.ordering:
        rows_qs = _sorted_by_ordering(rows_qs, inline_spec.ordering)
    rows = [tuple(_render_cell(row, column, user) for column in inline_spec.columns) for row in rows_qs]
    return RenderedInline(
        label=inline_spec.label,
        column_labels=tuple(column.label for column in inline_spec.columns),
        rows=rows,
    )


def registry_permission_required(which: Literal["list", "detail"]) -> Callable[[Callable], Callable]:
    """Innermost decorator (stacking order unchanged from Stage A —
    ``login_required`` outermost, then ``require_GET``, then this):
    resolves ``slug`` (a URL kwarg) to its ``ModelSpec``, 404s an unknown
    slug, then checks the spec's own ``list_permissions``/
    ``detail_permissions`` with ``has_perms()``.

    Exists because Stage A's ``@permission_required([...])`` binds a
    literal codename list at import time (``views.py`` — see
    ``rack_detail``'s decorator), and one generic view here serves a
    different permission set per registry entry, resolved only once the
    request names which model it wants.
    """

    def decorator(view_func: Callable) -> Callable:
        @wraps(view_func)
        def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            slug = kwargs.get("slug")
            spec = REGISTRY.get(slug) if slug is not None else None
            if spec is None:
                raise Http404(f"No such model {slug!r}")
            codenames = spec.list_permissions if which == "list" else spec.detail_permissions
            if not request.user.has_perms(codenames):
                raise PermissionDenied
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


@login_required
@require_GET
@registry_permission_required("list")
def model_list(request: HttpRequest, slug: str) -> HttpResponse:
    """The read-parity list page for one registered model — every
    ``list_column`` from its ``ModelSpec``, rendered through the fixed
    render vocabulary and nothing else. Not paginated: every model this
    registry covers is small (the largest, ``NetworkDevice``, is in the
    low hundreds even at full production scale) and Stage B reserves
    pagination for the one table that's genuinely unbounded — the audit
    trail (decision 16).
    """
    spec = REGISTRY[slug]
    queryset = spec.model._default_manager.all()
    if spec.ordering:
        queryset = queryset.order_by(*spec.ordering)
    if spec.list_select_related:
        queryset = queryset.select_related(*spec.list_select_related)
    if spec.list_prefetch_related:
        queryset = queryset.prefetch_related(*spec.list_prefetch_related)
    rows = [
        ModelListRow(
            pk=obj.pk, cells=tuple(_render_cell(obj, column, request.user) for column in spec.list_columns)
        )
        for obj in queryset
    ]
    context = {
        "spec": spec,
        "column_labels": tuple(column.label for column in spec.list_columns),
        "rows": rows,
    }
    return render(request, "inventory/model_list.html", context)


@login_required
@require_GET
@registry_permission_required("detail")
def model_detail(request: HttpRequest, slug: str, pk: int) -> HttpResponse:
    """The read-parity detail page for one registered model — every
    ``detail_field`` and every inline from its ``ModelSpec``.

    When the spec declares ``canonical_detail_view`` (Rack, NetworkDevice
    — decision 13), this issues a permanent redirect to that shaped view
    instead of rendering anything: the shaped page absorbed the fields it
    was missing (see ``rack_detail``/``device_detail``'s docstrings), so
    two pages never compete to be the record of the same object. A
    redirect is still a read — ``@require_GET`` is unaffected.
    """
    spec = REGISTRY[slug]
    if spec.canonical_detail_view:
        get_object_or_404(spec.model, pk=pk)
        return redirect(spec.canonical_detail_view, pk, permanent=True)

    queryset = spec.model._default_manager.all()
    if spec.detail_select_related:
        queryset = queryset.select_related(*spec.detail_select_related)
    if spec.detail_prefetch_related:
        queryset = queryset.prefetch_related(*spec.detail_prefetch_related)
    obj = get_object_or_404(queryset, pk=pk)

    fields = [
        RenderedField(label=column.label, items=_render_cell(obj, column, request.user))
        for column in spec.detail_fields
    ]
    inlines = [_render_inline(obj, inline_spec, request.user) for inline_spec in spec.inlines]

    # The audit panel renders only for the one generic page ADR 0020
    # decision 6 names outright (rack/switch/device) — Rack and
    # NetworkDevice get theirs on their shaped pages instead (see
    # rack_detail/device_detail), so NetworkSwitch's generic page is the
    # only model_detail render that needs one at all.
    audit_entries: list[AuditRow] | None = None
    audit_content_type_pk: int | None = None
    if slug == "networkswitch":
        audit_entries, audit_content_type_pk = _object_audit_panel_context(obj, request.user)

    context = {
        "spec": spec,
        "object": obj,
        "fields": fields,
        "inlines": inlines,
        "admin_change_url": reverse(f"admin:inventory_{spec.model._meta.model_name}_change", args=[obj.pk]),
        "audit_entries": audit_entries,
        "audit_content_type_pk": audit_content_type_pk,
    }
    return render(request, "inventory/model_detail.html", context)


# ---------------------------------------------------------------------------
# Audit trail (phase 15, ADR 0020, Stage B)
# ---------------------------------------------------------------------------
#
# Gated on auditlog.view_logentry — granted to Viewers deliberately
# (sync_roles.py:40-45, "who-changed-what is part of" seeing all data).
# Renders LogEntry.changes and nothing else (decision 15): no object-state
# reconstruction, and no applying today's AUDITLOG_INCLUDE_TRACKING_MODELS
# (settings.py:263, 17 entries mixing whole-model/include_fields/
# exclude_fields/m2m_fields shapes) to an old row — tracking config
# changes over time, and a row records what was tracked *then*.


@dataclass(frozen=True)
class ChangeRow:
    field: str
    text: str


@dataclass(frozen=True)
class AuditRow:
    pk: int
    timestamp: Any
    actor_label: str
    action_label: str
    content_type_label: str
    object_label: str
    object_url: str | None
    changes: list[ChangeRow]


def render_log_entry_changes(entry: LogEntry) -> list[ChangeRow]:
    """``LogEntry.changes_dict`` rendered field by field — deliberately
    **not** ``LogEntry.changes_display_dict()``: verified against the
    installed django-auditlog 3.4.1, that property does ``model =
    self.content_type.model_class()`` and then ``auditlog.contains(model
    ._meta.model)`` with no null guard, raising ``AttributeError`` for a
    content type whose model is no longer installed — despite the comment
    directly above it claiming to "gracefully handle" exactly that case.

    Two shapes, both real: a scalar change is ``{field: [old, new]}``; an
    m2m change (``models.py:122-127`` in the installed package) is
    ``{field: {"type": "m2m", "operation": ..., "objects": [...]}}`` — a
    renderer that assumes every value is a two-element list crashes or
    renders garbage on every profile/template VLAN change, which is
    exactly the shape ``SwitchPortVlanProfile.allowed_vlans``/
    ``RackTemplate.vlans`` produce.
    """
    changes = entry.changes_dict  # {} for a null/empty `changes` column
    rows = []
    for field_name, value in changes.items():
        if isinstance(value, dict) and value.get("type") == "m2m":
            operation = value.get("operation", "")
            objects = value.get("objects", [])
            text = f"{operation}: {', '.join(str(o) for o in objects)}" if objects else str(operation)
        else:
            try:
                old, new = value
            except (TypeError, ValueError):
                text = str(value)  # a shape neither renderer above recognises; show it raw rather than 500
            else:
                text = f"{_text_or_dash(old)} → {_text_or_dash(new)}"
        rows.append(ChangeRow(field=field_name, text=text))
    return rows


def _actor_label(entry: LogEntry) -> str:
    """``actor`` if set, else the retained ``actor_email`` fallback, else
    "system or deleted actor" — ``actor`` is ``on_delete=SET_NULL`` in the
    installed package, so a deleted user leaves rows behind with
    ``actor_email`` as the only remaining record of who it was. A blank
    actor column would read as "nobody did this", which is never true.
    """
    if entry.actor_id is not None and entry.actor is not None:
        return str(entry.actor)
    if entry.actor_email:
        return entry.actor_email
    return "system or deleted actor"


def _render_log_entry(entry: LogEntry, user: Any) -> AuditRow:
    """Shared by the site-wide ``/audit/`` list and every per-object panel
    (``_object_audit_panel_context``) — one renderer, one actor fallback,
    so the two can never show a different answer for the same row.
    """
    model = entry.content_type.model_class()
    object_url = None
    if model is not None and entry.object_id is not None:
        spec = _SPEC_BY_MODEL.get(model)
        if spec is not None:
            codename = f"{model._meta.app_label}.view_{model._meta.model_name}"
            if user.has_perm(codename):
                object_url = reverse("inventory:model_detail", args=[spec.slug, entry.object_id])
    return AuditRow(
        pk=entry.pk,
        timestamp=entry.timestamp,
        actor_label=_actor_label(entry),
        action_label=entry.get_action_display(),
        content_type_label=str(entry.content_type),
        object_label=entry.object_repr,
        object_url=object_url,
        changes=render_log_entry_changes(entry),
    )


def _content_type_for_model_no_create(model: type[Model]) -> ContentType | None:
    """A pure, never-creating lookup for ``model``'s ``ContentType`` row.

    ``ContentType.objects.get_for_model()`` — and, transitively,
    ``auditlog``'s own ``LogEntry.objects.get_for_object()``, which calls
    it internally — is documented to create the row on a cache miss
    (``django/contrib/contenttypes/models.py``: "creating the ContentType
    if necessary"). That makes loading an audit panel on an otherwise
    plain ``GET`` request capable of an ``INSERT`` — exactly the write
    this stage's central claim says never happens (Codex review). In
    practice every registered model's row already exists (Django's
    ``post_migrate`` signal creates one per model at migration time), so
    this only changes behaviour on the should-never-happen miss: render no
    history rather than silently create a row to answer a read.
    """
    try:
        return ContentType.objects.get(app_label=model._meta.app_label, model=model._meta.model_name)
    except ContentType.DoesNotExist:
        return None


def _object_audit_panel_context(obj: Model, user: Any) -> tuple[list[AuditRow] | None, int | None]:
    """The most recent 20 ``LogEntry`` rows for ``obj``, rendered the same
    way as the site-wide list — shared by the rack elevation, the switch
    parity page and the device page (ADR 0020 decision 6). Returns
    ``(None, None)`` when the viewer lacks ``auditlog.view_logentry``,
    which is the query-avoiding half of the belt-and-suspenders posture:
    the template *also* gates the panel on ``perms.auditlog.view_logentry``
    (Stage B: the panel must never turn a missing audit grant into a 403
    on an otherwise-permitted page), but there is no reason to pay for the
    query at all when the answer is already known here.

    Built directly against ``LogEntry`` with a non-creating content-type
    lookup rather than ``LogEntry.objects.get_for_object()`` — see
    ``_content_type_for_model_no_create``'s docstring. ``object_id`` (not
    ``object_pk``) is the right column here, mirroring ``get_for_object``'s
    own branch for an integer pk, which every model this stage covers has.
    """
    if not user.has_perm("auditlog.view_logentry"):
        return None, None
    content_type = _content_type_for_model_no_create(type(obj))
    if content_type is None:
        return [], None  # no ContentType row at all means no LogEntry can reference this model either
    entries = (
        LogEntry.objects.filter(content_type=content_type, object_id=obj.pk)
        .select_related("actor", "content_type")
        .order_by("-timestamp", "-pk")[:20]
    )
    return [_render_log_entry(entry, user) for entry in entries], content_type.pk


def _parse_filter_int(value: str) -> tuple[int | None, bool]:
    """``(parsed_value, ignored)`` for one audit filter's raw query-string
    value. A value that doesn't parse is *ignored*, not a 500 or a silent
    no-op — the page renders a visible note (decision 16) so "this filter
    did nothing because it was garbage" and "this filter matched nothing"
    stay two different, both-true statements.
    """
    try:
        return int(value), False
    except ValueError:
        return None, True


def _tracked_actors() -> list[Any]:
    actor_ids = LogEntry.objects.exclude(actor_id__isnull=True).values_list("actor_id", flat=True).distinct()
    return list(User.objects.filter(pk__in=actor_ids).order_by("username"))


def _tracked_content_types() -> list[ContentType]:
    """Every ``ContentType`` that actually has audit rows — sourced from
    ``LogEntry`` itself, not from ``REGISTRY``, so a content type for a
    model that's since been removed from the registry (or the codebase
    entirely) stays a selectable filter and its rows stay reachable.
    """
    content_type_ids = LogEntry.objects.values_list("content_type_id", flat=True).distinct()
    return list(ContentType.objects.filter(pk__in=content_type_ids).order_by("app_label", "model"))


@login_required
@require_GET
@permission_required(["auditlog.view_logentry"], raise_exception=True)
def audit(request: HttpRequest) -> HttpResponse:
    """Site-wide audit history — ordered ``(-timestamp, -pk)`` (decision
    16: ``-timestamp`` alone is not a total order, so ties would duplicate
    or skip rows across a page boundary), paginated at 50/page with
    Django's standard ``Paginator`` (the ``COUNT(*)`` cost is accepted for
    an installation this size; keyset pagination over ``(timestamp, pk)``
    is the named escape hatch if it ever hurts, and needs no template
    change).

    Three optional filters — ``actor`` (user pk), ``action`` (the
    ``LogEntry.Action`` integer), ``content_type`` (pk) — each either
    narrows the queryset or, if its value doesn't parse, is ignored with a
    visible note rather than a 500 or a silent no-op.
    """
    queryset = LogEntry.objects.select_related("actor", "content_type").order_by("-timestamp", "-pk")
    ignored_filters: list[str] = []

    actor_param = request.GET.get("actor", "")
    if actor_param:
        actor_id, ignored = _parse_filter_int(actor_param)
        if ignored:
            ignored_filters.append("actor")
        else:
            queryset = queryset.filter(actor_id=actor_id)

    action_param = request.GET.get("action", "")
    if action_param:
        action_value, ignored = _parse_filter_int(action_param)
        if ignored:
            ignored_filters.append("action")
        else:
            queryset = queryset.filter(action=action_value)

    content_type_param = request.GET.get("content_type", "")
    if content_type_param:
        content_type_id, ignored = _parse_filter_int(content_type_param)
        if ignored:
            ignored_filters.append("content_type")
        else:
            queryset = queryset.filter(content_type_id=content_type_id)

    paginator = Paginator(queryset, 50)
    page_obj = paginator.get_page(request.GET.get("page"))
    entries = [_render_log_entry(entry, request.user) for entry in page_obj.object_list]

    preserved_params = {key: value for key, value in request.GET.items() if key != "page"}
    querystring_prefix = f"{urlencode(preserved_params)}&" if preserved_params else ""

    context = {
        "entries": entries,
        "page_obj": page_obj,
        "querystring_prefix": querystring_prefix,
        "ignored_filters": ignored_filters,
        "actor_choices": _tracked_actors(),
        "action_choices": LogEntry.Action.choices,
        "content_type_choices": _tracked_content_types(),
        "actor_param": actor_param,
        "action_param": action_param,
        "content_type_param": content_type_param,
    }
    return render(request, "inventory/audit.html", context)
