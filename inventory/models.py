"""Domain models for Network Addresser 9000.

Canonical terminology lives in CONTEXT.md; design rationale and trade-offs
behind specific fields/relations are recorded as ADRs in docs/adr/. Address
suggestion and overlap validation (phase 3, see ROADMAP.md) live here too:
suggestion arithmetic itself is in suggestions.py, wired into each model's
``clean()`` so a blank suggested field is filled in on creation only —
matching ADR 0001's "suggests, but admin can override; once set, static."

Port profiles (phase 8, ADR 0010) live here too: a Network Switch/Device
Type is a *purpose profile* built on a hardware model — the same hardware
can have several profiles when what its ports are used for differs (see
``NetworkSwitchType``/``NetworkDeviceType``). Each profile owns a list of
``*TypePort`` template rows, which are copied exactly once into real
``NetworkSwitchPort``/``NetworkDevicePort`` rows when an instance of that
type is first created (``_materialize_ports``) — never re-synced
afterward. A type is immutable once it has any instance, and a profile's
type ports are locked once the profile has any instance, so "this profile"
always means one fixed port layout.

``SwitchPortVlanProfile`` (ADR 0012) is a *different* kind of profile, named
the same way by DESIGN.md but referenced *live* rather than seed-once:
``NetworkSwitchTypePort``/``NetworkSwitchPort`` store a profile id, not a
copy of its VLAN config, so editing a profile's allowed VLANs reaches every
port using it immediately — the one deliberate exception to this module's
otherwise seed-once/never-re-synced rule.
"""

import ipaddress
from collections import defaultdict
from collections.abc import Iterable
from typing import Any, cast

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Value
from django.db.models.functions import Coalesce, Concat

from . import dante
from .suggestions import (
    dhcp_range_overlaps_cidr,
    range_at_offset,
    range_offset,
    ranges_overlap,
    required_block_size,
    suggest_aligned_offset,
    suggest_default_gateway,
    suggest_rack_vlan_range,
    suggest_slot_address,
)
from .validators import validate_dns_label, validate_ipv4_cidr


class PortType(models.TextChoices):
    """Structured physical port type: link speed + connector.

    Shared by both Type Ports (the template) and instance ports (the
    materialized copy) — see module docstring. ``OTHER`` is an explicit
    safety valve so unlisted/uncommon hardware never blocks data entry; the
    rest of the list is extended by release as new hardware shows up, not
    meant to be exhaustive up front.
    """

    TEN_100M_RJ45 = "10_100m_rj45", "10/100M RJ45"
    GBE_RJ45 = "1gbe_rj45", "1GbE RJ45 (copper)"
    GBE_SFP = "1gbe_sfp", "1GbE SFP"
    GBE_COMBO = "1gbe_combo", "1GbE Combo (RJ45/SFP)"
    TWO_5GBE_RJ45 = "2_5gbe_rj45", "2.5GbE RJ45"
    TEN_GBE_RJ45 = "10gbe_rj45", "10GbE RJ45"
    TEN_GBE_SFP_PLUS = "10gbe_sfp_plus", "10GbE SFP+"
    TWENTYFIVE_GBE_SFP28 = "25gbe_sfp28", "25GbE SFP28"
    OTHER = "other", "Other / Unknown"


class PortAddressing(models.TextChoices):
    """Creation-time choice of how a device's ports materialize (ADR 0013).

    Transient — never stored (see ``NetworkDevice.port_addressing``); the
    materialized ``NetworkDevicePort`` rows themselves are the record of
    what was chosen.
    """

    STATIC = "static", "Static"
    DHCP = "dhcp", "DHCP"


class SwitchAddressing(models.TextChoices):
    """Creation-time choice of whether a switch's VLAN addresses
    materialize (ADR 0016).

    Not a reuse of ``PortAddressing``: ``NetworkSwitchAddress`` has no
    ``is_dhcp`` field, so there's nothing for a ``DHCP`` value to
    represent. ``MANUAL`` means "I will add addresses myself," not "this
    switch has no addresses" — transient, never stored, same reasoning as
    ``PortAddressing`` (see ``NetworkSwitch.address_materialization``).
    """

    STATIC = "static", "Static"
    MANUAL = "manual", "Manual"


class PortMode(models.TextChoices):
    """Switch port L2 mode — shared by switch Type Ports and instance ports."""

    TRUNK = "trunk", "Trunk"
    ACCESS = "access", "Access"


def _get_related(instance: Any, field_name: str) -> Any | None:
    """Safely read FK ``field_name`` off ``instance``, ``None`` if unset.

    Needed because Django admin's inline formsets set the FK's raw
    ``<field>_id`` to ``None`` for a not-yet-saved parent — see
    ``BaseInlineFormSet._construct_form``, which does this deliberately
    so form validation doesn't choke on a pk that doesn't exist yet — even
    though the actual related object is available on the instance. A plain
    ``instance.<field>_id`` truthiness check would wrongly read as "no
    parent assigned" while adding a new parent and its inline children in
    the same admin submission; accessing the descriptor directly returns
    the in-memory (possibly unsaved) object instead.
    """
    try:
        return getattr(instance, field_name)
    except ObjectDoesNotExist:
        return None


def _normalize_update_fields(
    model_cls: type[models.Model], update_fields: "list[str] | frozenset[str] | None"
) -> "frozenset[str] | None":
    """Normalize ``update_fields`` to field names (Django's own
    ``update_fields`` validation accepts either a field's name or its
    attname, e.g. both ``"switch_type"`` and ``"switch_type_id"``) so
    callers can test set membership without caring which spelling was
    passed. ``None`` (an unrestricted save) passes through unchanged —
    callers must treat that as "everything," not "nothing."

    Shared by ``_check_locked_fields_unchanged`` (below) and
    ``NetworkDeviceTypePort._hostname_suffix_only_edit()``, which needs the
    identical normalization to decide whether a given
    ``save(update_fields=...)`` actually touched a given field.
    """
    if update_fields is None:
        return None
    attname_to_name = {
        field.attname: field.name for field in model_cls._meta.concrete_fields if field.attname != field.name
    }
    return frozenset(attname_to_name.get(name, name) for name in update_fields)


def _check_locked_fields_unchanged(
    model_cls: type[models.Model],
    pk: int,
    current_values: dict[str, Any],
    *,
    update_fields: "list[str] | frozenset[str] | None",
) -> None:
    """Raise ``ValidationError`` if any of ``current_values`` differs from
    what's actually persisted for ``pk``.

    This is the enforcement mechanism for every "locked after creation"
    invariant in this module (a switch/device's type, an instance port's
    hardware/purpose fields, a type's declared ``port_count``). It runs
    from inside ``save()`` itself — not just ``clean()`` — because Django
    never calls ``clean()``/``full_clean()`` from ``save()``, so a plain
    ``instance.save()`` with no ``full_clean()`` call would otherwise
    silently bypass the invariant.

    ``current_values`` keys must be actual field names as Django's
    ``update_fields`` would name them (e.g. ``"switch_type"``, not
    ``"switch_type_id"``) — ``QuerySet.values()`` accepts either and
    returns the FK's raw id either way, so this stays consistent with
    ``current_values`` holding raw ids (``self.switch_type_id``) for FK
    fields.

    If ``update_fields`` is given and none of ``current_values``' keys are
    in it, this is a no-op: a ``save(update_fields=...)`` that explicitly
    excludes a locked field isn't changing it, regardless of what the
    in-memory instance happens to hold. Django's own ``update_fields``
    validation accepts either a field's name or its attname (e.g. both
    ``"switch_type"`` and ``"switch_type_id"``), so ``update_fields`` is
    normalized to field names before this comparison — otherwise
    ``save(update_fields=["switch_type_id"])`` would look like it excludes
    the locked field when it doesn't.

    Known gap (documented, not closed): ``QuerySet.update()`` and
    ``bulk_create()`` bypass ``Model.save()`` entirely and are not guarded
    by this at all. They are unsupported for locked fields on the models
    below.

    Known gap (documented, not closed), same root cause:
    ``SwitchPortVlanProfile.allowed_vlans`` isn't itself a field this helper
    checks (M2M managers don't go through ``Model.save()`` at all — see ADR
    0012), so its own locking/validation lives elsewhere: an ``m2m_changed``
    receiver for ``.add()``/``.set()``/``.clear()``, and
    ``SwitchPortVlanProfileAllowedVlan.save()``/``.clean()`` for direct
    through-row writes. A raw ``bulk_create()`` against that through table
    still bypasses both and is unsupported, consistent with the
    ``QuerySet.update()``/``bulk_create()`` gap above.
    """
    normalized_update_fields = _normalize_update_fields(model_cls, update_fields)
    if normalized_update_fields is not None and not (set(current_values) & normalized_update_fields):
        return
    original = model_cls._default_manager.filter(pk=pk).values(*current_values.keys()).first()
    if original is None:
        return  # row not visible (e.g. mid-delete elsewhere) — nothing to compare against
    changed = [field for field, value in current_values.items() if original[field] != value]
    if changed:
        raise ValidationError(
            f"{', '.join(sorted(changed))} cannot be changed after creation on "
            f"{model_cls.__name__} — remove and recreate this row instead."
        )


def _lock_type_rows(model_cls: type[models.Model], *pks: int | None) -> None:
    """Acquire a row lock (``SELECT ... FOR UPDATE``) on the given type
    rows — must run inside ``transaction.atomic()``.

    Serializes a profile's first materialization against a concurrent edit
    to its own port templates/count: without this, a switch/device create
    (reading the profile's current port templates) and a type-port edit
    (checking whether the profile is locked yet) can each independently
    observe a stale "not locked yet" state and both proceed, leaving the
    new instance's materialized ports out of sync with the profile it was
    supposedly copied from.
    """
    ids = sorted({pk for pk in pks if pk is not None})
    if ids:
        list(model_cls._default_manager.select_for_update().filter(pk__in=ids))


def _validate_switch_type_port_profile(switch_type: "NetworkSwitchType") -> None:
    """Raise ``ValidationError`` if ``switch_type``'s port profile is
    incomplete, or its port numbers don't form a contiguous ``1..port_count``
    sequence.

    Contiguity (not just count) matters here because ``port_count`` is
    meant to describe the numbered physical range 1..N — three type ports
    numbered 1, 2, and 99 would pass a bare count check but not actually
    describe a real N-port switch.
    """
    numbers = sorted(switch_type.type_ports.values_list("port_number", flat=True))
    if len(numbers) != switch_type.port_count:
        raise ValidationError(
            f"{switch_type} declares port_count {switch_type.port_count} but has "
            f"{len(numbers)} Network Switch Type Port(s) defined — define all of them "
            "before creating a switch of this type."
        )
    if numbers != list(range(1, switch_type.port_count + 1)):
        raise ValidationError(
            f"{switch_type}'s Network Switch Type Ports aren't numbered contiguously "
            f"1..{switch_type.port_count} (found {numbers})."
        )


def _validate_device_type_port_profile(device_type: "NetworkDeviceType") -> None:
    """Raise ``ValidationError`` if ``device_type``'s port profile is
    incomplete, or a non-zero ``slot_offset`` port has no offset-0 port on
    the same VLAN. Device type ports have no numbering requirement (unlike
    switch type ports) since ``port_number`` is optional for these.

    The offset-0-per-VLAN requirement predates ADR 0027 (it comes from ADR
    0017) and stays even though every port now derives independently from
    the device's own ``rack_slot`` rather than from a same-VLAN sibling —
    ADR 0027 itself notes this is "already satisfied" by every live
    console's built-in Dante Primary port, not that it's being relaxed.

    Called unconditionally from both ``NetworkDevice.clean()`` and
    ``_materialize_ports()`` — every addressing path, not only the static
    one. The offset check in particular must live here rather than in
    ``_check_static_materialization_possible()`` (ADR 0017 plan review,
    note 4): that method only runs for a racked+static device, so a DHCP
    or unracked device would otherwise sail past it and materialize an
    offset port with no offset-0 sibling on its VLAN — a row that could
    never correctly be made static later either.
    """
    count = device_type.type_ports.count()
    if count != device_type.port_count:
        raise ValidationError(
            f"{device_type} declares port_count {device_type.port_count} but has "
            f"{count} Network Device Type Port(s) defined — define all of them before "
            "creating a device of this type."
        )
    offsets_by_vlan: dict[int, set[int]] = {}
    vlan_by_id: dict[int, VLAN] = {}
    for type_port in device_type.type_ports.select_related("vlan"):
        offsets_by_vlan.setdefault(type_port.vlan_id, set()).add(type_port.slot_offset)
        vlan_by_id[type_port.vlan_id] = type_port.vlan
    for vlan_id, offsets in offsets_by_vlan.items():
        if 0 not in offsets:
            vlan = vlan_by_id[vlan_id]
            raise ValidationError(
                f"{device_type} has a slot_offset port on {vlan} with no offset-0 port on "
                f"that VLAN to derive its address from — add one, or set every port on "
                f"{vlan} to slot_offset 0."
            )


def _suggest_rack_slot_address(
    rack: "Rack | None", rack_slot: int | None, vlan_id: int, slot_offset: int = 0
) -> str | None:
    """Suggested static address for a rack-slot occupant on ``vlan_id``.

    ``None`` if unracked, or no ``RackVlanRange`` exists yet for that VLAN.
    Shared by ``NetworkSwitchAddress`` and ``NetworkDevicePort``.

    ``slot_offset`` (ADR 0017) shifts the suggestion past the occupant's
    own ordinal — ``range_base + rack_slot + slot_offset`` — for a device
    port whose address is derived from another port's rather than its own
    slot. Every other caller takes the default ``0``, which is exactly
    today's ``range_base + rack_slot`` and leaves their behaviour
    unchanged. This introduces no new arithmetic — ``suggest_slot_address``
    itself is unchanged; offsets only change what's passed as its ``slot``.
    """
    if rack is None or rack_slot is None:
        return None
    try:
        rack_range = rack.vlan_ranges.get(vlan_id=vlan_id)
    except RackVlanRange.DoesNotExist:
        return None
    try:
        validate_ipv4_cidr(rack_range.address_range)
    except ValidationError:
        return None  # that range's own malformed value; its own clean() would report it
    try:
        return suggest_slot_address(rack_range.address_range, rack_slot + slot_offset)
    except ValueError:
        # rack_slot (+ slot_offset) bypassed clean()'s span-vs-slot_count
        # guard (save() alone never enforces it) and overflows this range's
        # block — its own clean() would report that.
        return None


def occupied_rack_slot_ordinals(rack: "Rack") -> dict[int, str]:
    """Every ordinal claimed in ``rack`` (ADR 0027), mapped to a label for
    whatever claims it — a switch's own ``rack_slot``, or ``{rack_slot +
    offset}`` for each distinct ``slot_offset`` a device's type declares,
    always including 0 (a device occupies its own slot regardless of
    whether any type port sits there — ``NetworkDeviceType.
    claimed_offsets``).

    Two bounded queries — the same "two, not per-row" budget
    ``occupied_rack_slot_ranges()`` (below, now built on this) always had.
    Switches need no join (a switch always claims exactly one ordinal, its
    own). Devices join straight through to ``device_type__type_ports`` in
    **one** query — SQL aggregates can express "the highest offset"
    (``slot_span``) but not "the distinct set of offsets", so this walks
    the join's rows (one per (device, type port) pair — a ``LEFT OUTER
    JOIN``, so a type with zero type ports still contributes one row, with
    a ``None`` offset) in Python instead of annotating them.

    Last write wins on a genuinely double-claimed ordinal (reachable only
    through a ``clean()``-bypassing bare ``.save()`` — see
    ``RackSlotAssignmentMixin``'s "Known gap" docstring); this is a
    convenience lookup, not the authority on conflicts, which the rack
    elevation surfaces separately (``ElevationRow.conflicts``).
    """
    ordinals: dict[int, str] = {}
    for rack_slot, hostname, pk in NetworkSwitch.objects.filter(
        rack=rack, rack_slot__isnull=False
    ).values_list("rack_slot", "hostname", "pk"):
        ordinals[cast(int, rack_slot)] = hostname or f"Switch #{pk}"
    for device_id, hostname, rack_slot, offset in NetworkDevice.objects.filter(
        rack=rack, rack_slot__isnull=False
    ).values_list("id", "hostname", "rack_slot", "device_type__type_ports__slot_offset"):
        label = hostname or f"Device #{device_id}"
        ordinals[cast(int, rack_slot)] = label  # offset 0 always claimed, regardless of ports
        ordinals[cast(int, rack_slot) + (offset or 0)] = label
    return ordinals


def _bulk_claimed_offsets(device_type_ids: Iterable[int]) -> dict[int, frozenset[int]]:
    """Bulk form of ``NetworkDeviceType.claimed_offsets`` (ADR 0027
    decision 2) for every id in ``device_type_ids``, as ``{device_type_id:
    frozenset[int]}`` — one query, not one per candidate.

    ``NetworkSwitch``/``NetworkDevice._check_rack_slot_not_occupied()``
    each walk a SQL-prefiltered candidate list and ask every candidate's
    own ``claimed_offsets`` — calling that property directly there costs
    one extra, uncached query per candidate (it recomputes on every
    access by design; see its own docstring), an N+1 that scales with
    rack occupancy exactly where ADR 0027's own ``{0, 64}``-shaped
    hardware widens the candidate set the most: the SQL prefilter still
    narrows by the contiguous *envelope* (``rack_slot .. rack_slot +
    slot_span - 1``), which is at its widest for exactly this kind of
    type. Resolving every candidate's offsets here first, in one query,
    closes it. Mirrors ``views.resolve_claimed_offsets()``'s identical
    shape — duplicated rather than imported, since ``views.py`` imports
    from ``models.py`` and not the other way around.
    """
    device_type_ids = set(device_type_ids)
    if not device_type_ids:
        return {}
    offsets_by_type: dict[int, set[int]] = defaultdict(set)
    for device_type_id, offset in NetworkDeviceTypePort.objects.filter(
        device_type_id__in=device_type_ids
    ).values_list("device_type_id", "slot_offset"):
        offsets_by_type[device_type_id].add(offset)
    # Always includes 0 (a device occupies its own slot regardless of
    # whether any type port is declared there), and covers a type with
    # zero type ports, which contributes no row to the query above.
    return {
        device_type_id: frozenset(offsets_by_type.get(device_type_id, set()) | {0})
        for device_type_id in device_type_ids
    }


def occupied_rack_slot_ranges(rack: "Rack") -> list[tuple[int, int]]:
    """Every occupied ordinal in ``rack``, unioning both equipment tables
    (ADR 0019), as a list of ``(o, o)`` singleton ranges. Public — unlike
    its ``_``-prefixed neighbour above — because ``admin.py`` calls this to
    feed ``suggestions.lowest_free_run()`` for the switch path (a switch
    always spans 1, so a set of ordinals and a set of length-1 ranges are
    the same thing).

    Built on ``occupied_rack_slot_ordinals()`` (ADR 0027) rather than a
    second, span-based query — a device's own placement search moved to
    ``suggestions.lowest_free_placement()``, which wants the raw ordinal
    set directly and no longer calls this at all (ADR 0027 decision 2: the
    device path stops being a ``lowest_free_run()`` caller, since a
    claimed set isn't generally a contiguous run).
    """
    return [(ordinal, ordinal) for ordinal in sorted(occupied_rack_slot_ordinals(rack))]


def _address_containment_error(
    address: str, vlan: "VLAN", rack: "Rack | None", rack_slot: int | None
) -> str | None:
    """``None`` if ``address`` fits ``vlan``'s subnet and, when racked, the
    rack's assigned ``RackVlanRange``; otherwise an error message.

    Racked equipment always requires an assigned range — addresses aren't
    allowed to free-float within just the VLAN subnet once racked, since
    that could otherwise land inside the VLAN's DHCP range or on its
    gateway (DESIGN.md's "addresses come from the rack's reserved range"
    model). Pure read — no exclusions/uniqueness here, so it doubles as the
    check for re-validating an *already-saved* address after its equipment
    moves, not just for a fresh/edited address row.
    """
    if not vlan.subnet:
        # L2-only VLAN (ADR 0012) — a legitimate, addressing-less state, not
        # an error to defer to VLAN's own clean() the way a malformed
        # subnet below is; this must reject outright or a static address
        # would silently pass with nothing to validate it against.
        return f"{vlan} has no subnet (L2-only) — it cannot be assigned a static address."
    try:
        validate_ipv4_cidr(vlan.subnet)
    except ValidationError:
        return None  # VLAN's own subnet is malformed; its own clean() will report that
    try:
        address_obj = ipaddress.IPv4Address(address)
    except ValueError:
        return None  # malformed value; the field's own validator already reports it

    vlan_network = ipaddress.IPv4Network(vlan.subnet, strict=True)
    if address_obj not in vlan_network:
        return f"{address} is not within {vlan}'s subnet ({vlan.subnet})."

    if rack is not None and rack_slot is not None:
        try:
            rack_range = rack.vlan_ranges.get(vlan_id=vlan.pk)
        except RackVlanRange.DoesNotExist:
            return (
                f"{rack} has no address range assigned for {vlan} yet — assign one via the "
                "rack's VLAN ranges before addressing equipment on this VLAN."
            )
        try:
            validate_ipv4_cidr(rack_range.address_range)
        except ValidationError:
            return None  # that range's own malformed value; its own clean() will report it
        range_network = ipaddress.IPv4Network(rack_range.address_range, strict=True)
        if address_obj not in range_network:
            return f"{address} is not within {rack}'s range on {vlan} ({rack_range.address_range})."

    if vlan.dhcp_range_start and vlan.dhcp_range_end:
        try:
            dhcp_start = ipaddress.IPv4Address(vlan.dhcp_range_start)
            dhcp_end = ipaddress.IPv4Address(vlan.dhcp_range_end)
        except ValueError:
            pass  # VLAN's own malformed range; its own clean() will report it
        else:
            # Normalized the same way dhcp_range_overlaps_cidr() is: ordering
            # is only enforced by VLAN.clean(), not the DB, so a reversed
            # pair reaching this *different, already-persisted* VLAN's fields
            # via a clean()-bypassing write must not silently make this an
            # unsatisfiable (always-False) comparison — that would silently
            # disable the "reject a static address inside the DHCP range"
            # rule for every address on that VLAN.
            if dhcp_start > dhcp_end:
                dhcp_start, dhcp_end = dhcp_end, dhcp_start
            if dhcp_start <= address_obj <= dhcp_end:
                return (
                    f"{address} falls within {vlan}'s DHCP range "
                    f"({vlan.dhcp_range_start}-{vlan.dhcp_range_end})."
                )
    return None


def _validate_static_address(
    address: str,
    vlan: "VLAN",
    rack: "Rack | None",
    rack_slot: int | None,
    *,
    exclude_switch_address_pk: int | None,
    exclude_device_port_pk: int | None,
) -> None:
    """Shared static-address invariants for ``NetworkSwitchAddress``/``NetworkDevicePort``.

    Validates containment (see ``_address_containment_error``) and
    uniqueness against every other static address on the same VLAN —
    switch or device port alike. No DB constraint can span both tables, so
    the uniqueness half is an interim, full_clean-time-only guard (same
    caveat as ``RackSlotAssignmentMixin``'s cross-table check).
    """
    error = _address_containment_error(address, vlan, rack, rack_slot)
    if error:
        raise ValidationError({"address": error})

    switch_conflicts = NetworkSwitchAddress.objects.filter(vlan=vlan, address=address)
    if exclude_switch_address_pk is not None:
        switch_conflicts = switch_conflicts.exclude(pk=exclude_switch_address_pk)
    switch_conflict = switch_conflicts.first()
    if switch_conflict is not None:
        raise ValidationError(
            {"address": f"{address} is already assigned to {switch_conflict.switch} on {vlan}."}
        )

    device_conflicts = NetworkDevicePort.objects.filter(vlan=vlan, address=address)
    if exclude_device_port_pk is not None:
        device_conflicts = device_conflicts.exclude(pk=exclude_device_port_pk)
    device_conflict = device_conflicts.first()
    if device_conflict is not None:
        raise ValidationError(
            {"address": f"{address} is already assigned to {device_conflict.device} on {vlan}."}
        )


def _stored_hostname(model_cls: type[models.Model], pk: int) -> str | None:
    """The persisted ``hostname`` for ``pk``, or ``None`` if the row isn't
    visible (mirrors ``_check_locked_fields_unchanged()``'s identical
    guard) — what each model's ``clean()`` compares its in-memory value
    against to decide whether this save is a rename at all.
    """
    return model_cls._default_manager.filter(pk=pk).values_list("hostname", flat=True).first()


def _validate_hostname_unique(
    hostname: str,
    *,
    exclude_switch_pk: int | None,
    exclude_device_pk: int | None,
) -> None:
    """Cross-table hostname uniqueness (ADR 0023 decision 6, amended
    twice) — ``NetworkSwitch.hostname``, ``NetworkDevice.hostname`` and
    the derived ``NetworkDevicePort.hostname`` (ADR 0022 decision 4) all
    share one namespace. Same shape as ``_validate_static_address()``: a
    ``full_clean()``-time-only guard, since no database constraint can
    span three tables, and it inherits that check's known cross-table
    race (#5) rather than introducing a second, stricter mechanism.

    Deliberately **not** reused from ``inventory.hostnames.
    hostname_is_taken()`` — that module imports from this one
    (``NetworkSwitch``/``NetworkDevice``/``NetworkDevicePort``), so the
    reverse import would be circular. The two checks share the same
    three-table shape by construction, not by call-sharing.

    **Callers, not this function, decide when to call it** — rename-only
    (ADR 0023 decision 6, amended twice): only when an existing row's
    ``hostname`` differs from what is stored, never on creation. The live
    database holds 32 equipment rows across 5 duplicated hostnames (bare
    model names the importer gave every instance of a model — ``IK42``
    alone names 17 amps), so validating unconditionally would make all 32
    unsaveable, and the importer's ``construct -> full_clean() -> save()``
    path writes duplicate CSV descriptions by design (the addressing CSV
    repeats ``IK42`` eighteen times) — enforcing on creation would break
    every rebuild. Little is lost: the computed path
    (``inventory.hostnames.choose_sequence()``) checks against this same
    predicate before it ever proposes a name, so it cannot collide either
    way; a hand-typed rename is the realistic route to a duplicate, and
    that is exactly what this refuses.

    **The honest consequence: hostnames are not unique in the database,
    and no code may assume they are.** Blank is exempt — the spare pool
    and every pre-phase-18 row need no backfill.
    """
    if not hostname:
        return
    switch_conflicts = NetworkSwitch.objects.filter(hostname=hostname)
    if exclude_switch_pk is not None:
        switch_conflicts = switch_conflicts.exclude(pk=exclude_switch_pk)
    switch_conflict = switch_conflicts.first()
    if switch_conflict is not None:
        raise ValidationError({"hostname": f"{hostname!r} is already in use by {switch_conflict}."})

    device_conflicts = NetworkDevice.objects.filter(hostname=hostname)
    if exclude_device_pk is not None:
        device_conflicts = device_conflicts.exclude(pk=exclude_device_pk)
    device_conflict = device_conflicts.first()
    if device_conflict is not None:
        raise ValidationError({"hostname": f"{hostname!r} is already in use by {device_conflict}."})

    port_conflicts = NetworkDevicePort.objects.filter(source_type_port__hostname_suffix__gt="").exclude(
        device__hostname=""  # Concat would yield "-suffix"; the property returns None, never that
    )
    if exclude_device_pk is not None:
        port_conflicts = port_conflicts.exclude(device_id=exclude_device_pk)
    port_conflict = (
        port_conflicts.annotate(
            derived=Concat("device__hostname", Value("-"), "source_type_port__hostname_suffix")
        )
        .filter(derived=hostname)
        .first()
    )
    if port_conflict is not None:
        raise ValidationError(
            {"hostname": f"{hostname!r} is already in use as a derived port name on {port_conflict.device}."}
        )


def _assemble_hostname_stem(
    *,
    owner_slug: str | None,
    location_slug: str | None,
    type_slug: str | None,
    purpose: str,
    sequence: int | None,
) -> str | None:
    """The same pure dash-join as ``inventory.hostnames.assemble_hostname()``
    — duplicated rather than imported, for the identical circular-import
    reason ``_validate_hostname_unique()`` duplicates ``hostname_is_taken()``'s
    shape above rather than calling it: ``inventory.hostnames`` imports
    ``NetworkSwitch``/``NetworkDevice``/``NetworkDevicePort`` from this
    module, so the reverse import is impossible, and this module never
    reaches up into a module built on top of it.

    ``hostname_diverges`` (ADR 0023 decision 9, corrected in phase 18 PR 4)
    is this function's one call site. If a third ever needs the exact same
    formula, that is the signal to extract it into a genuinely shared,
    import-free module instead of a third copy — not to reach for either
    existing copy from the other's module.

    ``None`` when a blocking component (``owner_slug``/``type_slug``) is
    missing; otherwise the non-blank components dash-joined, exactly
    matching ``assemble_hostname()``'s own contract.
    """
    if not owner_slug or not type_slug:
        return None
    parts = [
        owner_slug,
        location_slug or None,
        type_slug,
        purpose or None,
        None if sequence is None else str(sequence),
    ]
    return "-".join(part for part in parts if part)


class AuditedModel(models.Model):
    """Abstract base recording who created a row and when.

    Mutation history (edits, removals) is layered on top of this in a
    later phase — see ADR 0004 — this base only covers creation.
    """

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
    )

    class Meta:
        abstract = True


class Department(AuditedModel):
    """An operator-facing organizational label a VLAN may optionally carry
    (ADR 0021) — "Audio", "Lighting", "Video". Descriptive only: nothing in
    this codebase branches on a department's value, and it does not scope
    allocation — a rack range, suggestion or stored address is computed
    identically regardless of which department(s) its VLANs belong to.

    Not to be confused with the *role* a VLAN plays within a department
    (Control, Dante Primary, Dante Secondary — ADR 0021 designs it, phase
    21 builds it). Department is who owns the VLAN; role is what code
    branches on to decide which VLAN is which within that ownership. This
    model carries only the former.

    No row here is ever system-seeded (unlike the "Default" Switch Port
    VLAN Profile and its Default VLAN, ADR 0012) — a fresh database has no
    departments until an operator creates one.
    """

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~models.Q(name=""), name="department_name_not_blank"),
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        # Stripped here too, not just clean() — Model.save() never calls
        # clean(), so a direct Department.objects.create(name="Audio ")
        # would otherwise bypass the strip and persist trailing whitespace
        # the DB's case-insensitive collation doesn't also fold away.
        if self.name:
            self.name = self.name.strip()
        super().save(
            force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
        )

    def clean(self) -> None:
        super().clean()
        if self.name:
            self.name = self.name.strip()


class Owner(AuditedModel):
    """Who owns a piece of equipment — "MPS", "BEJ" — the first component
    of a computed hostname (ADR 0023 decision 1). A table rather than free
    text for the same reason as ``Department`` (issue #10).

    Not to be confused with ``Department`` (ADR 0021): a Department owns a
    VLAN and is purely descriptive vocabulary nothing branches on; an
    Owner owns equipment and is a **blocking** hostname component — no
    owner means no computed hostname at all (ADR 0023 decision 1).
    ``Rack.owner`` is a creation-time *default* for a racked item's own
    ``owner``, not inheritance — moving a rack to a different owner never
    touches equipment already in it (ADR 0019's suggest-don't-lock).
    """

    slug = models.CharField(
        max_length=63,
        unique=True,
        validators=[validate_dns_label],
        help_text='Short, DNS-safe identifier used in hostnames, e.g. "mps".',
    )
    name = models.CharField(max_length=100, unique=True, help_text='Full name, e.g. "MPS".')

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~models.Q(slug=""), name="owner_slug_not_blank"),
            models.CheckConstraint(condition=~models.Q(name=""), name="owner_name_not_blank"),
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.slug})"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        # Stripped (and, for slug, lowercased) here too, not just clean() —
        # Model.save() never calls clean() (Department.save()'s reasoning
        # above), so a direct Owner.objects.create(slug="MPS ") would
        # otherwise persist an unstripped, uncased value validate_dns_label
        # would have rejected. slug is lowercased (unlike Department.name,
        # which strips but deliberately doesn't casefold) because it's
        # concatenated verbatim into a hostname.
        if self.slug:
            self.slug = self.slug.strip().lower()
        if self.name:
            self.name = self.name.strip()
        super().save(
            force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
        )

    def clean_fields(self, exclude=None) -> None:
        # Normalize *before* the field validators run — Model.full_clean()
        # calls clean_fields() (which runs slug's own validate_dns_label
        # validator) before clean(), Django's own ordering. Normalizing
        # only in clean()/save() left full_clean() validating the raw,
        # not-yet-normalized value: "MPS " failed validate_dns_label here
        # before clean() ever got a chance to strip/lowercase it,
        # contradicting this field's documented contract. Mirrors
        # NetworkDeviceTypePort.clean_fields()'s identical fix for
        # hostname_suffix.
        if self.slug:
            self.slug = self.slug.strip().lower()
        super().clean_fields(exclude=exclude)

    def clean(self) -> None:
        super().clean()
        if self.slug:
            self.slug = self.slug.strip().lower()
        if self.name:
            self.name = self.name.strip()


class VLAN(AuditedModel):
    """An 802.1Q VLAN and its IPv4 addressing — one row, per CONTEXT.md."""

    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="vlans",
        help_text="Optional. The department that owns this VLAN, e.g. Audio, Lighting, Video.",
    )
    name = models.CharField(max_length=100)
    vlan_id = models.PositiveIntegerField(
        unique=True,
        validators=[MinValueValidator(1), MaxValueValidator(4094)],
        help_text="802.1Q VLAN ID (1-4094).",
    )
    subnet = models.CharField(
        max_length=18,
        blank=True,
        validators=[validate_ipv4_cidr],
        help_text=(
            "IPv4 subnet in CIDR notation, e.g. 10.200.0.0/21. Leave blank for an L2-only VLAN "
            "with no tracked addressing — no gateway, DHCP range, rack range, or static address "
            "may then be set on it."
        ),
    )
    default_gateway = models.GenericIPAddressField(
        protocol="IPv4",
        blank=True,
        null=True,
        help_text="Suggested as the lowest host address in the subnet; stored and overridable.",
    )
    dhcp_range_start = models.GenericIPAddressField(
        protocol="IPv4",
        blank=True,
        null=True,
        help_text=(
            "Start of the DHCP address range (inclusive). Leave both start and end blank if "
            "this VLAN has no DHCP range."
        ),
    )
    dhcp_range_end = models.GenericIPAddressField(
        protocol="IPv4",
        blank=True,
        null=True,
        help_text="End of the DHCP address range (inclusive). Must be greater than the start address.",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(dhcp_range_start__isnull=True, dhcp_range_end__isnull=True)
                    | models.Q(dhcp_range_start__isnull=False, dhcp_range_end__isnull=False)
                ),
                name="vlan_dhcp_range_start_end_together",
            ),
            # Backs the "subnet-less VLAN is L2-only" rule (ADR 0012) at the
            # DB level, not just in clean() — a subnet-less VLAN has nothing
            # for a gateway or DHCP range to be validated against.
            models.CheckConstraint(
                condition=(
                    ~models.Q(subnet="")
                    | (
                        models.Q(default_gateway__isnull=True)
                        & models.Q(dhcp_range_start__isnull=True)
                        & models.Q(dhcp_range_end__isnull=True)
                    )
                ),
                name="vlan_subnetless_has_no_addressing",
            ),
        ]
        ordering = ["vlan_id"]

    def __str__(self) -> str:
        return f"{self.name} (VLAN {self.vlan_id})"

    def clean(self) -> None:
        super().clean()
        if not self.subnet:
            # L2-only VLAN (ADR 0012) — no addressing is tracked at all, so
            # nothing below is meaningful; back this with the DB constraint
            # above, not just this check, since QuerySet.update()/
            # bulk_create() bypass clean() entirely.
            if self.default_gateway or self.dhcp_range_start or self.dhcp_range_end:
                raise ValidationError(
                    "A VLAN with no subnet is L2-only (ADR 0012) and cannot have a default "
                    "gateway or DHCP range."
                )
            # *Blanking* a subnet is a subnet change like any other, so it has
            # to re-validate existing dependents the same way the non-blank
            # path below does — otherwise an already-addressed VLAN could be
            # turned L2-only, stranding rack ranges and static addresses on a
            # VLAN that every other check says may not have them (and that
            # they could then never be re-saved against).
            if self.pk is not None:
                self._reject_blanking_subnet_with_dependents()
            return
        try:
            validate_ipv4_cidr(self.subnet)
        except ValidationError:
            return  # subnet itself is invalid; clean_fields() already reports it
        vlan_network = ipaddress.IPv4Network(self.subnet, strict=True)

        if self.pk is None and not self.default_gateway:
            suggestion = suggest_default_gateway(self.subnet)
            if suggestion:
                self.default_gateway = suggestion

        # From here on, validate the final values regardless of whether they
        # were just suggested, supplied by the admin, or (on an edit) already
        # stored — a changed subnet can just as easily invalidate an existing
        # gateway/DHCP range/rack range as a freshly-typed one.
        if self.default_gateway:
            try:
                gateway_address = ipaddress.IPv4Address(self.default_gateway)
            except ValueError:
                pass  # malformed value; the field's own validator already reports it
            else:
                if gateway_address not in vlan_network:
                    raise ValidationError(
                        {"default_gateway": f"{self.default_gateway} is not within subnet {self.subnet}."}
                    )

        # bool(), not "is None": GenericIPAddressField.empty_strings_allowed is
        # False, so a blank form submission reaches this point as "" (only
        # converted to NULL by the DB layer at save() time, after clean() has
        # already run) — an "is None" check would silently miss that case and
        # let a mismatched pair fall through to a raw DB IntegrityError from
        # ``vlan_dhcp_range_start_end_together`` instead of this message.
        if bool(self.dhcp_range_start) != bool(self.dhcp_range_end):
            raise ValidationError("dhcp_range_start and dhcp_range_end must both be set or both left blank.")

        dhcp_start = dhcp_end = None
        if self.dhcp_range_start and self.dhcp_range_end:
            try:
                # Parsed into locals first, only assigned to dhcp_start/dhcp_end
                # together below — full_clean() still calls clean() even after
                # clean_fields() has already flagged one endpoint as malformed,
                # so a partial assignment here (e.g. dhcp_start set, dhcp_end
                # left None after the parse below raises) would let the later
                # ``dhcp_start <= ... <= dhcp_end`` comparisons crash with a
                # raw TypeError instead of clean_fields()'s ValidationError.
                parsed_start = ipaddress.IPv4Address(self.dhcp_range_start)
                parsed_end = ipaddress.IPv4Address(self.dhcp_range_end)
            except ValueError:
                pass  # malformed value; the field's own validator already reports it
            else:
                dhcp_start, dhcp_end = parsed_start, parsed_end
                if dhcp_start >= dhcp_end:
                    raise ValidationError(
                        {
                            "dhcp_range_end": (
                                f"{self.dhcp_range_end} must be greater than the start address "
                                f"({self.dhcp_range_start})."
                            )
                        }
                    )
                if dhcp_start not in vlan_network or dhcp_end not in vlan_network:
                    raise ValidationError(
                        {
                            "dhcp_range_start": (
                                f"{self.dhcp_range_start}-{self.dhcp_range_end} is not fully within "
                                f"subnet {self.subnet}."
                            )
                        }
                    )
                if dhcp_start <= vlan_network.network_address <= dhcp_end:
                    raise ValidationError(
                        {
                            "dhcp_range_start": (
                                f"range must not contain the network address "
                                f"({vlan_network.network_address})."
                            )
                        }
                    )
                if dhcp_start <= vlan_network.broadcast_address <= dhcp_end:
                    raise ValidationError(
                        {
                            "dhcp_range_end": (
                                f"range must not contain the broadcast address "
                                f"({vlan_network.broadcast_address})."
                            )
                        }
                    )
                if self.default_gateway:
                    try:
                        gateway_address = ipaddress.IPv4Address(self.default_gateway)
                    except ValueError:
                        pass
                    else:
                        if dhcp_start <= gateway_address <= dhcp_end:
                            raise ValidationError(
                                {
                                    "dhcp_range_start": (
                                        f"range must not contain the default gateway "
                                        f"({self.default_gateway})."
                                    )
                                }
                            )

        if self.pk is not None:
            for rack_range in self.rack_ranges.all():
                try:
                    validate_ipv4_cidr(rack_range.address_range)
                except ValidationError:
                    continue  # that range's own malformed value; its own clean() will report it
                range_network = ipaddress.IPv4Network(rack_range.address_range, strict=True)
                if not range_network.subnet_of(vlan_network):
                    raise ValidationError(
                        f"subnet {self.subnet} no longer contains {rack_range.rack}'s existing range "
                        f"({rack_range.address_range}) on this VLAN; update or remove that range first."
                    )
                if (
                    dhcp_start is not None
                    and self.dhcp_range_start is not None
                    and self.dhcp_range_end is not None
                    and dhcp_range_overlaps_cidr(
                        self.dhcp_range_start, self.dhcp_range_end, rack_range.address_range
                    )
                ):
                    raise ValidationError(
                        {
                            "dhcp_range_start": (
                                f"{self.dhcp_range_start}-{self.dhcp_range_end} overlaps "
                                f"{rack_range.rack}'s existing range ({rack_range.address_range})."
                            )
                        }
                    )
            # Static assignments are allowed even without a RackVlanRange (they
            # only need to fit the VLAN's own subnet in that case), so a
            # subnet edit has to be checked against those directly too, not
            # just against rack ranges.
            for switch_address in self.switch_addresses.all():
                try:
                    address_obj = ipaddress.IPv4Address(switch_address.address)
                except ValueError:
                    continue
                if address_obj not in vlan_network:
                    raise ValidationError(
                        f"subnet {self.subnet} no longer contains {switch_address.switch}'s existing "
                        f"address ({switch_address.address}) on this VLAN; update or remove it first."
                    )
                if dhcp_start is not None and dhcp_end is not None and dhcp_start <= address_obj <= dhcp_end:
                    raise ValidationError(
                        f"DHCP range {self.dhcp_range_start}-{self.dhcp_range_end} would newly contain "
                        f"{switch_address.switch}'s existing address ({switch_address.address}) on this "
                        "VLAN; update or remove it first."
                    )
            for device_port in self.device_ports.filter(address__isnull=False):
                try:
                    address_obj = ipaddress.IPv4Address(device_port.address)
                except ValueError:
                    continue
                if address_obj not in vlan_network:
                    raise ValidationError(
                        f"subnet {self.subnet} no longer contains {device_port.device}'s existing "
                        f"address ({device_port.address}) on this VLAN; update or remove it first."
                    )
                if dhcp_start is not None and dhcp_end is not None and dhcp_start <= address_obj <= dhcp_end:
                    raise ValidationError(
                        f"DHCP range {self.dhcp_range_start}-{self.dhcp_range_end} would newly contain "
                        f"{device_port.device}'s existing address ({device_port.address}) on this VLAN; "
                        "update or remove it first."
                    )

    def _reject_blanking_subnet_with_dependents(self) -> None:
        """Block clearing ``subnet`` on a VLAN that still has addressing.

        An L2-only VLAN may not have a ``RackVlanRange``, a switch address,
        or a static device port (ADR 0012), and every *creation* path already
        rejects those. Without this, the one way into that forbidden state is
        to address a VLAN normally and then blank its subnet afterward —
        stranding rows that could never be re-saved, and that would hard-fail
        the next time their equipment moved rack.
        """
        rack_range = self.rack_ranges.select_related("rack").first()
        if rack_range is not None:
            raise ValidationError(
                {
                    "subnet": (
                        f"cannot be cleared: {rack_range.rack} still has an address range "
                        f"({rack_range.address_range}) on this VLAN. Remove it first — a VLAN "
                        "with no subnet is L2-only and cannot carry addressing (ADR 0012)."
                    )
                }
            )
        switch_address = self.switch_addresses.select_related("switch").first()
        if switch_address is not None:
            raise ValidationError(
                {
                    "subnet": (
                        f"cannot be cleared: {switch_address.switch} still has a static address "
                        f"({switch_address.address}) on this VLAN. Remove it first — a VLAN with "
                        "no subnet is L2-only and cannot carry addressing (ADR 0012)."
                    )
                }
            )
        device_port = self.device_ports.filter(address__isnull=False).select_related("device").first()
        if device_port is not None:
            raise ValidationError(
                {
                    "subnet": (
                        f"cannot be cleared: {device_port.device} still has a static address "
                        f"({device_port.address}) on this VLAN. Remove it or set the port to DHCP "
                        "first — a VLAN with no subnet is L2-only and cannot carry addressing "
                        "(ADR 0012)."
                    )
                }
            )
        template_link = self.rack_template_links.select_related("template").first()
        if template_link is not None:
            raise ValidationError(
                {
                    "subnet": (
                        f"cannot be cleared: {template_link.template} still includes this VLAN. "
                        "Remove it from the template first — a VLAN with no subnet is L2-only and "
                        "cannot carry addressing (ADR 0012)."
                    )
                }
            )


class RackTemplate(AuditedModel):
    """A named, reusable set of VLANs (ADR 0014) that seeds a new Rack's
    ``RackVlanRange`` rows in one step at creation.

    Seed-once, not live-referenced (ADR 0010's pattern, not ADR 0012's):
    applying a template copies its current VLAN list into real rows at that
    moment. Editing a template afterward — or deleting it entirely — has no
    effect on any rack already created from it, because a Rack keeps no
    reference back to its template (decision 5). That also means, unlike
    ``SwitchPortVlanProfile``, nothing here ever locks: there is no "in use"
    state for a template to fall into, so it stays freely deletable and
    freely editable for its entire life (decision 4).
    """

    name = models.CharField(max_length=100, unique=True)
    slot_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text="Optional. Supplies the rack-creation form's slot count when left blank there "
        "— type a different value on that form to override it.",
    )
    vlans: models.ManyToManyField = models.ManyToManyField(
        VLAN,
        through="RackTemplateVlan",
        related_name="+",
        blank=True,
        help_text="VLANs to allocate a rack address range for when a rack is created from this "
        "template. A VLAN with no subnet (L2-only, ADR 0012) cannot be included.",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~models.Q(name=""), name="racktemplate_name_not_blank"),
            models.CheckConstraint(
                condition=models.Q(slot_count__isnull=True) | models.Q(slot_count__gte=1),
                name="racktemplate_slot_count_gte_1",
            ),
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        # Never evaluates a queryset — this renders in selectors, list
        # columns, and every ValidationError message that names a template.
        return self.name

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        # Stripped here too, not just clean() — Model.save() never calls
        # clean(), so a direct RackTemplate.objects.create(name="Foo ")
        # would otherwise bypass the strip and persist trailing whitespace
        # the DB's case-insensitive collation doesn't also fold away.
        if self.name:
            self.name = self.name.strip()
        super().save(
            force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
        )

    def clean(self) -> None:
        super().clean()
        if self.name:
            self.name = self.name.strip()


class RackTemplateVlan(AuditedModel):
    """Explicit through model for ``RackTemplate.vlans`` (decision 2).

    A plain M2M's auto-generated join table has no ``on_delete`` to set, so
    it can't protect a VLAN from removal (ADR 0007) — this explicit model
    gives the ``vlan`` side a real ``PROTECT`` FK, the same reason
    ``SwitchPortVlanProfileAllowedVlan`` exists. Unlike that through model,
    ``template`` itself is ``CASCADE``: a Rack Template is freely deletable
    (decision 4 — nothing ever references it once a rack exists), so its
    membership rows should simply disappear with it rather than blocking
    the delete.

    Direct creation of a row here bypasses ``.add()``/``.set()`` entirely,
    so it never fires ``m2m_changed`` — ``clean()``/``save()`` re-validate
    the L2-only rule for that path. ``.add()``/``.set()`` write the through
    table without ever calling this model's ``save()`` (Django's documented
    behavior for custom-through M2M managers), so they're covered instead
    by the ``m2m_changed`` receiver below — the same split
    ``SwitchPortVlanProfileAllowedVlan`` has for the same reason.
    """

    template = models.ForeignKey(RackTemplate, on_delete=models.CASCADE, related_name="vlan_links")
    vlan = models.ForeignKey(VLAN, on_delete=models.PROTECT, related_name="rack_template_links")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["template", "vlan"], name="unique_rack_template_vlan"),
        ]

    def __str__(self) -> str:
        return f"{self.template} includes {self.vlan}"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        self._validate_vlan_has_subnet()
        super().save(
            force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
        )

    def clean(self) -> None:
        super().clean()
        self._validate_vlan_has_subnet()

    def _validate_vlan_has_subnet(self) -> None:
        vlan = _get_related(self, "vlan")
        if vlan is not None and not vlan.subnet:
            raise ValidationError(
                {
                    "vlan": (
                        f"{vlan} has no subnet (L2-only, ADR 0012) — a Rack Template needs a VLAN "
                        "with tracked addressing."
                    )
                }
            )


def _validate_rack_template_vlan_change(
    sender: type[models.Model],
    instance: "RackTemplate",
    action: str,
    pk_set: "set[int] | None",
    reverse: bool,
    **kwargs: Any,
) -> None:
    """Closes the gap ``RackTemplateVlan.clean()``/``save()`` can't:
    ``template.vlans.add()``/``.set()`` write the through table directly,
    without ever calling ``RackTemplateVlan.save()`` — the same reason
    ``SwitchPortVlanProfile`` needs ``_validate_profile_allowed_vlans_change``
    (see that function's docstring). ``m2m_changed`` is the only hook Django
    offers for those calls.

    Queries the VLANs being added fresh by ``pk_set`` rather than trusting
    any cached objects on ``instance`` — inherently non-stale, unlike the
    profile receiver's scalar re-read, since there's no ``instance``-level
    state to go stale here in the first place.

    ``related_name="+"`` on ``vlans`` means there is no reverse accessor, so
    ``reverse=True`` can never actually fire here — the guard is defensive,
    not load-bearing (same note as the profile's receiver).
    """
    if reverse or action != "pre_add" or not pk_set:
        return
    l2_only = VLAN.objects.filter(pk__in=pk_set, subnet="").values_list("name", "vlan_id")
    if l2_only.exists():
        names = ", ".join(f"{name} (VLAN {vlan_id})" for name, vlan_id in l2_only)
        raise ValidationError(
            f"{instance} cannot include {names} — a VLAN with no subnet is L2-only (ADR 0012) "
            "and a Rack Template needs VLANs with tracked addressing."
        )


models.signals.m2m_changed.connect(_validate_rack_template_vlan_change, sender=RackTemplate.vlans.through)


def _vlan_alignment_input(vlan: "VLAN") -> "tuple[str, list[str], tuple[str, str] | None] | None":
    """``(subnet, used_ranges, dhcp_range)`` for ``suggest_aligned_offset()``
    — built the same way ``Rack._check_template_application_possible()``
    already builds it for its own per-VLAN first-fit pre-flight, so every
    ADR 0025 allocation path (the template, the sticky single-range case,
    and the inline formset) reads "free" identically. ``None`` when
    ``vlan``'s own subnet is malformed — nothing to align against.

    Malformed sibling ``RackVlanRange``/``VLAN`` data (a bare ``save()``
    that bypassed ``clean()``) is skipped rather than raised — that
    sibling's own ``clean()`` is what reports it, not this helper's job.
    """
    try:
        validate_ipv4_cidr(vlan.subnet)
    except ValidationError:
        return None
    used_ranges = []
    for value in vlan.rack_ranges.values_list("address_range", flat=True):
        try:
            validate_ipv4_cidr(value)
        except ValidationError:
            continue
        used_ranges.append(value)
    dhcp_range = None
    if vlan.dhcp_range_start and vlan.dhcp_range_end:
        try:
            ipaddress.IPv4Address(vlan.dhcp_range_start)
            ipaddress.IPv4Address(vlan.dhcp_range_end)
        except ValueError:
            pass  # VLAN's own malformed dhcp range; its own clean() reports it
        else:
            dhcp_range = (vlan.dhcp_range_start, vlan.dhcp_range_end)
    return (vlan.subnet, used_ranges, dhcp_range)


def _candidate_range_is_free(
    candidate_cidr: str, subnet: str, used_ranges: "list[str]", dhcp_range: "tuple[str, str] | None"
) -> bool:
    """Whether ``candidate_cidr`` — one already-known block, not a search
    — sits inside ``subnet`` and overlaps neither ``used_ranges`` nor
    ``dhcp_range``. Used only by the sticky rule (``RackVlanRange.
    clean()``) to test a rack's established offset against a VLAN it
    hasn't allocated on yet, as distinct from ``iter_free_offsets()``/
    ``suggest_aligned_offset()``'s searches over many candidates.
    """
    vlan_network = ipaddress.IPv4Network(subnet, strict=True)
    candidate_network = ipaddress.IPv4Network(candidate_cidr, strict=True)
    if not candidate_network.subnet_of(vlan_network):
        return False
    if any(ranges_overlap(candidate_cidr, other) for other in used_ranges):
        return False
    return not (
        dhcp_range is not None and dhcp_range_overlaps_cidr(dhcp_range[0], dhcp_range[1], candidate_cidr)
    )


def _format_allocation(address_range: str, subnet: str) -> str:
    """ "<address_range> (offset <n>)" — the vocabulary every ADR 0025
    advisory that names an allocation outcome uses (Codex review round 2,
    finding 3). Decision 3 and the whole ADR are about the offset from a
    VLAN's *network address*, deliberately, because a third-octet or a bare
    CIDR reading doesn't survive a VLAN that isn't a /21 — but the CIDR
    is what an operator will actually type into the next field, so both
    are reported rather than either alone: ``10.201.1.32/27 (offset
    288)``, not just one or the other. Falls back to the bare CIDR if the
    offset can't be computed (a malformed value reaching this point is
    already a different problem for something else to report, not this
    helper's).
    """
    try:
        offset = range_offset(subnet, address_range)
    except ValueError:
        return address_range
    return f"{address_range} (offset {offset})"


def _rack_established_offset(rack: "Rack") -> "tuple[int | None, bool]":
    """``(offset, disagreed)`` — the single offset every one of ``rack``'s
    *existing* ``RackVlanRange`` rows agrees on, for the sticky rule (ADR
    0025 decision 1) to extend to a new VLAN. ``offset`` is ``None`` when
    there is no single agreed value; ``disagreed`` distinguishes *why*,
    since the two cases get different advisory wording (ADR 0025 plan,
    "Advisory surfacing"): ``True`` only when at least two distinct valid
    offsets exist among the rack's ranges (a genuinely misaligned rack),
    ``False`` when there are zero or one — nothing to disagree about, most
    commonly a rack that has no ranges yet at all, which isn't a
    divergence, just a rack with nothing established to be sticky about.

    Never guesses (decision 2): a range whose own value or VLAN doesn't
    parse is skipped exactly like ``_vlan_alignment_input()`` skips a
    malformed sibling, rather than treated as a disagreement.
    """
    offsets = set()
    for sibling in rack.vlan_ranges.all().select_related("vlan"):
        try:
            offsets.add(range_offset(sibling.vlan.subnet, sibling.address_range))
        except ValueError:
            continue  # malformed vlan.subnet or address_range; not this check's job
    if len(offsets) == 1:
        return offsets.pop(), False
    return None, len(offsets) > 1


class Rack(AuditedModel):
    """An abstract grouping of equipment with a fixed slot count.

    A slot is an addressing ordinal (base address + slot number), not a
    physical rack-unit position — physical RU height/placement is
    deliberately not modeled (CONTEXT.md).

    Has no "purpose" field by design — a spare rack is an ordinary Rack
    whose slots happen to hold spare equipment, and a rack created from a
    Rack Template (ADR 0014) is likewise just an ordinary Rack the moment
    it exists (CONTEXT.md).
    """

    name = models.CharField(max_length=100)
    slot_count = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    owner = models.ForeignKey(
        Owner,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="racks",
        help_text=(
            "Optional. Defaults a racked item's own Owner at creation (ADR 0023) — a suggestion, "
            "not inheritance; moving this rack to a different owner never touches equipment "
            "already in it."
        ),
    )
    location_slug = models.CharField(
        max_length=63,
        null=True,
        blank=True,
        unique=True,
        validators=[validate_dns_label],
        help_text=(
            'Optional, DNS-safe location name used in hostnames, e.g. "wpcsrl". Blank means '
            "this rack contributes no location component — not that it's virtual: AVIO and "
            "SPARE both carry one, only CONSOLES doesn't (ADR 0023)."
        ),
    )

    #: Class-level default for the ``template`` property below — never a
    #: plain class attribute, since Django's ``Model.__init__`` only
    #: accepts unknown kwargs (``objects.create(template=...)``) when the
    #: name is a field or a property (mirrors ADR 0013's
    #: ``NetworkDevice._port_addressing``/``port_addressing``).
    _template: "RackTemplate | None" = None

    #: Stashed by ``RackVlanRangeInlineFormSet.clean()`` (ADR 0025) when a
    #: rack is created with a template *and* manually-entered inline
    #: blanks on the same submission — the one joint offset computed
    #: across both, so ``_apply_template()`` doesn't independently
    #: recompute its own offset and hand the inline rows something
    #: different (the "ordering trap": ``all_valid(formsets)`` runs before
    #: ``save_model()``, so the template's rows don't exist yet when the
    #: formset's own ``clean()`` runs). Same never-a-plain-class-attribute
    #: reasoning as ``_template`` above.
    _aligned_offset: "int | None" = None

    #: Set alongside ``_aligned_offset`` above, unconditionally, whenever
    #: ``RackVlanRangeInlineFormSet._align_offsets()`` runs (Codex review
    #: finding 2) — distinguishes "the formset never ran, so
    #: ``_resolve_template_offset()`` should compute its own search
    #: restricted to the template's VLANs" from "the formset ran a joint
    #: search over the *union* of the template's VLANs and its own blank
    #: rows, and that search failed." Collapsing those two into one
    #: ``_aligned_offset is None`` check would let ``_apply_template()``
    #: quietly fall back to a *narrower* search over just the template's
    #: own subset after the wider one already failed — exactly the "a
    #: subset gets silently aligned while the rest goes independent"
    #: outcome decisions 2 and 3 forbid. ``False`` (the default) only for
    #: a programmatic ``Rack.objects.create(template=...)`` call with no
    #: admin form/formset involved at all, where there is no "union" to
    #: have tried and failed in the first place.
    _aligned_offset_attempted: bool = False

    #: True only while ``_apply_template()`` is actively looping over its
    #: own template's VLANs. Read by ``RackVlanRange.clean()``'s sticky
    #: rule (ADR 0025 decision 1) to suppress itself: by the time
    #: ``_apply_template()`` runs, ``self.pk`` is already set (it runs from
    #: inside ``Rack.save()``, after the row is inserted), so without this
    #: flag the sticky rule would fire on the *second* VLAN of a
    #: fallback-to-first-fit template application, "establishing" an
    #: offset from the *first* VLAN's just-saved fallback range — one
    #: rack-level batch decision producing a cascade of per-VLAN sticky
    #: re-decisions, and a duplicate advisory for what is conceptually one
    #: event. The sticky rule stays fully live for its real purpose: a
    #: genuinely later, independent addition to an already-existing rack.
    #: Same never-a-plain-class-attribute reasoning as ``_template`` above.
    _applying_template: bool = False

    #: Class-level default for the ``_range_alignment_advisories``
    #: property below — same reasoning as ``_template`` above: a shared
    #: mutable list would leak advisories across every Rack instance.
    _range_alignment_advisories_store: "list[str] | None" = None

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(condition=models.Q(slot_count__gte=1), name="rack_slot_count_gte_1"),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        # Stripped/lowercased/blank-to-None here too, not just clean() —
        # Model.save() never calls clean() (Department.save()'s reasoning),
        # so a direct Rack.objects.create(location_slug="  ") would
        # otherwise persist an unstripped value instead of None.
        self.location_slug = self._normalize_location_slug(self.location_slug)
        # ``self.pk is None or self._state.adding`` — see NetworkSwitch.save()
        # for why neither check alone is sufficient.
        is_new = self.pk is None or self._state.adding
        with transaction.atomic():
            super().save(
                force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
            )
            if is_new and self._template is not None:
                self._apply_template()

    @staticmethod
    def _normalize_location_slug(value: str | None) -> str | None:
        # "" -> None (settled decision 5): null=True + unique=True is what
        # MySQL enforces uniqueness on, and MySQL permits unlimited NULLs
        # in a unique index — "" would collide with itself the moment a
        # second blank rack were saved.
        if value is None:
            return None
        value = value.strip().lower()
        return value or None

    def clean_fields(self, exclude=None) -> None:
        # Normalize *before* the field validators run — see
        # Owner.clean_fields()/NetworkDeviceTypePort.clean_fields() for why
        # this can't wait for clean().
        self.location_slug = self._normalize_location_slug(self.location_slug)
        super().clean_fields(exclude=exclude)

    def clean(self) -> None:
        super().clean()
        self.location_slug = self._normalize_location_slug(self.location_slug)
        if self.pk is None:
            # Advisory only — _apply_template() re-checks against the same
            # snapshot inside save()'s transaction, which is the real
            # guarantee. A second, independent read here could in
            # principle observe different template membership than the
            # apply actually uses: this project runs READ COMMITTED, not
            # REPEATABLE READ (verified against the app's own connection —
            # Django's MySQL backend sets isolation per connection,
            # defaulting to read committed, regardless of the MariaDB
            # server's global setting). That's fine for an advisory check,
            # but not for the guarantee itself — see _apply_template().
            if self._template is not None and self.slot_count is not None:
                links = list(self._template.vlan_links.select_related("vlan").order_by("vlan__vlan_id"))
                self._check_template_application_possible(links)
            return  # nothing else assigned yet on a not-yet-created rack
        if self.switches.filter(rack_slot__gt=self.slot_count).exists():
            raise ValidationError(
                {"slot_count": f"{self.slot_count} is smaller than the rack_slot of a switch assigned here."}
            )
        if self.devices.filter(rack_slot__gt=self.slot_count).exists():
            raise ValidationError(
                {"slot_count": f"{self.slot_count} is smaller than the rack_slot of a device assigned here."}
            )
        for rack_range in self.vlan_ranges.all():
            try:
                validate_ipv4_cidr(rack_range.address_range)
            except ValidationError:
                continue  # that range's own clean() will report its own malformed value
            range_network = ipaddress.IPv4Network(rack_range.address_range, strict=True)
            if range_network.num_addresses < required_block_size(self.slot_count):
                raise ValidationError(
                    {
                        "slot_count": (
                            f"{self.slot_count} no longer fits the existing {rack_range.address_range} "
                            f"range on {rack_range.vlan}; update or remove that range first."
                        )
                    }
                )

    @property
    def template(self) -> "RackTemplate | None":
        """Creation-time-only Rack Template to seed this rack's
        ``RackVlanRange`` rows from (ADR 0014). Never stored — the
        materialized ranges are the only record of what was applied;
        setting this after creation has no effect, since
        ``_apply_template()`` only runs once. A rack keeps no reference to
        its template by design (decision 5), so this is deliberately never
        a field.
        """
        return self._template

    @template.setter
    def template(self, value: "RackTemplate | None") -> None:
        if value is not None and not isinstance(value, RackTemplate):
            raise ValidationError(f"{value!r} is not a RackTemplate.")
        self._template = value

    @property
    def _range_alignment_advisories(self) -> "list[str]":
        """Advisories recorded by any of ADR 0025's three allocation paths
        — the fallback-to-first-fit case below, the sticky rule in
        ``RackVlanRange.clean()``, and the inline formset's joint offset
        (``RackVlanRangeInlineFormSet.clean()``) — all keyed off this one
        rack instance so ``RackAdmin.save_model()`` can emit whatever
        accumulated during that request's validation, via
        ``messages.info``, the same pattern ``_emit_hostname_advisories()``
        already uses for phase 18's advisories. Read directly by any
        programmatic caller too — this is a plain instance attribute, not
        admin-only plumbing.
        """
        if self._range_alignment_advisories_store is None:
            self._range_alignment_advisories_store = []
        return self._range_alignment_advisories_store

    @property
    def range_offsets_diverge(self) -> bool:
        """Stateless indicator (ADR 0025 decision 5), modelled directly on
        ``NetworkSwitch.hostname_diverges`` (ADR 0023 decision 9): ``True``
        when this rack's ``RackVlanRange`` rows don't all share one
        offset.

        Zero or one range is never divergent — there's nothing for it to
        disagree with. A rack with two or more ranges where **any**
        offset is ``None`` (a malformed stored value, an L2-only VLAN
        reachable only via a bypassed ``save()``, or a range genuinely
        outside its VLAN's subnet) counts as **diverging**: it cannot be
        shown to be aligned, and silently reporting "aligned" for data
        this property can't read would be worse than a false positive
        (ADR 0025 plan, review note 3).

        No extra query when the caller has already prefetched
        ``vlan_ranges__vlan`` — same no-extra-queries posture as
        ``hostname_diverges``: this reads only ``self.vlan_ranges.all()``
        (satisfied from the prefetch cache when present) and each range's
        own ``.offset`` property, which needs only the already-
        ``select_related``d ``vlan``.
        """
        ranges = list(self.vlan_ranges.all())
        if len(ranges) < 2:
            return False
        offsets = {rack_range.offset for rack_range in ranges}
        return None in offsets or len(offsets) > 1

    def _resolve_template_offset(self, links: "list[RackTemplateVlan]") -> int | None:
        """The joint offset ``_apply_template()`` should use across
        ``links``' VLANs (ADR 0025 decision 1's "batch" half).

        ``self._aligned_offset_attempted`` (Codex review finding 2) is the
        load-bearing branch here: when it's ``True``,
        ``RackVlanRangeInlineFormSet._align_offsets()`` already ran one
        joint search over the *union* of the template's VLANs and its own
        blank rows, and this method must not run a *second*, narrower
        search restricted to just the template's own subset — using
        ``self._aligned_offset`` if that union search succeeded and is
        still free here, otherwise ``None``, full stop. Silently
        recomputing over the subset after the union failed is exactly the
        bug this branch exists to prevent: template VLANs A and B could
        agree with each other on an offset that the union (including a
        third, inline VLAN C) had already rejected — decisions 2 and 3
        say that when nothing is common to everything being allocated,
        *everything* falls back independently, not that a subset gets
        quietly aligned while the rest goes it alone.

        Only when ``_aligned_offset_attempted`` is ``False`` — no formset
        ran at all, i.e. a programmatic ``Rack.objects.create(template=
        ...)`` call with no admin form involved — does this method run
        its own fresh search, restricted to exactly this template's VLANs
        (decision 6: never the whole VLAN set), because in that case
        there is no wider union that could have already failed.
        """
        vlans_input = []
        for link in links:
            info = _vlan_alignment_input(link.vlan)
            if info is None:
                # A malformed subnet here would already have failed
                # _check_template_application_possible() (called just
                # before this), so this is unreached in practice — kept
                # defensive rather than assumed, matching this module's
                # general posture toward sibling data.
                return None
            vlans_input.append(info)
        if not vlans_input:
            return None
        if self._aligned_offset_attempted:
            stashed = self._aligned_offset
            if stashed is None:
                return None  # the formset's own union search already failed; never narrow it
            try:
                candidates = [
                    range_at_offset(subnet, stashed, self.slot_count) for subnet, _, _ in vlans_input
                ]
            except ValueError:
                return None
            if all(
                _candidate_range_is_free(cidr, subnet, used, dhcp)
                for cidr, (subnet, used, dhcp) in zip(candidates, vlans_input, strict=True)
            ):
                return stashed
            return None
        return suggest_aligned_offset(vlans_input, self.slot_count)

    def _apply_template(self) -> None:
        """One-time copy of ``self.template``'s VLAN list into real
        ``RackVlanRange`` rows (ADR 0014 decision 7). Each range is built
        unsaved and put through ``full_clean()`` before ``save()`` —
        ``RackVlanRange`` has no ``save()`` override, so a bare
        ``objects.create()`` would otherwise persist an empty string on a
        NOT NULL column instead of triggering ``RackVlanRange.clean()``'s
        existing suggestion logic. Runs inside the same transaction as
        this rack's insert (see ``save()``), so any failure rolls back the
        rack and every range materialized before it (decision 8's
        all-or-nothing).

        Reads the template's VLAN links exactly once into ``links`` and
        reuses that same snapshot for both the pre-flight and this loop,
        rather than querying twice — this project runs READ COMMITTED (see
        ``clean()``), so two independent reads inside this one transaction
        could observe different membership if a concurrent template edit
        landed in between. No row lock on the template: that would only
        narrow this specific torn-read window, not ADR 0014's already-
        accepted range-allocation race (see that ADR's Known-gap section),
        so a snapshot is the right amount of correctness for what it costs.

        ADR 0025: a single joint offset (``_resolve_template_offset()``)
        is computed once across every listed VLAN. On a hit, each range is
        built with an explicit ``address_range`` — skipping the per-row
        suggester while still passing through ``full_clean()``/
        ``_validate_range()`` — so the suggester never independently
        re-decides a VLAN this method has already placed. On a miss, every
        range is built blank exactly as before ADR 0025, and one advisory
        names which VLAN landed on which offset (decision 3 — the outcome
        is reported, not blamed on a VLAN, since no single VLAN "caused"
        an empty intersection of free offsets). ``_resolve_template_offset()``
        never narrows that miss to a fresh search over just this
        template's own subset when a wider search already ran and failed
        (``self._aligned_offset_attempted`` — Codex review finding 2; see
        that method's own docstring) — a submission combining a template
        with inline VLANs can therefore end up with *two* advisories, this
        one (about the template's own rows) and the inline formset's
        (about its own blank rows), since neither can see the other's
        VLANs at the point it has to report.

        ``self._applying_template`` brackets the loop below so
        ``RackVlanRange.clean()``'s sticky rule stays quiet for its
        duration — without it, the *second* blank range in a fallback
        would see the *first* one (already saved, since ``self.pk`` is set
        the moment this method runs) as an "established" rack offset and
        opportunistically adopt or reject it on its own, turning one
        rack-level batch decision into a cascade of per-VLAN sticky
        re-decisions with a duplicate advisory riding along.
        """
        template = self._template
        assert template is not None  # only ever called from save() after that same check
        links = list(template.vlan_links.select_related("vlan").order_by("vlan__vlan_id"))
        self._check_template_application_possible(links)
        offset = self._resolve_template_offset(links) if links else None
        fallback_details = []
        self._applying_template = True
        try:
            for link in links:
                if offset is not None:
                    address_range = range_at_offset(link.vlan.subnet, offset, self.slot_count)
                else:
                    address_range = ""
                rng = RackVlanRange(
                    rack=self, vlan=link.vlan, address_range=address_range, created_by=self.created_by
                )
                rng.full_clean()
                rng.save()
                if offset is None:
                    fallback_details.append(
                        f"{link.vlan}: {_format_allocation(rng.address_range, link.vlan.subnet)}"
                    )
        finally:
            self._applying_template = False
        if offset is None and fallback_details:
            self._range_alignment_advisories.append(
                "No single offset is free on every VLAN this rack is being given a range on — "
                "allocated per VLAN instead of jointly: " + "; ".join(fallback_details)
            )

    def _check_template_application_possible(self, links: "list[RackTemplateVlan]") -> None:
        """Pure pre-flight over a snapshot of a template's VLAN links:
        whether each listed VLAN can allocate a ``slot_count``-sized block
        right now. Pure and independent of ``self.pk``, so it can run from
        both ``clean()`` (admin form errors, its own freshly-read snapshot)
        and the top of ``_apply_template()`` (the same snapshot that
        materialization then uses).

        Collects every VLAN that can't be allocated rather than stopping at
        the first, so the operator sees the whole picture in one pass —
        mirrors ADR 0013's ``_check_static_materialization_possible()``,
        which names both conflicting ports rather than just one.
        """
        failures = []
        for link in links:
            vlan = link.vlan
            try:
                validate_ipv4_cidr(vlan.subnet)
            except ValidationError:
                failures.append(f"{vlan} (invalid subnet)")
                continue
            used_ranges = []
            for value in vlan.rack_ranges.values_list("address_range", flat=True):
                try:
                    validate_ipv4_cidr(value)
                except ValidationError:
                    continue  # sibling range's own malformed value; not this check's job
                used_ranges.append(value)
            dhcp_range = None
            if vlan.dhcp_range_start and vlan.dhcp_range_end:
                try:
                    ipaddress.IPv4Address(vlan.dhcp_range_start)
                    ipaddress.IPv4Address(vlan.dhcp_range_end)
                except ValueError:
                    pass  # VLAN's own malformed dhcp range; its own clean() reports it
                else:
                    dhcp_range = (vlan.dhcp_range_start, vlan.dhcp_range_end)
            suggestion = suggest_rack_vlan_range(vlan.subnet, self.slot_count, used_ranges, dhcp_range)
            if suggestion is None:
                failures.append(str(vlan))
        if failures:
            # Keyed "template" — not a real model field, but safe here
            # unlike NetworkDevice._check_static_materialization_possible()'s
            # analogous re-raise-as-plain-error workaround: this method only
            # ever runs when self._template is set, which itself only
            # happens via RackAddForm (which always declares a "template"
            # form field) or a programmatic caller with no ModelForm
            # involved at all — so there's no call site where "template"
            # could fail to match a form field and crash Django's
            # add_error().
            raise ValidationError(
                {
                    "template": (
                        f"No free address block sized for slot_count={self.slot_count} on: "
                        f"{', '.join(failures)}. Resize the rack, free up space on those VLANs, or "
                        "remove them from the template before applying it."
                    )
                }
            )


class RackVlanRange(AuditedModel):
    """A Rack's reserved IPv4 address range on one VLAN.

    Manually assigned per ADR 0001 — the system suggests the next free
    block, but the range does not recompute automatically once set.
    """

    rack = models.ForeignKey(Rack, on_delete=models.CASCADE, related_name="vlan_ranges")
    vlan = models.ForeignKey(VLAN, on_delete=models.PROTECT, related_name="rack_ranges")
    address_range = models.CharField(
        max_length=18,
        blank=True,
        validators=[validate_ipv4_cidr],
        help_text="Leave blank to suggest the next free block sized for the rack's slot count.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["rack", "vlan"], name="unique_rack_vlan_range"),
        ]
        ordering = ["vlan", "address_range"]

    def __str__(self) -> str:
        return f"{self.rack} / {self.vlan}: {self.address_range}"

    @property
    def offset(self) -> int | None:
        """This range's offset from its VLAN's network address (ADR 0025)
        — the tolerant counterpart to the strict ``range_offset()`` pure
        function. ``None``, never a raised exception, when the VLAN has
        no subnet (L2-only, ADR 0012), either value is malformed (a bare
        ``save()`` bypasses ``clean()`` — ``RobustnessTests`` exists
        precisely because the write path allows this), or the range sits
        outside its own VLAN's subnet — a range this far wrong can't be
        shown to have *an* offset, honestly.

        This is how the offset reaches every read-only surface without a
        view annotation or a template filter: rack_detail's columns and
        ``Rack.range_offsets_diverge`` are both ``RackVlanRange`` objects
        with ``vlan`` already ``select_related``, which is everything this
        property needs.
        """
        vlan = _get_related(self, "vlan")
        if vlan is None or not vlan.subnet:
            return None
        try:
            validate_ipv4_cidr(vlan.subnet)
            validate_ipv4_cidr(self.address_range)
        except ValidationError:
            return None
        try:
            vlan_network = ipaddress.IPv4Network(vlan.subnet, strict=True)
            range_network = ipaddress.IPv4Network(self.address_range, strict=True)
        except ValueError:
            return None
        if not range_network.subnet_of(vlan_network):
            return None
        return range_offset(vlan.subnet, self.address_range)

    def clean(self) -> None:
        super().clean()
        rack = _get_related(self, "rack")
        vlan = _get_related(self, "vlan")
        if vlan is not None and not vlan.subnet:
            raise ValidationError(
                {
                    "vlan": (
                        f"{vlan} has no subnet (L2-only, ADR 0012) — a rack range needs a VLAN "
                        "with tracked addressing."
                    )
                }
            )
        if self.pk is None and not self.address_range and rack is not None and vlan is not None:
            used_ranges = []
            for value in vlan.rack_ranges.exclude(pk=self.pk).values_list("address_range", flat=True):
                try:
                    validate_ipv4_cidr(value)
                except ValidationError:
                    continue  # that sibling range's own malformed value; its own clean() reports it
                used_ranges.append(value)
            dhcp_range = None
            if vlan.dhcp_range_start and vlan.dhcp_range_end:
                try:
                    ipaddress.IPv4Address(vlan.dhcp_range_start)
                    ipaddress.IPv4Address(vlan.dhcp_range_end)
                except ValueError:
                    pass  # VLAN's own malformed dhcp range; its own clean() reports it
                else:
                    dhcp_range = (vlan.dhcp_range_start, vlan.dhcp_range_end)
            try:
                validate_ipv4_cidr(vlan.subnet)
            except ValidationError:
                pass  # VLAN's own subnet is invalid; nothing sensible to suggest
            else:
                # ADR 0025 decision 1's sticky half: a blank range added to
                # an *already-existing* rack (rack.pk is not None — a rack
                # still being created has no saved siblings to be sticky
                # to, and is handled by _apply_template()/the inline
                # formset instead) adopts the rack's established offset
                # when one exists and a block at it is free here.
                # _rack_established_offset() returns (None, False) both
                # when the rack has no ranges yet at all (nothing to
                # inherit — not a divergence) and when its existing ranges
                # don't parse; it returns (None, True) only when they
                # genuinely disagree (decision 2 — never guess one).
                #
                # Also suppressed for the duration of rack._applying_template
                # (see that flag's docstring on Rack): _apply_template()'s
                # own fallback loop already makes one rack-level batch
                # decision, and without this guard the sticky rule would
                # opportunistically re-decide it VLAN by VLAN as each
                # fallback range gets saved, against siblings that only
                # exist because *this same* fallback is still in progress.
                suggestion = None
                established_offset: int | None = None
                disagreed = False
                if rack.pk is not None and not rack._applying_template:
                    established_offset, disagreed = _rack_established_offset(rack)
                    if established_offset is not None:
                        try:
                            candidate_cidr = range_at_offset(vlan.subnet, established_offset, rack.slot_count)
                        except ValueError:
                            candidate_cidr = None
                        if candidate_cidr is not None and _candidate_range_is_free(
                            candidate_cidr, vlan.subnet, used_ranges, dhcp_range
                        ):
                            suggestion = candidate_cidr
                if suggestion is None:
                    suggestion = suggest_rack_vlan_range(
                        vlan.subnet, rack.slot_count, used_ranges, dhcp_range
                    )
                    if suggestion and rack.pk is not None:
                        described = _format_allocation(suggestion, vlan.subnet)
                        if established_offset is not None:
                            rack._range_alignment_advisories.append(
                                f"{vlan}: {rack}'s established offset ({established_offset}) isn't "
                                f"free here — allocated {described} instead."
                            )
                        elif disagreed:
                            rack._range_alignment_advisories.append(
                                f"{vlan}: {rack}'s existing ranges don't all share one offset, so "
                                f"none could be inherited — allocated {described} by first-fit."
                            )
                if suggestion:
                    self.address_range = suggestion
        if not self.address_range:
            raise ValidationError(
                {
                    "address_range": (
                        "This field is required — no suggestion could be computed "
                        "automatically (check the VLAN's subnet is large enough for "
                        "this rack), so it must be entered manually."
                    )
                }
            )
        self._validate_range()

    def _validate_range(self) -> None:
        vlan = _get_related(self, "vlan")
        if vlan is None:
            return  # vlan wasn't set at all; clean_fields() already reports the missing-field error
        try:
            validate_ipv4_cidr(vlan.subnet)
        except ValidationError:
            return  # VLAN's own subnet is invalid; its own clean() will report that
        try:
            validate_ipv4_cidr(self.address_range)
        except ValidationError:
            return  # address_range itself is invalid; clean_fields() already reports it
        vlan_network = ipaddress.IPv4Network(vlan.subnet, strict=True)
        range_network = ipaddress.IPv4Network(self.address_range, strict=True)
        if not range_network.subnet_of(vlan_network):
            raise ValidationError(
                {"address_range": f"{self.address_range} is not within {vlan}'s subnet ({vlan.subnet})."}
            )
        rack = _get_related(self, "rack")
        if rack is not None and range_network.num_addresses < required_block_size(rack.slot_count):
            raise ValidationError(
                {
                    "address_range": (
                        f"{self.address_range} isn't big enough for {rack} (slot_count "
                        f"{rack.slot_count}): it needs {required_block_size(rack.slot_count)} "
                        "addresses (slots 1..slot_count, plus the block's own base and top addresses "
                        "reserved)."
                    )
                }
            )
        for other in vlan.rack_ranges.exclude(pk=self.pk):
            if ranges_overlap(self.address_range, other.address_range):
                raise ValidationError(
                    {
                        "address_range": (
                            f"{self.address_range} overlaps {other.rack}'s range "
                            f"{other.address_range} on {vlan}."
                        )
                    }
                )
        if vlan.dhcp_range_start and vlan.dhcp_range_end:
            try:
                ipaddress.IPv4Address(vlan.dhcp_range_start)
                ipaddress.IPv4Address(vlan.dhcp_range_end)
            except ValueError:
                pass  # VLAN's own malformed dhcp range; its own clean() reports it
            else:
                if dhcp_range_overlaps_cidr(vlan.dhcp_range_start, vlan.dhcp_range_end, self.address_range):
                    raise ValidationError(
                        {
                            "address_range": (
                                f"{self.address_range} overlaps {vlan}'s DHCP range "
                                f"({vlan.dhcp_range_start}-{vlan.dhcp_range_end})."
                            )
                        }
                    )
        # A range edit can leave already-assigned static addresses (switch or
        # device) for this rack, on this VLAN, outside the new block — block
        # the edit rather than silently orphaning them. Only meaningful once
        # the rack itself is saved (nothing can reference an unsaved rack yet).
        if rack is not None and rack.pk is not None:
            for switch_address in NetworkSwitchAddress.objects.filter(switch__rack=rack, vlan=vlan):
                try:
                    addr = ipaddress.IPv4Address(switch_address.address)
                except ValueError:
                    continue
                if addr not in range_network:
                    raise ValidationError(
                        {
                            "address_range": (
                                f"{self.address_range} would no longer contain {switch_address.switch}'s "
                                f"existing address ({switch_address.address}); update or remove it first."
                            )
                        }
                    )
            for device_port in NetworkDevicePort.objects.filter(
                device__rack=rack, vlan=vlan, address__isnull=False
            ):
                try:
                    addr = ipaddress.IPv4Address(device_port.address)
                except ValueError:
                    continue
                if addr not in range_network:
                    raise ValidationError(
                        {
                            "address_range": (
                                f"{self.address_range} would no longer contain {device_port.device}'s "
                                f"existing address ({device_port.address}); update or remove it first."
                            )
                        }
                    )


class RackSlotAssignmentMixin:
    """Shared ``clean()`` logic for equipment with a ``rack``/``rack_slot`` pair.

    A slot is 1-based; ``rack`` and ``rack_slot`` are all-or-neither; when both
    are set, ``rack_slot`` plus this occupant's ``slot_span`` must fall
    within the rack's ``slot_count`` — this last check is cross-table so it
    can't be expressed as a DB constraint.

    Also cross-checks the *other* equipment table so a switch and a device
    can't both claim an occupied ordinal — since a device claims the
    **set** of ordinals ``{rack_slot + offset}`` for each offset its type
    declares (ADR 0027 decision 2), not necessarily a contiguous run, this
    is a set-membership check, not merely a range-overlap or exact-match
    one; see ``_check_rack_slot_not_occupied()`` on each subclass. This is
    an interim, form/full_clean-time
    guard, not a concurrency-safe one — a shared rack-occupancy table would
    be needed to close the direct-ORM/race-condition gap. That's re-filed
    under ``ROADMAP.md``'s "Later / not yet designed" section (the "Rack
    *slot* occupancy has no DB-level overlap guarantee..." item) rather
    than any phase still in flight — this docstring used to point at
    phase 3's "Overlap validation" work, which shipped rack-range-vs-range
    and rack-range-vs-DHCP overlap, a different table and a different
    problem; that pointer was stale and this corrects it (ADR 0017).
    """

    rack: Rack | None
    rack_slot: int | None

    pk: int | None

    @property
    def slot_span(self) -> int:
        """Number of consecutive ordinals, starting at ``rack_slot``, this
        occupant claims. ``1`` for everything except ``NetworkDevice``,
        which overrides this to delegate to its type's ``slot_span`` (ADR
        0017 — a device whose type declares offset ports occupies more
        than its own ``rack_slot``).
        """
        return 1

    def clean(self) -> None:
        super().clean()  # type: ignore[misc]
        if (self.rack is None) != (self.rack_slot is None):
            raise ValidationError(
                "rack and rack_slot must both be set (racked) or both be empty (spare pool)."
            )
        if self.rack is not None and self.rack_slot is not None:
            span = self.slot_span
            if self.rack_slot + span - 1 > self.rack.slot_count:
                if span == 1:
                    raise ValidationError(
                        f"rack_slot {self.rack_slot} exceeds {self.rack}'s slot_count "
                        f"({self.rack.slot_count})."
                    )
                raise ValidationError(
                    f"rack_slot {self.rack_slot} plus this occupant's span ({span}, ending at "
                    f"ordinal {self.rack_slot + span - 1}) exceeds {self.rack}'s slot_count "
                    f"({self.rack.slot_count})."
                )
            self._check_rack_slot_not_occupied()
        if self.pk is not None:
            # Unracking or moving equipment that already has this-VLAN static
            # addresses can't be validated inside those address rows' own
            # clean() — they aren't part of this save — so re-check them here.
            self._validate_existing_addresses_still_fit()

    def _check_rack_slot_not_occupied(self) -> None:
        raise NotImplementedError

    def _validate_existing_addresses_still_fit(self) -> None:
        raise NotImplementedError


def _persisted_is_system(pk: int) -> bool:
    """The actually-persisted ``is_system`` value for profile ``pk``.

    Deliberately not ``self.is_system`` — that in-memory attribute can be
    set on an instance without ever being saved (``is_system`` is
    ``editable=False`` but still a plain Python attribute), which would
    otherwise let a caller unlock or delete the system profile just by
    assigning ``profile.is_system = False`` before calling ``save()``/
    ``delete()``. Callers that already hold this row's lock (``save()``/
    ``delete()``, both via ``_lock_profile_rows()``) get the guaranteed
    latest-committed value; ``clean()`` reads it unlocked, same tradeoff
    ``_check_scalar_fields_locked()`` itself documents for that path.
    """
    return bool(
        SwitchPortVlanProfile._default_manager.filter(pk=pk).values_list("is_system", flat=True).first()
    )


def _lock_profile_rows(*pks: int | None) -> None:
    """Acquire a row lock (``SELECT ... FOR UPDATE``) on the given
    ``SwitchPortVlanProfile`` rows — must run inside ``transaction.atomic()``.

    Mirrors ``_lock_type_rows()`` (same race, different table): without this,
    a profile edit/deletion reading "is this profile in use?" and a
    concurrent change to what references it (a new switch port, a profile
    swap on an existing port, or a switch's first materialization) can each
    independently observe a stale "not in use yet" state and both proceed.
    Kept as a separate helper rather than overloading ``_lock_type_rows()``
    since profiles aren't Types and the callers read more clearly named for
    what they're actually locking.
    """
    ids = sorted({pk for pk in pks if pk is not None})
    if ids:
        list(SwitchPortVlanProfile._default_manager.select_for_update().filter(pk__in=ids))


#: Locked once any real ``NetworkSwitchPort`` references the profile (not
#: merely a ``NetworkSwitchTypePort`` — see ``SwitchPortVlanProfile``).
_PROFILE_IN_USE_LOCKED_FIELDS: frozenset[str] = frozenset({"port_mode", "native_vlan"})

#: Locked permanently on the system default profile — a superset of the
#: in-use set because the default's ``all_vlans_allowed`` is also part of
#: its documented, fixed fallback behavior (DESIGN.md's "Switch Port
#: Profile VLAN Selection").
_PROFILE_SYSTEM_LOCKED_FIELDS: frozenset[str] = frozenset({"port_mode", "native_vlan", "all_vlans_allowed"})


class SwitchPortVlanProfileQuerySet(models.QuerySet):
    """Blocks bulk ``QuerySet.delete()`` on a profile that's the system
    default or still in use — mirrors ``NetworkSwitchTypePortQuerySet``
    (a per-instance ``delete()`` override alone doesn't stop Django's bulk
    SQL DELETE from bypassing it).
    """

    def delete(self):
        with transaction.atomic():
            ids = list(self.values_list("pk", flat=True))
            _lock_profile_rows(*ids)
            blocked = SwitchPortVlanProfile._default_manager.filter(pk__in=ids).filter(
                models.Q(is_system=True) | models.Q(type_ports__isnull=False) | models.Q(ports__isnull=False)
            )
            if blocked.exists():
                raise ValidationError(
                    "One or more selected profiles is the system default profile, or still in "
                    "use by a switch type port or switch port; remove those references first."
                )
            return super().delete()


class SwitchPortVlanProfile(AuditedModel):
    """A reusable, named bundle of switch port L2 config (DESIGN.md's
    "Switch Port VLAN Profiles" / "Switch Port Profile VLAN Selection").

    Referenced *live* by ``NetworkSwitchTypePort``/``NetworkSwitchPort`` —
    unlike the seed-once materialization pattern the rest of ADR 0010 uses,
    a port stores this profile's id, not a copy of its fields, so editing a
    profile's VLANs reaches every port that uses it immediately. This is a
    deliberate departure from ADR 0010's "materialize once, never re-sync"
    rule, made because the entire point of a named profile ("Audio Trunk",
    "Dante Primary") is to redefine what it means fleet-wide in one place —
    see ADR 0012.

    ``port_mode``/``native_vlan`` lock once any real ``NetworkSwitchPort``
    references this profile — not merely a ``NetworkSwitchTypePort``, since a
    profile still being wired into type definitions has no real port
    depending on its exact VLAN yet, mirroring ADR 0010's "locks once it has
    any instance". ``allowed_vlans``/``all_vlans_allowed`` stay editable even
    then: adding a tagged VLAN to a trunk that's already in use is this
    profile's whole reason to exist. ``name`` is never locked (cosmetic).

    The system default profile (``is_system=True``, seeded by migration —
    see ``default_switch_port_vlan_profile()``) locks all three scalar
    fields permanently, including ``all_vlans_allowed``, since it's the
    documented fallback every unselected type port lands on. ``is_system``
    itself is immutable after creation — enforced the same way every other
    locked field in this module is (``_check_locked_fields_unchanged``) — so
    it can't be flipped off as a way to unlock or delete this row.

    A fourth invariant — Access mode excludes ``all_vlans_allowed`` — is a
    pure scalar-vs-scalar rule with no M2M involved at all, so it's backed
    by a real DB ``CheckConstraint`` and checked plainly in ``clean()``/
    ``save()`` (``_validate_port_mode_excludes_all_vlans_allowed``).

    The other three ``allowed_vlans`` invariants (native VLAN not also
    listed as an allowed VLAN; no allowed VLANs while ``all_vlans_allowed``
    is set; no allowed VLANs in Access mode) can't be enforced that simply:
    a new profile has no pk yet (so its M2M manager can't be queried), and
    for an edited profile ``ModelForm.save_m2m()`` runs *after* ``save()``,
    so ``clean()`` would only ever see the previous links. They're enforced
    instead, each against the actual state that path can see: the admin
    form's ``clean()`` (submitted complete state — the only path that can
    also grant ``_trust_pending_m2m_from_form`` once it has proven that
    state sound, letting a combined scalar-and-M2M edit through); an
    ``m2m_changed`` receiver on ``allowed_vlans`` (``.add()``/``.set()``);
    ``SwitchPortVlanProfileAllowedVlan``'s own ``clean()``/``save()`` (direct
    through-row creation, which never fires ``m2m_changed``); and this
    model's own ``save()``, which re-checks a scalar change against
    already-*persisted* links (see ``_validate_scalars_against_persisted_links``).
    """

    name = models.CharField(max_length=100, unique=True)
    port_mode = models.CharField(
        max_length=10,
        choices=PortMode.choices,
        default=PortMode.TRUNK,
        help_text="Defaults to Trunk.",
    )
    native_vlan = models.ForeignKey(
        VLAN,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Primary (untagged) VLAN — implicitly allowed, so it must not also be listed "
        "under Allowed VLANs.",
    )
    all_vlans_allowed = models.BooleanField(
        default=False,
        help_text="If set, every VLAN is allowed on this port and Allowed VLANs must be empty.",
    )
    allowed_vlans: models.ManyToManyField = models.ManyToManyField(
        VLAN,
        through="SwitchPortVlanProfileAllowedVlan",
        related_name="+",
        blank=True,
        help_text="Additional tagged VLANs, beyond the implied Native VLAN. Must be empty if "
        "Allow All VLANs is set or Port Mode is Access.",
    )
    is_system = models.BooleanField(
        default=False,
        editable=False,
        help_text="System default profile (seeded by migration) — permanently locked, never deletable.",
    )

    #: Not a Django field — a one-save-lived escape hatch set by
    #: ``SwitchPortVlanProfileForm.clean()`` once it has already validated
    #: the *complete* submitted state (new scalars together with the new
    #: ``allowed_vlans`` selection). ``SwitchPortVlanProfile.save()`` clears
    #: it again immediately after using it, so it never outlives the one
    #: save() call it was granted for. See
    #: ``_validate_scalars_against_persisted_links`` for why
    #: ``Model.clean()``/``save()`` need this exemption at all.
    _trust_pending_m2m_from_form: bool = False

    objects = SwitchPortVlanProfileQuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~models.Q(name=""), name="switchportvlanprofile_name_not_blank"),
            models.CheckConstraint(
                # Access is inherently single-VLAN/untagged, so it can never
                # coexist with "every VLAN is allowed" — independent of
                # whether any explicit allowed_vlans links exist, unlike the
                # invariants in _validate_scalars_against_persisted_links().
                # A pure scalar-vs-scalar rule with no M2M timing subtlety,
                # so (unlike those) it can be a real DB constraint.
                condition=~(models.Q(port_mode=PortMode.ACCESS) & models.Q(all_vlans_allowed=True)),
                name="switchportvlanprofile_access_excludes_all_vlans_allowed",
            ),
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        # Never evaluates a queryset — this renders in admin selectors, list
        # columns, and every ValidationError message that names a profile.
        return self.name

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        with transaction.atomic():
            self._validate_port_mode_excludes_all_vlans_allowed(update_fields=update_fields)
            if self.pk is not None:
                _lock_profile_rows(self.pk)
                # is_system is immutable after creation, full stop — checked
                # before deciding which *other* fields are locked, since that
                # decision itself depends on whether this is the system row.
                _check_locked_fields_unchanged(
                    SwitchPortVlanProfile,
                    self.pk,
                    {"is_system": self.is_system},
                    update_fields=update_fields,
                )
                # for_update=True: this runs inside the transaction that
                # already holds the profile's row lock, so the "is a real
                # port using this profile" read should participate in that
                # same "always latest committed data" guarantee rather than
                # riding whatever REPEATABLE READ snapshot this transaction
                # established earlier (e.g. from loading the instance for an
                # admin edit) — a locking SELECT and a plain SELECT in the
                # same MySQL/InnoDB transaction are not guaranteed to see the
                # same committed state otherwise.
                self._check_scalar_fields_locked(update_fields=update_fields, for_update=True)
                self._validate_scalars_against_persisted_links(update_fields=update_fields)
            super().save(
                force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
            )
            # One-save exemption, consumed: SwitchPortVlanProfileForm.clean()
            # grants this for the one save() that follows it (before its
            # save_m2m() applies the newly-validated links) — left set, it
            # would silently exempt every later save() on this same instance
            # from _validate_scalars_against_persisted_links(), including
            # ones a form never validated at all.
            self._trust_pending_m2m_from_form = False

    def clean(self) -> None:
        super().clean()
        self._validate_port_mode_excludes_all_vlans_allowed()
        if self.pk is not None:
            _check_locked_fields_unchanged(
                SwitchPortVlanProfile, self.pk, {"is_system": self.is_system}, update_fields=None
            )
            # Not for_update: a bare full_clean() has no enclosing
            # transaction to lock within (same interim, best-effort
            # reasoning RackSlotAssignmentMixin.clean() documents for its
            # own cross-table check) — save() below is the actual guarantee.
            self._check_scalar_fields_locked(update_fields=None)
            self._validate_scalars_against_persisted_links()

    def delete(self, using=None, keep_parents=False) -> tuple[int, dict[str, int]]:
        with transaction.atomic():
            _lock_profile_rows(self.pk)
            # _persisted_is_system(), not self.is_system — see
            # _check_scalar_fields_locked for why the in-memory attribute
            # can't be trusted here either.
            if _persisted_is_system(self.pk) or self._referenced_by_any_port(for_update=True):
                raise ValidationError(
                    f"{self} cannot be deleted: it is the system default profile, or is still in "
                    "use by a switch type port or switch port."
                )
            return super().delete(using=using, keep_parents=keep_parents)

    @property
    def allows_all_vlans(self) -> bool:
        return self.all_vlans_allowed

    @property
    def effective_allowed_vlans(self) -> "set[VLAN]":
        """Native VLAN plus explicitly allowed VLANs. "All VLANs allowed"
        logically includes the native VLAN too. Meaningful only when
        ``allows_all_vlans`` is ``False`` — returns the same set either way
        so a caller that ignores the flag still gets something sane rather
        than an empty or misleading result.
        """
        vlans = set(self.allowed_vlans.all())
        native_vlan = _get_related(self, "native_vlan")
        if native_vlan is not None:
            vlans.add(native_vlan)
        return vlans

    def _in_use(self, *, for_update: bool = False) -> bool:
        """Whether a real ``NetworkSwitchPort`` references this profile —
        the trigger for locking ``port_mode``/``native_vlan`` (a profile
        referenced only by a ``NetworkSwitchTypePort`` is still fully
        editable). Deliberately narrower than deletion eligibility — see
        ``_referenced_by_any_port()``.
        """
        if self.pk is None:
            return False
        ports = self.ports.select_for_update() if for_update else self.ports
        return ports.exists()

    def _referenced_by_any_port(self, *, for_update: bool = False) -> bool:
        """Whether *any* port — type port or real port — references this
        profile. Used for deletion eligibility, a different question from
        ``_in_use()``'s scalar-locking trigger: a profile referenced only
        by a type port stays editable, but it still isn't deletable, since
        ``NetworkSwitchTypePort.profile`` is ``on_delete=PROTECT`` and would
        block the delete regardless. Checking it explicitly here gives a
        friendly ``ValidationError`` instead of a raw ``ProtectedError`` for
        that case, and matches ``SwitchPortVlanProfileQuerySet.delete()``'s
        bulk-path predicate.
        """
        if self.pk is None:
            return False
        type_ports = self.type_ports.select_for_update() if for_update else self.type_ports
        return type_ports.exists() or self._in_use(for_update=for_update)

    def _check_scalar_fields_locked(
        self, *, update_fields: "list[str] | frozenset[str] | None", for_update: bool = False
    ) -> None:
        # _persisted_is_system(), not self.is_system: the in-memory attribute
        # can be mutated without saving, and this decides which fields are
        # locked, so it must not trust an unsaved, possibly-forged value.
        if _persisted_is_system(self.pk):
            locked_fields = _PROFILE_SYSTEM_LOCKED_FIELDS
        elif self._in_use(for_update=for_update):
            locked_fields = _PROFILE_IN_USE_LOCKED_FIELDS
        else:
            return
        _check_locked_fields_unchanged(
            SwitchPortVlanProfile,
            self.pk,
            {
                field: (self.native_vlan_id if field == "native_vlan" else getattr(self, field))
                for field in locked_fields
            },
            update_fields=update_fields,
        )

    def _validate_port_mode_excludes_all_vlans_allowed(
        self, *, update_fields: "list[str] | frozenset[str] | None" = None
    ) -> None:
        """Access mode is inherently single-VLAN/untagged, so it can never
        coexist with ``all_vlans_allowed`` — independent of whether any
        explicit ``allowed_vlans`` links exist, unlike the invariants in
        ``_validate_scalars_against_persisted_links()`` below. A pure
        scalar-vs-scalar rule with no M2M timing subtlety, so — unlike
        those — it's backed by a real DB ``CheckConstraint`` too; this
        Python-level check exists only to turn that constraint's raw
        ``IntegrityError`` into a friendly message before it ever reaches
        the database.
        """
        if update_fields is not None and not ({"port_mode", "all_vlans_allowed"} & set(update_fields)):
            return
        if self.port_mode == PortMode.ACCESS and self.all_vlans_allowed:
            raise ValidationError(
                {"all_vlans_allowed": "all_vlans_allowed cannot be set while port_mode is Access."}
            )

    def _validate_scalars_against_persisted_links(
        self, *, update_fields: "list[str] | frozenset[str] | None" = None
    ) -> None:
        """Guards the one ``allowed_vlans`` invariant a plain scalar edit can
        violate on its own: flipping ``all_vlans_allowed``/``port_mode`` to a
        state that's incompatible with *already-persisted* allowed-VLAN
        links, or repointing ``native_vlan`` onto a VLAN that's already an
        explicit allowed link.

        Pending (not-yet-saved) M2M changes from the same admin submission
        aren't visible here — ``ModelForm.save_m2m()`` only runs after
        ``save()`` — those are validated separately by the ``m2m_changed``
        receiver and the through model's own ``clean()``/``save()``; see the
        class docstring. ``SwitchPortVlanProfileForm.clean()`` is the one
        place that ever sees the *submitted* scalars and the *submitted*
        M2M selection together — when it has already validated that
        complete state, it sets ``self._trust_pending_m2m_from_form`` on
        this instance, and this check trusts that proof rather than
        re-deriving a wrong answer from stale, still-persisted links. That
        flag is the only way to satisfy this check with a combined edit
        that both flips a scalar *and* clears the links that would
        otherwise conflict with it (e.g. enabling ``all_vlans_allowed``
        while clearing ``allowed_vlans`` in the same submission) — clearing
        links goes through ``.set([])``/``.clear()``, which the
        ``m2m_changed`` receiver correctly treats as always-safe and never
        validates, so nothing else in this module ever proves that
        particular combination sound. ``SwitchPortVlanProfile.save()``
        clears the flag again right after using it, so it exempts exactly
        the one save() call the form's clean() granted it for — never a
        later, unrelated save() on the same in-memory instance.

        Honors ``update_fields`` the same way ``_check_locked_fields_unchanged``
        does: a ``save(update_fields=[...])`` that doesn't touch any of the
        three fields this check cares about can't have introduced a conflict,
        regardless of what those fields currently hold in memory. Without
        this, e.g. ``profile.all_vlans_allowed = True;
        profile.save(update_fields=["name"])`` would raise even though
        ``all_vlans_allowed`` is never written.
        """
        if self._trust_pending_m2m_from_form:
            return
        relevant_fields = {"all_vlans_allowed", "port_mode", "native_vlan"}
        if update_fields is not None:
            attname_to_name = {
                field.attname: field.name
                for field in SwitchPortVlanProfile._meta.concrete_fields
                if field.attname != field.name
            }
            normalized_update_fields = {attname_to_name.get(name, name) for name in update_fields}
            if not (relevant_fields & normalized_update_fields):
                return
        if (self.all_vlans_allowed or self.port_mode == PortMode.ACCESS) and self.allowed_vlans.exists():
            raise ValidationError(
                "This profile's existing allowed VLANs must be removed first before setting "
                "all_vlans_allowed or switching port_mode to Access."
            )
        if self.allowed_vlans.filter(pk=self.native_vlan_id).exists():
            raise ValidationError(
                {
                    "native_vlan": (
                        f"{self.native_vlan} is already an explicit allowed VLAN on this profile — "
                        "the native VLAN is implicitly allowed and must not also be listed."
                    )
                }
            )


class SwitchPortVlanProfileAllowedVlan(AuditedModel):
    """Explicit through model for ``SwitchPortVlanProfile.allowed_vlans``.

    A plain M2M's auto-generated join table has no ``on_delete`` to set, so
    it can't protect a VLAN from removal (ADR 0007) — this explicit model
    gives the ``vlan`` side a real ``PROTECT`` FK, which Django's deletion
    collector honors for both ``Model.delete()`` and bulk
    ``QuerySet.delete()``/admin bulk-delete alike (the same pattern ADR
    0010 used for the switch/device type-port ``allowed_vlans`` this
    profile's own field replaces).

    Direct creation of a row here bypasses ``.add()``/``.set()`` entirely,
    so it never fires ``m2m_changed`` — this re-validates the same three
    invariants the signal receiver checks (see ``SwitchPortVlanProfile``),
    against its own profile's *current* scalar state. ``save()`` locks and
    re-reads that state fresh from the database rather than trusting a
    cached related object, for the same reason the ``m2m_changed`` receiver
    does — a stale ``self.profile`` could predate a concurrent, already-
    committed scalar change. ``bulk_create()`` remains a documented,
    unsupported bypass, consistent with this module's existing locked-field
    policy.
    """

    profile = models.ForeignKey(
        SwitchPortVlanProfile, on_delete=models.CASCADE, related_name="allowed_vlan_links"
    )
    vlan = models.ForeignKey(VLAN, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["profile", "vlan"], name="unique_profile_allowed_vlan"),
        ]

    def __str__(self) -> str:
        return f"{self.profile} allows {self.vlan}"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        with transaction.atomic():
            self._validate_against_profile(for_update=True)
            super().save(
                force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
            )

    def clean(self) -> None:
        super().clean()
        # Not for_update — see SwitchPortVlanProfile.clean() for why a bare
        # full_clean() doesn't take a lock; save() above is the guarantee.
        self._validate_against_profile()

    def _validate_against_profile(self, *, for_update: bool = False) -> None:
        if self.profile_id is None:
            return
        if for_update:
            profile = (
                SwitchPortVlanProfile._default_manager.select_for_update().filter(pk=self.profile_id).first()
            )
            if profile is None:
                return  # profile not visible (e.g. mid-delete elsewhere) — nothing to validate against
        else:
            profile = _get_related(self, "profile")
            if profile is None:
                return
        if profile.all_vlans_allowed or profile.port_mode == PortMode.ACCESS:
            raise ValidationError(
                f"{profile} does not accept explicit allowed VLANs while all_vlans_allowed is "
                "set or its port_mode is Access."
            )
        if profile.native_vlan_id == self.vlan_id:
            raise ValidationError(
                {"vlan": f"{self.vlan} is already {profile}'s native VLAN — it's implicitly allowed."}
            )


def _validate_profile_allowed_vlans_change(
    sender: type[models.Model],
    instance: "SwitchPortVlanProfile",
    action: str,
    pk_set: "set[int] | None",
    reverse: bool,
    **kwargs: Any,
) -> None:
    """The ``m2m_changed`` receiver in this module (see ``SwitchPortVlanProfile``
    docstring for why) — ``_clear_installed_cards_before_delete()`` below is
    the other one, on ``pre_delete``, for an unrelated reason (ADR 0022 PR
    3's Audit section). ``Model.clean()`` can't validate ``allowed_vlans`` —
    no pk yet on create, and stale on edit since ``ModelForm.save_m2m()``
    runs after ``save()`` — and the through model's own ``clean()``/``save()``
    only catches *direct* row creation, since ``.add()``/``.set()`` write the
    through table without ever calling its ``save()``. ``m2m_changed`` is the
    only hook Django offers for those calls.

    ``related_name="+"`` on ``allowed_vlans`` means there is no reverse
    accessor, so ``reverse=True`` can never actually fire here — the guard is
    defensive, not load-bearing.

    Locks the profile row and then **re-reads its scalars from the database**
    before validating (Django's ``.add()``/``.set()`` already run inside their
    own ``transaction.atomic()``, so this signal fires from within one).
    Validating ``instance``'s in-memory values instead would leave the very
    race this lock exists to close wide open: the caller's copy can predate a
    concurrent, already-committed change to ``native_vlan``/``port_mode``/
    ``all_vlans_allowed``, so the lock would serialize the section while the
    check still read stale data. Taking a lock and then trusting a cached
    object is lock-shaped, not lock-safe.
    """
    if reverse or action != "pre_add":
        return
    with transaction.atomic():
        _lock_profile_rows(instance.pk)
        current = (
            SwitchPortVlanProfile._default_manager.filter(pk=instance.pk)
            .values("all_vlans_allowed", "port_mode", "native_vlan")
            .first()
        )
        if current is None:
            return  # row not visible (e.g. mid-delete elsewhere) — nothing to validate against
        if current["all_vlans_allowed"] or current["port_mode"] == PortMode.ACCESS:
            raise ValidationError(
                f"{instance} does not accept explicit allowed VLANs while all_vlans_allowed is "
                "set or its port_mode is Access."
            )
        if pk_set and current["native_vlan"] in pk_set:
            raise ValidationError(
                f"{instance}'s native VLAN can't also be added as an explicit allowed VLAN — "
                "it's already implicitly allowed."
            )


models.signals.m2m_changed.connect(
    _validate_profile_allowed_vlans_change, sender=SwitchPortVlanProfile.allowed_vlans.through
)


#: Identity of the seeded system default VLAN/profile rows — shared by
#: migration ``0006_switch_port_vlan_profiles``' seed step and the
#: ``seed_defaults`` management command (the latter re-seeds these if
#: they're ever removed by ``manage.py flush``, which the migration can't
#: repair after the fact). Plain literals, not model classes, so importing
#: them into a migration doesn't create the "migrations must use historical
#: models" coupling that a model-class import would.
DEFAULT_VLAN_ID = 1
DEFAULT_VLAN_NAME = "Default VLAN"
DEFAULT_PROFILE_NAME = "Default"


def default_switch_port_vlan_profile() -> int:
    """Resolve the unique system default ``SwitchPortVlanProfile``'s pk *at
    call time* — never cache a pk at import time, which would bind to
    whatever happened to exist (or not) when models were first imported,
    rather than what's actually in the database when a port is created.
    """
    try:
        return SwitchPortVlanProfile.objects.get(is_system=True).pk
    except SwitchPortVlanProfile.DoesNotExist as exc:
        raise ValidationError(
            "No system default Switch Port VLAN Profile exists — run `manage.py seed_defaults` "
            "(or apply migrations) first."
        ) from exc
    except SwitchPortVlanProfile.MultipleObjectsReturned as exc:
        raise ValidationError(
            "Multiple system default Switch Port VLAN Profiles exist — exactly one row may have "
            "is_system=True."
        ) from exc


class NetworkSwitchType(AuditedModel):
    """A switch make/model *profile* (ADR 0010).

    The same physical hardware can have several profiles when what its
    ports are used for differs — e.g. "SG350-10MP — For Drive Rack" vs
    "SG350-10MP — For Amp Rack": identical hardware, different per-port
    VLAN purposes. ``name`` is the required, non-blank profile label;
    identity is ``(manufacturer, model, name)``, not just
    ``(manufacturer, model)``. A single-profile model still needs a name
    (conventionally "Default") so the type selector is never ambiguous for
    a non-expert audience.
    """

    manufacturer = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    name = models.CharField(
        max_length=100,
        help_text='Profile label, e.g. "For Drive Rack", or "Default" for a single-profile model.',
    )
    port_count = models.PositiveIntegerField(
        help_text="Must equal the number of Network Switch Type Ports defined for this profile."
    )
    hostname_slug = models.CharField(
        max_length=63,
        blank=True,
        validators=[validate_dns_label],
        help_text=(
            'Operator-set hostname abbreviation, e.g. "sg300-10mp". Never auto-filled — '
            'slugify("IK-42") gives "ik-42" where the name in use might be "ik42" — and '
            "deliberately not unique: two profiles of one model both carry the same abbreviation. "
            "Blank means this Type offers no computed hostnames (ADR 0023)."
        ),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["manufacturer", "model", "name"], name="unique_switch_type"),
            models.CheckConstraint(condition=~models.Q(name=""), name="networkswitchtype_name_not_blank"),
        ]
        ordering = ["manufacturer", "model", "name"]

    def __str__(self) -> str:
        return f"{self.manufacturer} {self.model} — {self.name}"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        # hostname_slug is normalized here, outside the locked-fields
        # dict below — it deliberately never joins the lock (settled
        # decision 3): a typo'd abbreviation must stay fixable without
        # creating a new named profile.
        if self.hostname_slug:
            self.hostname_slug = self.hostname_slug.strip().lower()
        with transaction.atomic():
            if self.pk is not None:
                _lock_type_rows(NetworkSwitchType, self.pk)
                if self.switches.exists():
                    _check_locked_fields_unchanged(
                        NetworkSwitchType,
                        self.pk,
                        {
                            "manufacturer": self.manufacturer,
                            "model": self.model,
                            "name": self.name,
                            "port_count": self.port_count,
                        },
                        update_fields=update_fields,
                    )
            super().save(
                force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
            )

    def clean_fields(self, exclude=None) -> None:
        # Normalize *before* the field validators run — see
        # Owner.clean_fields() for why this can't wait for clean().
        if self.hostname_slug:
            self.hostname_slug = self.hostname_slug.strip().lower()
        super().clean_fields(exclude=exclude)

    def clean(self) -> None:
        super().clean()
        if self.hostname_slug:
            self.hostname_slug = self.hostname_slug.strip().lower()
        if self.pk is not None and self.switches.exists():
            _check_locked_fields_unchanged(
                NetworkSwitchType,
                self.pk,
                {
                    "manufacturer": self.manufacturer,
                    "model": self.model,
                    "name": self.name,
                    "port_count": self.port_count,
                },
                update_fields=None,
            )


class NetworkSwitchTypePortQuerySet(models.QuerySet):
    """Blocks bulk ``QuerySet.delete()`` on a locked profile's type ports.

    ``NetworkSwitchTypePort.delete()`` guards a single-row delete, but
    Django's ``QuerySet.delete()`` (e.g. ``switch_type.type_ports.all()
    .delete()``, or an admin bulk-delete action) is a bulk SQL DELETE that
    never calls per-instance ``delete()`` — this closes that bypass without
    introducing a signal, consistent with how every other invariant in this
    module is enforced.
    """

    def delete(self):
        with transaction.atomic():
            type_ids = list(self.values_list("switch_type_id", flat=True).distinct())
            _lock_type_rows(NetworkSwitchType, *type_ids)
            if NetworkSwitchType._default_manager.filter(pk__in=type_ids, switches__isnull=False).exists():
                raise ValidationError(
                    "This profile's ports are locked because it already has switch instances; "
                    "create a new named profile to change the port layout."
                )
            return super().delete()


class NetworkSwitchTypePort(AuditedModel):
    """A port definition template on a Network Switch Type (profile).

    Copied exactly once into a real ``NetworkSwitchPort`` when a switch of
    this type is first created (``NetworkSwitch._materialize_ports``) — not
    kept in sync with later edits. Locked (see ``clean()``/``save()``/
    ``delete()``) once the parent type has any switch instance, per ADR
    0010 — change a profile's port layout by creating a new named profile
    instead.

    ``profile`` (a ``SwitchPortVlanProfile``) is a *live* reference, not a
    seed-once copy like everything else on this model — see
    ``SwitchPortVlanProfile`` and ADR 0012. Which profile a type port points
    at still locks the same way as any other field here once the type has
    instances; it's the profile's own contents that can keep changing.
    """

    switch_type = models.ForeignKey(NetworkSwitchType, on_delete=models.CASCADE, related_name="type_ports")
    port_number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    description = models.CharField(max_length=255, blank=True)
    port_type = models.CharField(max_length=20, choices=PortType.choices)
    profile = models.ForeignKey(
        SwitchPortVlanProfile,
        on_delete=models.PROTECT,
        related_name="type_ports",
        default=default_switch_port_vlan_profile,
        help_text="Switch Port VLAN Profile — defaults to the system Default profile if none is selected.",
    )

    objects = NetworkSwitchTypePortQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["switch_type", "port_number"], name="unique_switch_type_port_number"
            ),
            models.CheckConstraint(
                condition=models.Q(port_number__gte=1), name="networkswitchtypeport_port_number_gte_1"
            ),
        ]
        ordering = ["switch_type", "port_number"]

    def __str__(self) -> str:
        return f"{self.switch_type} type port {self.port_number}"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        with transaction.atomic():
            persisted_switch_type_id = self._persisted_switch_type_id()
            _lock_type_rows(NetworkSwitchType, self.switch_type_id, persisted_switch_type_id)
            if self._profile_locked() or self._persisted_profile_locked(persisted_switch_type_id):
                raise ValidationError(
                    "This profile's ports are locked because it already has switch instances; "
                    "create a new named profile to change the port layout."
                )
            super().save(
                force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
            )

    def delete(self, using=None, keep_parents=False) -> tuple[int, dict[str, int]]:
        # Checks the *persisted* parent, not just the in-memory
        # ``switch_type`` — reassigning this row to a different, unlocked
        # profile in memory and then calling delete() would otherwise
        # delete it out from under its real (locked) parent unchecked.
        with transaction.atomic():
            persisted_switch_type_id = self._persisted_switch_type_id()
            _lock_type_rows(NetworkSwitchType, self.switch_type_id, persisted_switch_type_id)
            if self._profile_locked() or self._persisted_profile_locked(persisted_switch_type_id):
                raise ValidationError(
                    "This profile's ports are locked because it already has switch instances; "
                    "create a new named profile to change the port layout."
                )
            return super().delete(using=using, keep_parents=keep_parents)

    def clean(self) -> None:
        super().clean()
        if self._profile_locked():
            raise ValidationError(
                "This profile's ports are locked because it already has switch instances; "
                "create a new named profile to change the port layout."
            )
        switch_type = _get_related(self, "switch_type")
        if (
            switch_type is not None
            and self.port_number
            and switch_type.port_count
            and self.port_number > switch_type.port_count
        ):
            raise ValidationError(
                {
                    "port_number": (
                        f"port_number {self.port_number} exceeds {switch_type}'s port_count "
                        f"({switch_type.port_count})."
                    )
                }
            )

    def _profile_locked(self) -> bool:
        switch_type = _get_related(self, "switch_type")
        return switch_type is not None and switch_type.pk is not None and switch_type.switches.exists()

    def _persisted_switch_type_id(self) -> int | None:
        if self.pk is None:
            return None
        return (
            NetworkSwitchTypePort._default_manager.filter(pk=self.pk)
            .values_list("switch_type_id", flat=True)
            .first()
        )

    def _persisted_profile_locked(self, original_switch_type_id: int | None) -> bool:
        """Whether this row's *persisted* parent (before any in-memory
        reassignment) is locked. ``_profile_locked()`` alone only sees the
        in-memory ``switch_type`` — reassigning a locked type port to a
        different, unlocked profile would otherwise pass that check and
        silently move the row out from under the locked profile.

        Takes the persisted parent id explicitly rather than recomputing it
        via ``_persisted_switch_type_id()`` — every caller already needs
        that value for ``_lock_type_rows()`` first.
        """
        if original_switch_type_id is None:
            return False
        return NetworkSwitchType._default_manager.filter(
            pk=original_switch_type_id, switches__isnull=False
        ).exists()


class NetworkSwitch(RackSlotAssignmentMixin, AuditedModel):
    """A physical switch instance. Unracked (rack is null) = spare pool.

    ``switch_type`` is fixed at creation (see ``save()``) — re-typing a
    switch means removing and recreating it, not editing this field (ADR
    0010): this keeps the port-profile guarantees on
    ``NetworkSwitchTypePort`` meaningful, and avoids the alternative of
    silently re-materializing (and thereby discarding DHCP-adjacent state,
    connections, and per-port overrides on) an already-configured switch.
    """

    switch_type = models.ForeignKey(NetworkSwitchType, on_delete=models.PROTECT, related_name="switches")
    hostname = models.CharField(max_length=63, blank=True)
    serial_number = models.CharField(max_length=100, blank=True)
    rack = models.ForeignKey(Rack, on_delete=models.PROTECT, null=True, blank=True, related_name="switches")
    rack_slot = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])
    dhcp_server_enabled = models.BooleanField(default=False)
    owner = models.ForeignKey(
        Owner,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="switches",
        help_text="Hostname component 1 (ADR 0023) — defaults from this switch's rack at creation.",
    )
    hostname_purpose = models.CharField(
        max_length=63,
        blank=True,
        validators=[validate_dns_label],
        help_text='Hostname component 4, e.g. "sub" or "midhi-01-04" — a non-numeric qualifier '
        "belongs here, not in the sequence below (ADR 0023).",
    )
    hostname_sequence = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Hostname component 5 — an integer distinguishing otherwise-identical names, "
        "e.g. 1 or 2 for mps-avio-aes-1 and mps-avio-aes-2 (ADR 0023).",
    )

    #: Class-level default for the ``address_materialization`` property
    #: below — never a plain class attribute, since Django's
    #: ``Model.__init__`` only accepts unknown kwargs
    #: (``objects.create(address_materialization=...)``) when the name is
    #: a field or a property (ADR 0016, mirroring ADR 0013's
    #: ``NetworkDevice._port_addressing``/``port_addressing``).
    _address_materialization: str = SwitchAddressing.STATIC

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["rack", "rack_slot"], name="unique_switch_rack_slot"),
            models.CheckConstraint(
                condition=models.Q(rack_slot__isnull=True) | models.Q(rack_slot__gte=1),
                name="networkswitch_rack_slot_gte_1",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(rack__isnull=True, rack_slot__isnull=True)
                    | models.Q(rack__isnull=False, rack_slot__isnull=False)
                ),
                name="networkswitch_rack_and_slot_together",
            ),
        ]
        ordering = ["hostname"]

    def __str__(self) -> str:
        return self.hostname or f"Switch #{self.pk}"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        # Stripped/lowercased here too, not just clean() — Model.save()
        # never calls clean() (Department.save()'s reasoning). hostname
        # joins hostname_purpose here (ADR 0023 decision 8, amended): the
        # importer's construct -> full_clean() -> save() path already goes
        # through clean_fields()/clean() below, but a bare objects.create()
        # or bulk write that skips full_clean() must still not persist raw
        # casing.
        if self.hostname:
            self.hostname = self.hostname.strip().lower()
        if self.hostname_purpose:
            self.hostname_purpose = self.hostname_purpose.strip().lower()
        # ``self.pk is None or self._state.adding``, not either alone:
        # ``_state.adding`` alone is wrong when a pk is pre-assigned
        # (fixtures, scripted inserts) before the row actually exists;
        # ``pk is None`` alone is wrong after ``instance.delete()`` (which
        # resets pk to None but leaves ``_state.adding`` False), which would
        # otherwise silently skip materialization on a re-save of the same
        # in-memory instance.
        is_new = self.pk is None or self._state.adding
        with transaction.atomic():
            if not is_new:
                _check_locked_fields_unchanged(
                    NetworkSwitch, self.pk, {"switch_type": self.switch_type_id}, update_fields=update_fields
                )
            elif self.switch_type_id is not None:
                # Locks the type row so a concurrent edit to its port
                # templates/count can't interleave with this materialization.
                _lock_type_rows(NetworkSwitchType, self.switch_type_id)
            super().save(
                force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
            )
            if is_new:
                self._materialize_ports()
                self._materialize_addresses()

    def clean_fields(self, exclude=None) -> None:
        # Normalize *before* the field validators run — see
        # Owner.clean_fields() for why this can't wait for clean().
        if self.hostname:
            self.hostname = self.hostname.strip().lower()
        if self.hostname_purpose:
            self.hostname_purpose = self.hostname_purpose.strip().lower()
        super().clean_fields(exclude=exclude)

    def clean(self) -> None:
        super().clean()
        if self.hostname:
            self.hostname = self.hostname.strip().lower()
        if self.hostname_purpose:
            self.hostname_purpose = self.hostname_purpose.strip().lower()
        if self.pk is None or self._state.adding:
            switch_type = _get_related(self, "switch_type")
            if switch_type is not None and switch_type.pk is not None:
                _validate_switch_type_port_profile(switch_type)
            # Departure from ADR 0016, recorded there: the ADR declines this
            # pre-flight, reasoning that a collision is the only remaining
            # failure and _validate_static_address() already reports those
            # clearly. That's an argument about message quality, and it
            # misses that Django's admin calls save_model() — and therefore
            # save(), and therefore _materialize_addresses() — *after* form
            # validation has already passed. A ValidationError raised from
            # inside save() has no form left to attach to; without a
            # clean()-time check, an Editor who hits a collision creating a
            # switch would get a 500, not a form error (the same failure
            # mode test_admin_add_post_address_collision_renders_form_error_not_500
            # exists to prevent on the device side).
            if self._materializes_addresses():
                self._check_address_materialization_possible()
        else:
            _check_locked_fields_unchanged(
                NetworkSwitch, self.pk, {"switch_type": self.switch_type_id}, update_fields=None
            )
            # Rename-only (ADR 0023 decision 6, amended twice) — only when
            # an existing row's hostname differs from what's stored, never
            # on creation (the branch above). _stored_hostname() mirrors
            # _check_locked_fields_unchanged()'s own guard: a row that
            # isn't visible (mid-delete elsewhere) has nothing to compare
            # against, so this is silently skipped rather than raising.
            stored_hostname = _stored_hostname(NetworkSwitch, self.pk)
            if stored_hostname is not None and self.hostname != stored_hostname:
                _validate_hostname_unique(self.hostname, exclude_switch_pk=self.pk, exclude_device_pk=None)

    @property
    def hostname_diverges(self) -> bool:
        """Stateless indicator (ADR 0023 decision 9, corrected in phase 18
        PR 4): a stored ``hostname`` exists, ``assemble_hostname()``'s
        equivalent join over this row's current components produces a
        name, and the two differ. No new field — recomputed on every
        access — and no collision query: this uses only the pure-join
        half (``_assemble_hostname_stem()``), never
        ``choose_sequence()``/``hostname_is_taken()``, so rendering it
        costs no query beyond the three relation reads (``owner``,
        ``rack``, ``switch_type``) a caller must already have
        ``select_related`` for its own sake.

        Narrower than ADR 0023's original "every component present"
        wording, which would make divergence unreachable in practice —
        no live device has both a purpose and a sequence. The operative
        test is just: does the stored name still match what its own
        components would produce right now? A rack move that leaves the
        previous rack's location baked into a name is exactly what this
        is for (#54); it says nothing about which reading is *right*.
        """
        if not self.hostname:
            return False
        owner = _get_related(self, "owner")
        rack = _get_related(self, "rack")
        switch_type = _get_related(self, "switch_type")
        computed = _assemble_hostname_stem(
            owner_slug=owner.slug if owner is not None else None,
            location_slug=rack.location_slug if rack is not None else None,
            type_slug=switch_type.hostname_slug if switch_type is not None else None,
            purpose=self.hostname_purpose,
            sequence=self.hostname_sequence,
        )
        return computed is not None and computed != self.hostname

    @property
    def address_materialization(self) -> str:
        """Creation-time-only choice of whether this switch's VLAN
        addresses materialize (ADR 0016). Never stored — the materialized
        ``NetworkSwitchAddress`` rows are the record of what was chosen;
        setting this after creation has no effect since
        ``_materialize_addresses()`` only runs once.
        """
        return self._address_materialization

    @address_materialization.setter
    def address_materialization(self, value: str) -> None:
        if value not in SwitchAddressing.values:
            raise ValidationError(
                f"{value!r} is not a valid address_materialization — must be one of "
                f"{SwitchAddressing.values}."
            )
        self._address_materialization = value

    def _materializes_addresses(self) -> bool:
        """Whether this (not-yet-materialized) switch will get VLAN
        addresses — only when racked (unracked is spare pool, DHCP-
        configured by definition per CONTEXT.md) and the static choice is
        in effect (ADR 0016).
        """
        return self.rack is not None and self.address_materialization == SwitchAddressing.STATIC

    def _check_address_materialization_possible(self) -> None:
        """Pre-flight over ``self.rack``'s ``RackVlanRange`` rows for
        whether address materialization can succeed — pure, needs no
        switch pk, so it can run from both ``clean()`` (admin form errors)
        and the top of ``_materialize_addresses()`` (the
        ``objects.create()`` path, which never calls ``clean()``).

        Unlike the device side, a missing range is not a failure here —
        the rack's ranges *are* the VLAN list (ADR 0016's trade-off
        section), so an empty list is trivially satisfied. The only
        failure this checks for is a collision on a range that does exist.
        """
        if self.rack is None:
            return  # nothing to check — mirrors _materializes_addresses()'s own guard
        for rack_range in self.rack.vlan_ranges.select_related("vlan").order_by("vlan__vlan_id"):
            address = _suggest_rack_slot_address(self.rack, self.rack_slot, rack_range.vlan_id)
            if address is None:
                continue  # rack_range.vlan itself has no usable range; nothing to validate
            try:
                _validate_static_address(
                    address,
                    rack_range.vlan,
                    self.rack,
                    self.rack_slot,
                    exclude_switch_address_pk=None,
                    exclude_device_port_pk=None,
                )
            except ValidationError as exc:
                # _validate_static_address() raises keyed on "address" — the
                # right shape for NetworkSwitchAddress.clean() (which has
                # that field), but this call site is NetworkSwitch.clean(),
                # which doesn't. A dict-keyed ValidationError for a
                # nonexistent form field crashes Django's admin
                # add_error() with a raw ValueError instead of rendering a
                # form error, so re-raise as a plain, non-field error here
                # (copied from NetworkDevice._check_static_materialization_
                # possible()).
                raise ValidationError(exc.messages) from exc

    def _materialize_addresses(self) -> None:
        """One-time creation of one ``NetworkSwitchAddress`` per
        ``RackVlanRange`` on this switch's rack (ADR 0016), each filled by
        the existing suggestion path. Runs inside the same transaction as
        this switch's insert (see ``save()``), so any failure here rolls
        back the switch and every address materialized before it.

        A rack with no ranges — or an unracked switch, or the ``MANUAL``
        choice — simply produces no addresses; that is not an error (see
        ``_check_address_materialization_possible()``).
        """
        if not self._materializes_addresses() or self.rack is None:
            return
        self._check_address_materialization_possible()
        for rack_range in self.rack.vlan_ranges.select_related("vlan").order_by("vlan__vlan_id"):
            address = NetworkSwitchAddress(switch=self, vlan=rack_range.vlan, created_by=self.created_by)
            address.full_clean()
            address.save()

    def _materialize_ports(self) -> None:
        """One-time copy of ``switch_type``'s Network Switch Type Ports into
        real ``NetworkSwitchPort`` rows (ADR 0010). Runs inside the same
        transaction as this switch's insert (see ``save()``), so an
        incomplete profile or a failed child row leaves neither the switch
        nor any partial ports behind.
        """
        _validate_switch_type_port_profile(self.switch_type)
        for type_port in self.switch_type.type_ports.all():
            NetworkSwitchPort.objects.create(
                switch=self,
                port_number=type_port.port_number,
                description=type_port.description,
                port_type=type_port.port_type,
                # ``profile_id``, not ``profile``: the profile object itself is
                # never used here, and touching the descriptor would fetch the
                # row once per type port (a 48-port switch paid 48 extra
                # SELECTs for a value it already had as a raw id).
                profile_id=type_port.profile_id,
                source_type_port=type_port,
                created_by=self.created_by,
            )

    def _check_rack_slot_not_occupied(self) -> None:
        """ADR 0027: a switch always claims exactly its own ordinal, but a
        device's claim is a **set**, not a contiguous span
        (``NetworkDeviceType.claimed_offsets``), so an equality test
        against a device's ``rack_slot`` alone would miss a device whose
        offset ports reach this switch's slot without starting there (a
        device at 7 with offsets ``{0, 1}`` occupying 7 and 8, and a
        switch trying to claim 8).

        The query below still narrows candidates to those whose own
        contiguous *envelope* (``rack_slot .. rack_slot + slot_span - 1``)
        could possibly reach this switch's slot — cheap, and sufficient as
        a prefilter, since any *actual* claimed ordinal is necessarily
        inside that envelope — and the exact, set-based check happens in
        Python afterward, the same "SQL can bound, only Python can express
        a set" split ``occupied_rack_slot_ordinals()`` documents.

        ``claimed_offsets`` for every candidate is resolved once, in bulk,
        before the loop (``_bulk_claimed_offsets()``) rather than read
        off each candidate's own property — that property recomputes on
        every access, so reading it per candidate here is an N+1 (ADR 0027
        PR 1 review).
        """
        candidates = list(
            NetworkDevice.objects.filter(rack=self.rack, rack_slot__lte=self.rack_slot)
            .annotate(_span=Coalesce(models.Max("device_type__type_ports__slot_offset"), 0) + 1)
            .annotate(_end=models.F("rack_slot") + models.F("_span") - 1)
            .filter(_end__gte=self.rack_slot)
            .select_related("device_type")
        )
        claimed_offsets = _bulk_claimed_offsets(candidate.device_type_id for candidate in candidates)
        for candidate in candidates:
            assert candidate.rack_slot is not None  # DB constraint: rack and rack_slot are all-or-neither
            offsets = claimed_offsets.get(candidate.device_type_id, frozenset({0}))
            if self.rack_slot in {candidate.rack_slot + offset for offset in offsets}:
                raise ValidationError(
                    f"Rack slot {self.rack_slot} in {self.rack} is already occupied by a device."
                )

    def _validate_existing_addresses_still_fit(self) -> None:
        for address in self.addresses.all():
            if address.address is None:
                continue  # DB CheckConstraint guarantees this can't happen; satisfies mypy
            if self.rack is None:
                raise ValidationError(
                    f"Cannot unrack {self}: it still has a static address ({address.address} on "
                    f"{address.vlan}); remove or reassign its addresses first."
                )
            error = _address_containment_error(address.address, address.vlan, self.rack, self.rack_slot)
            if error:
                raise ValidationError(f"Moving {self} would leave an existing address invalid: {error}")


class NetworkSwitchAddress(AuditedModel):
    """A switch's static address on one VLAN.

    Defaults to rack range base + rack slot (ADR 0003's computed-but-
    stored pattern applies here too) via ``clean()``, when the switch is
    racked and a ``RackVlanRange`` already exists for the VLAN.
    """

    switch = models.ForeignKey(NetworkSwitch, on_delete=models.CASCADE, related_name="addresses")
    vlan = models.ForeignKey(VLAN, on_delete=models.PROTECT, related_name="switch_addresses")
    address = models.GenericIPAddressField(
        protocol="IPv4",
        blank=True,
        null=True,
        help_text="Leave blank to suggest rack range base + rack slot.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["switch", "vlan"], name="unique_switch_vlan_address"),
            models.UniqueConstraint(fields=["vlan", "address"], name="unique_switch_vlan_address_value"),
            models.CheckConstraint(
                condition=models.Q(address__isnull=False),
                name="networkswitchaddress_address_required",
            ),
        ]
        ordering = ["vlan", "address"]

    def __str__(self) -> str:
        return f"{self.switch} / {self.vlan}: {self.address}"

    def clean(self) -> None:
        super().clean()
        switch = _get_related(self, "switch")
        vlan = _get_related(self, "vlan")
        if switch is not None and switch.rack is None:
            raise ValidationError(
                "Unracked switches are spare pool (DHCP-configured per CONTEXT.md) and "
                "don't get a static VLAN address; rack the switch first."
            )
        if self.pk is None and not self.address and switch is not None and vlan is not None:
            suggestion = _suggest_rack_slot_address(switch.rack, switch.rack_slot, vlan.pk)
            if suggestion:
                self.address = suggestion
        if not self.address:
            raise ValidationError(
                {
                    "address": (
                        "This field is required — no suggestion could be computed "
                        "automatically (a RackVlanRange must already be assigned for "
                        "this VLAN), so it must be entered manually."
                    )
                }
            )
        if switch is not None and vlan is not None:
            _validate_static_address(
                self.address,
                vlan,
                switch.rack,
                switch.rack_slot,
                exclude_switch_address_pk=self.pk,
                exclude_device_port_pk=None,
            )


def _lock_switch_port_rows(*pks: int | None) -> None:
    """Acquire a row lock on the given ``NetworkSwitchPort`` rows — must run
    inside ``transaction.atomic()``.

    Closes the race between changing this port's ``profile`` and
    connecting/reassigning a ``NetworkDevicePort.switch_port`` to it: both
    sides take this lock before checking whether the other condition
    (device connected / profile being changed) holds, so they can't each
    observe a stale, still-consistent state and both proceed.
    """
    ids = sorted({pk for pk in pks if pk is not None})
    if ids:
        list(NetworkSwitchPort._default_manager.select_for_update().filter(pk__in=ids))


class NetworkSwitchPort(AuditedModel):
    """A single physical port on a switch — L2 config only, no address.

    Materialized exactly once from the switch's ``switch_type`` when the
    switch is first created (``NetworkSwitch._materialize_ports``).
    ``port_type`` is a locked hardware fact copied from the type port;
    ``description`` stays editable per switch, same as before.

    ``profile`` (a ``SwitchPortVlanProfile``) replaces the old
    ``port_mode``/``native_vlan``/``allowed_vlans`` fields — see
    ``SwitchPortVlanProfile`` and ADR 0012. Unlike ``description``, it can't
    be swapped freely: DESIGN.md allows selecting a different profile "unless
    a device is already connected" (``connected_device_port``), enforced in
    ``clean()``/``save()`` below.
    """

    switch = models.ForeignKey(NetworkSwitch, on_delete=models.CASCADE, related_name="ports")
    port_number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    description = models.CharField(max_length=255, blank=True)
    port_type = models.CharField(
        max_length=20,
        choices=PortType.choices,
        blank=True,
        help_text="Physical hardware fact, copied from the switch's type — locked after creation.",
    )
    profile = models.ForeignKey(
        SwitchPortVlanProfile,
        on_delete=models.PROTECT,
        related_name="ports",
        default=default_switch_port_vlan_profile,
        help_text="Switch Port VLAN Profile. Can be swapped for another unless a device is connected.",
    )
    source_type_port = models.ForeignKey(
        NetworkSwitchTypePort,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="materialized_ports",
        help_text="Provenance only — never used to re-derive this port's fields (seed-once).",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["switch", "port_number"], name="unique_switch_port_number"),
            models.CheckConstraint(
                condition=models.Q(port_number__gte=1),
                name="networkswitchport_port_number_gte_1",
            ),
        ]
        ordering = ["switch", "port_number"]

    def __str__(self) -> str:
        return f"{self.switch} port {self.port_number}"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        with transaction.atomic():
            is_new = self.pk is None or self._state.adding
            if is_new:
                # Materialization (and any other insert) flips the target
                # profile's in-use state — lock it before it's read anywhere
                # else concurrently (see ``SwitchPortVlanProfile``).
                _lock_profile_rows(self.profile_id)
            else:
                _lock_switch_port_rows(self.pk)
                persisted_profile_id = self._persisted_profile_id()
                _lock_profile_rows(self.profile_id, persisted_profile_id)
                # ``self._profile_field_included(update_fields)``: without
                # this, save(update_fields=["description"]) on an instance
                # whose in-memory profile_id happens to differ from what's
                # persisted (e.g. reused for an unrelated computation) would
                # wrongly reject the save — profile was never going to be
                # written at all.
                if (
                    persisted_profile_id != self.profile_id
                    and self._profile_field_included(update_fields)
                    and self._has_connected_device_port()
                ):
                    raise ValidationError(
                        {
                            "profile": (
                                "profile cannot be changed while a device port is connected to "
                                "this switch port; disconnect it first."
                            )
                        }
                    )
                _check_locked_fields_unchanged(
                    NetworkSwitchPort, self.pk, self._locked_fields(), update_fields=update_fields
                )
            super().save(
                force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
            )

    def clean(self) -> None:
        super().clean()
        if self.pk is not None:
            _check_locked_fields_unchanged(
                NetworkSwitchPort, self.pk, self._locked_fields(), update_fields=None
            )
            persisted_profile_id = self._persisted_profile_id()
            if persisted_profile_id != self.profile_id and self._has_connected_device_port():
                raise ValidationError(
                    {
                        "profile": (
                            "profile cannot be changed while a device port is connected to this "
                            "switch port; disconnect it first."
                        )
                    }
                )

    def _locked_fields(self) -> dict[str, Any]:
        # ``switch``/``port_number``/``source_type_port`` identify which
        # physical port this row represents (materialized once from the
        # switch's type, ADR 0010) — only ``description``/``profile`` are
        # meant to be editable per switch, so a plain save() must not be
        # able to silently move or renumber a materialized port.
        return {
            "switch": self.switch_id,
            "port_number": self.port_number,
            "port_type": self.port_type,
            "source_type_port": self.source_type_port_id,
        }

    def _persisted_profile_id(self) -> int | None:
        if self.pk is None:
            return None
        return (
            NetworkSwitchPort._default_manager.filter(pk=self.pk).values_list("profile_id", flat=True).first()
        )

    @staticmethod
    def _profile_field_included(update_fields: "list[str] | frozenset[str] | None") -> bool:
        """Whether a ``save(update_fields=...)`` call would actually write
        ``profile`` — ``None`` means "every field", so this is ``True`` for
        a plain ``save()``. Accepts both ``"profile"`` and the attname
        ``"profile_id"``, matching Django's own ``update_fields`` handling.
        """
        return update_fields is None or "profile" in update_fields or "profile_id" in update_fields

    def _has_connected_device_port(self) -> bool:
        return NetworkDevicePort.objects.filter(switch_port_id=self.pk).exists()


def switch_port_profile_summary(port: "NetworkSwitchPort") -> str:
    """The "Profile config" computed column — mode, native VLAN, and (for a
    trunk) the allowed VLANs — read off ``port.profile``.

    Module-level and not a method on either caller, so the admin's
    ``NetworkSwitchPortInline.profile_summary`` and the read-only UI's
    generic parity page (``inventory/views.py``, phase 15 Stage B) share one
    implementation rather than risking two independently-drifting copies of
    the same formatting rule (``PLAN-read-only-ui.md`` Stage B: "do not
    reimplement the formatting twice"). Callers are responsible for their
    own query shaping — this reads ``port.profile``,
    ``profile.native_vlan``, and ``profile.allowed_vlans`` and does not
    prefetch anything itself.
    """
    profile = port.profile
    mode = profile.get_port_mode_display()
    if profile.all_vlans_allowed:
        return f"{mode}, all VLANs allowed"
    allowed = ", ".join(str(vlan.vlan_id) for vlan in profile.allowed_vlans.all())
    summary = f"{mode}, native {profile.native_vlan.vlan_id}"
    return f"{summary}, allowed {allowed}" if allowed else summary


class NetworkDeviceModel(AuditedModel):
    """The bare hardware identity a ``NetworkDeviceType`` profile is built
    on (ADR 0026) — "an Amphenol RJD32A3-0050", independent of what any
    particular profile's ports are wired for. Several ``NetworkDeviceType``
    rows (``related_name="profiles"``) can share one model: a Martin Audio
    IK-42 "with Dante Card" and "without Dante Card" are the same box.

    ``description`` is what an `Amphenol RJD32A3-0050` *is* to someone who
    doesn't already know the part number ("Dante Interface with AES3
    I/O") — model-level, not profile-level, because two profiles of one
    model describing the hardware differently would be the exact drift
    ADR 0026 was written to prevent. Distinct from
    ``NetworkDeviceTypePort.description``, which is a *port's* purpose
    label, not a statement about the hardware as a whole.

    ``hostname_slug`` moved here from ``NetworkDeviceType`` in ADR 0026 PR
    2, for the same reason as ``description``: it is a fact about the
    hardware, not the profile, and ADR 0023 decision 1 makes it
    *blocking* — a divergent value between two profiles of one model used
    to silently compute different hostnames for identical hardware, and
    now can't, because there is only one value to read.

    **Deliberately not locked** — the one place this model departs from
    every locked-after-instances neighbour in this module. ADR 0010
    locked a profile's ``manufacturer``/``model`` because a denormalized
    copy going stale on one profile while its siblings kept the old value
    was a real hazard; with the hardware identity in one row, editing it
    updates every profile of that model coherently, which is correct
    behaviour, not drift (ADR 0026 decision 3). Duplicate models (e.g.
    `Lab Gruppen LM26` alongside `Lab.Gruppen LM26`) are creatable and not
    merged here — decision 4, issue #79. ``save()``/``clean_fields()``/
    ``clean()`` below exist only to normalize ``hostname_slug`` (the
    ``Owner`` shape, not the locked-profile shape) — there is no
    ``_locked_snapshot()`` and no lock check anywhere on this model.
    """

    manufacturer = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    description = models.CharField(
        max_length=255,
        blank=True,
        help_text=(
            'What this hardware is, e.g. "Dante Interface with AES3 I/O" — a model-level fact, '
            "not a profile's port purpose (see NetworkDeviceTypePort.description). Blank is fine; "
            "nothing depends on it being filled."
        ),
    )
    hostname_slug = models.CharField(
        max_length=63,
        blank=True,
        validators=[validate_dns_label],
        help_text=(
            'Operator-set hostname abbreviation, e.g. "ik42". Never auto-filled — '
            'slugify("IK-42") gives "ik-42" where the name in use might be "ik42". Blank means '
            "no profile of this model offers a computed hostname (ADR 0023)."
        ),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["manufacturer", "model"], name="unique_device_model"),
            models.CheckConstraint(
                condition=~models.Q(manufacturer=""), name="networkdevicemodel_manufacturer_not_blank"
            ),
            models.CheckConstraint(condition=~models.Q(model=""), name="networkdevicemodel_model_not_blank"),
        ]
        ordering = ["manufacturer", "model"]

    def __str__(self) -> str:
        return f"{self.manufacturer} {self.model}"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        # hostname_slug moved here from NetworkDeviceType (ADR 0026 PR 2) —
        # normalized on save exactly as it was there, and as Owner.save()
        # normalizes its own slug: this model is deliberately unlocked
        # (see the class docstring), so there is no lock check to run
        # alongside it, unlike NetworkDeviceType.save().
        if self.hostname_slug:
            self.hostname_slug = self.hostname_slug.strip().lower()
        super().save(
            force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
        )

    def clean_fields(self, exclude=None) -> None:
        # Normalize *before* the field validators run — see
        # Owner.clean_fields() for why this can't wait for clean().
        if self.hostname_slug:
            self.hostname_slug = self.hostname_slug.strip().lower()
        super().clean_fields(exclude=exclude)

    def clean(self) -> None:
        super().clean()
        if self.hostname_slug:
            self.hostname_slug = self.hostname_slug.strip().lower()


class NetworkDeviceType(AuditedModel):
    """A device make/model *profile* (ADR 0010) — see ``NetworkSwitchType``
    for what "profile" means here. E.g. "Martin Audio IK-42 — with Dante
    Card" vs "— without Dante Card", or "Shure ULXD4Q — Split Mode" vs
    "— Redundant Mode": identical hardware, different port sets/purposes.

    The hardware identity itself lives on ``NetworkDeviceModel`` (ADR
    0026) — this class is a *profile of* one, not the bare hardware model.
    """

    device_model = models.ForeignKey(NetworkDeviceModel, on_delete=models.PROTECT, related_name="profiles")
    name = models.CharField(
        max_length=100,
        help_text='Profile label, e.g. "with Dante Card", or "Default" for a single-profile model.',
    )
    port_count = models.PositiveIntegerField(
        help_text="Must equal the number of Network Device Type Ports defined for this profile."
    )
    is_add_in_card = models.BooleanField(
        default=False,
        help_text=(
            "This type's instances are cards fitted inside another device and routinely moved "
            "between hosts — a DMI-DANTE, an X-Dante. Leave off for ordinary equipment."
        ),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["device_model", "name"], name="unique_device_type"),
            models.CheckConstraint(condition=~models.Q(name=""), name="networkdevicetype_name_not_blank"),
        ]
        ordering = ["device_model__manufacturer", "device_model__model", "name"]

    def __str__(self) -> str:
        return f"{self.device_model} — {self.name}"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        with transaction.atomic():
            if self.pk is not None:
                _lock_type_rows(NetworkDeviceType, self.pk)
                if self.devices.exists():
                    _check_locked_fields_unchanged(
                        NetworkDeviceType, self.pk, self._locked_snapshot(), update_fields=update_fields
                    )
            super().save(
                force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
            )

    @property
    def slot_span(self) -> int:
        """``max(slot_offset) + 1`` across this type's ports (ADR 0017), or
        ``1`` for a type with no offset ports (or no ports at all yet) —
        the highest ordinal an instance's claim *reaches*. Kept **only**
        for the rack ``slot_count`` bound (``rack_slot + slot_span - 1 <=
        slot_count``), which genuinely is about the highest ordinal
        reached, not the full set of ordinals claimed. See
        ``claimed_offsets`` below for occupancy proper (ADR 0027 decision
        2) — two honest concepts rather than forcing both through one
        number: a type with offsets ``{0, 64}`` has a ``slot_span`` of 65
        but a ``claimed_offsets`` of only ``{0, 64}``.

        Computed, not stored — a type's port list is immutable once any
        instance exists (ADR 0010), so this can never drift from what a
        created device actually occupies (decided over denormalizing it
        onto ``NetworkDevice``: both are stable, and either is a single
        query, so the query-cost argument for a stored copy is a wash).
        Unconditional over *every* type port, not just addressable/static
        ones — a DHCP-materialized console still spans its type's
        ordinals, since ``is_dhcp`` is editable per port after creation
        and a span that depended on it would drift.
        """
        max_offset = self.type_ports.aggregate(models.Max("slot_offset"))["slot_offset__max"]
        return (max_offset or 0) + 1

    @property
    def claimed_offsets(self) -> frozenset[int]:
        """Every distinct ordinal, relative to an instance's own
        ``rack_slot``, this type's instances claim (ADR 0027 decision 2) —
        ``{rack_slot + offset for offset in claimed_offsets}`` is the
        occupied ordinal **set**, not a range: a type with offsets ``{0,
        64}`` claims exactly two ordinals and is indifferent to the 63
        between them (the fix for issue #83).

        Always includes ``0`` — a device occupies its own slot regardless
        of whether any type port is actually declared there. Computed, not
        stored, same reasoning as ``slot_span`` above; the two are
        deliberately separate properties (``slot_span`` for the
        ``slot_count`` bound, this for occupancy) rather than one value
        pressed into both jobs.
        """
        offsets = set(self.type_ports.values_list("slot_offset", flat=True))
        offsets.add(0)
        return frozenset(offsets)

    def clean(self) -> None:
        super().clean()
        if self.pk is not None and self.devices.exists():
            _check_locked_fields_unchanged(
                NetworkDeviceType, self.pk, self._locked_snapshot(), update_fields=None
            )

    def _locked_snapshot(self) -> dict[str, Any]:
        # Shared by save()/clean() above. is_add_in_card joins the snapshot
        # (ADR 0022 PR 3) — flipping it after instances exist would either
        # strand fitted devices (turning it off under a card with a host)
        # or retroactively offer ordinary equipment to the fit picker
        # (turning it on under stock that was never meant to be fitted).
        return {
            "device_model": self.device_model_id,
            "name": self.name,
            "port_count": self.port_count,
            "is_add_in_card": self.is_add_in_card,
        }


class NetworkDeviceTypePortQuerySet(models.QuerySet):
    """Blocks bulk ``QuerySet.delete()`` on a locked profile's type ports —
    see ``NetworkSwitchTypePortQuerySet`` for why this is needed alongside
    the model's own ``delete()`` override.
    """

    def delete(self):
        with transaction.atomic():
            type_ids = list(self.values_list("device_type_id", flat=True).distinct())
            _lock_type_rows(NetworkDeviceType, *type_ids)
            if NetworkDeviceType._default_manager.filter(pk__in=type_ids, devices__isnull=False).exists():
                raise ValidationError(
                    "This profile's ports are locked because it already has device instances; "
                    "create a new named profile to change the port layout."
                )
            return super().delete()


class NetworkDeviceTypePort(AuditedModel):
    """A port definition template on a Network Device Type (profile).

    Copied exactly once into a real ``NetworkDevicePort`` when a device of
    this type is first created — see ``NetworkDevice._materialize_ports``.
    Locked once the parent type has any device instance (ADR 0010), same
    as ``NetworkSwitchTypePort``. Unlike switch type ports, ``description``
    (not ``port_number``) is the identity — most of these devices have a
    fixed purpose per port but no meaningful port number (e.g. "Dante
    Primary").

    ``slot_offset`` (ADR 0017, generalized by ADR 0027) is every static
    address's whole addressing mechanism: ``range_base + rack_slot +
    slot_offset``. Every type port defaults to offset 0 (its own slot),
    and a VLAN with any non-zero-offset port must also carry an offset-0
    port on that VLAN (``_validate_device_type_port_profile``). A non-zero
    offset covers two distinct hardware shapes — a second port whose
    address the hardware itself computes from the first and refuses to
    let anyone change (a DiGiCo console's audio engine, always control
    address + 1), and a second, independently-addressed interface on a
    VLAN the device already uses (a Yamaha console's Device Control
    interface, ADR 0027 decision 3, closing #42 — retiring ADR 0022's
    ``OPERATOR`` mechanism for exactly this case). Ordinary multi-device
    hardware (a console plus a separately-addressed extender, an add-in
    card) stays as separate, independently-addressed ``NetworkDevice``
    rows instead — see ADR 0017's scope-boundary section.
    """

    device_type = models.ForeignKey(NetworkDeviceType, on_delete=models.CASCADE, related_name="type_ports")
    port_number = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])
    description = models.CharField(
        max_length=255, help_text='Required — this port\'s purpose/identity, e.g. "Dante Primary".'
    )
    port_type = models.CharField(max_length=20, choices=PortType.choices)
    vlan = models.ForeignKey(VLAN, on_delete=models.PROTECT, related_name="+")
    slot_offset = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Address offset from the device's own rack slot (ADR 0027) — every static address "
            "is range_base + rack_slot + slot_offset. Leave at 0 unless this port needs a "
            "second, distinct address on its VLAN (e.g. a console engine at control + 1, or a "
            "second interface like a Yamaha console's Device Control)."
        ),
    )
    hostname_suffix = models.CharField(
        max_length=63,
        blank=True,
        validators=[validate_dns_label],
        help_text=(
            'Names this port in address lists as "<device hostname>-<suffix>", e.g. "engine" '
            "for a console's audio engine. Store it bare, with no leading dash. Leave blank "
            "for a port that shares the device's own name."
        ),
    )
    ordinal = models.PositiveIntegerField(
        editable=False,
        default=0,
        help_text="Stable display order; auto-assigned.",
    )

    objects = NetworkDeviceTypePortQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["device_type", "description"], name="unique_device_type_port_description"
            ),
            models.UniqueConstraint(
                fields=["device_type", "ordinal"], name="unique_device_type_port_ordinal"
            ),
            models.CheckConstraint(
                condition=~models.Q(description=""), name="networkdevicetypeport_description_not_blank"
            ),
            models.CheckConstraint(
                condition=models.Q(port_number__isnull=True) | models.Q(port_number__gte=1),
                name="networkdevicetypeport_port_number_gte_1",
            ),
        ]
        ordering = ["device_type", "ordinal"]

    def __str__(self) -> str:
        return f"{self.device_type} type port: {self.description}"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        with transaction.atomic():
            if self.hostname_suffix:
                self.hostname_suffix = self.hostname_suffix.strip().lower()
            persisted_device_type_id = self._persisted_device_type_id()
            _lock_type_rows(NetworkDeviceType, self.device_type_id, persisted_device_type_id)
            if (
                self._profile_locked() or self._persisted_profile_locked(persisted_device_type_id)
            ) and not self._hostname_suffix_only_edit(update_fields=update_fields):
                raise ValidationError(
                    "This profile's ports are locked because it already has device instances; "
                    "create a new named profile to change the port layout."
                )
            # ``force=True``: recompute under the lock rather than trusting
            # whatever ``clean()`` may have already set — admin formsets run
            # every row's ``clean()`` before any row is saved, so two new
            # ports added to the same profile in one submission would
            # otherwise both compute the same "next" ordinal and collide on
            # ``unique_device_type_port_ordinal``.
            self._assign_ordinal_if_unset(force=True)
            super().save(
                force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
            )

    def delete(self, using=None, keep_parents=False) -> tuple[int, dict[str, int]]:
        # Checks the *persisted* parent — see ``NetworkSwitchTypePort.delete()``.
        # No hostname_suffix exemption here (unlike save()/clean() below) —
        # removing the whole row is never "just a hostname_suffix edit".
        with transaction.atomic():
            persisted_device_type_id = self._persisted_device_type_id()
            _lock_type_rows(NetworkDeviceType, self.device_type_id, persisted_device_type_id)
            if self._profile_locked() or self._persisted_profile_locked(persisted_device_type_id):
                raise ValidationError(
                    "This profile's ports are locked because it already has device instances; "
                    "create a new named profile to change the port layout."
                )
            return super().delete(using=using, keep_parents=keep_parents)

    def clean_fields(self, exclude=None) -> None:
        # Normalize *before* the field validators run (Codex review of PR
        # 1, P2) — ``Model.full_clean()`` calls ``clean_fields()`` (which
        # runs ``hostname_suffix``'s own ``validate_dns_label`` validator)
        # *before* it calls ``clean()`` (Django's own ordering, not this
        # model's choice). Normalizing only in ``clean()``/``save()`` as
        # originally written left ``full_clean()`` validating the raw,
        # not-yet-normalized value — ``"  Engine  "`` failed
        # ``validate_dns_label`` here before ``clean()`` ever got a chance
        # to strip/lowercase it, contradicting this field's documented
        # contract (``"MPS "`` stores ``"mps"`` rather than erroring, ADR
        # 0022 decision 4). Overriding ``clean_fields()`` rather than
        # dropping the reusable validator keeps ``validate_dns_label`` as
        # real, reusable validation metadata on the field (phase 18 reuses
        # it) instead of hand-rolling the same check again in ``clean()``.
        # ``clean()``'s own strip/lower below stays — this covers
        # ``full_clean()``; that one covers a direct ``.clean()`` call.
        if self.hostname_suffix:
            self.hostname_suffix = self.hostname_suffix.strip().lower()
        super().clean_fields(exclude=exclude)

    def clean(self) -> None:
        super().clean()
        if self.hostname_suffix:
            self.hostname_suffix = self.hostname_suffix.strip().lower()
        if self._profile_locked() and not self._hostname_suffix_only_edit():
            raise ValidationError(
                "This profile's ports are locked because it already has device instances; "
                "create a new named profile to change the port layout."
            )
        self._assign_ordinal_if_unset()

    def _hostname_suffix_only_edit(
        self, *, update_fields: "list[str] | frozenset[str] | None" = None
    ) -> bool:
        """Whether this write to an *existing* row can be exempted from the
        profile lock — ADR 0022 decision 4. A derived port label has no
        materialized counterpart to disagree with, so it must stay fixable
        without creating a new named profile for every device of this type.

        Compares **every concrete field except ``hostname_suffix``**
        against the persisted row — introspected from
        ``NetworkDeviceTypePort._meta.concrete_fields`` rather than
        hand-listed (Codex review of PR 1, P1: the original version listed
        eight fields and silently omitted ``created_by``/``created_at``,
        which let ``port.created_by = user;
        port.save(update_fields=["created_by"])`` slip through the lock on
        a locked row with no ``hostname_suffix`` write involved at all — a
        hand-picked list can't help but omit a field nobody thought to
        name; introspection can't). ``pk``/``id`` is excluded since a row
        never meaningfully changes its own primary key.

        Honors ``update_fields`` exactly as ``_check_locked_fields_
        unchanged()`` does (the identical normalization, since Django's
        ``update_fields`` accepts a field's name or its attname): a field
        this specific ``save()`` call excludes can't be the thing that
        smuggles an edit through, regardless of what's dirty in memory on
        the in-memory instance for it. ``clean()`` has no
        ``update_fields`` of its own to pass (``Model.clean()`` is never
        given one), so it implicitly compares the full row, matching every
        other ``clean()``-time lock check in this module.

        ``False`` for a brand new row (``self.pk is None``) — the
        exemption is for editing an already-materialized port, never for
        adding or removing one. Compares against the *persisted* row, not
        the in-memory one, so a write can't smuggle another field's edit
        through by also touching ``hostname_suffix`` in the same call —
        this is the first exemption the profile lock has ever had (plan
        Risks section), and both this method and the admin inline's
        ``has_change_permission()``/``get_readonly_fields()`` must agree
        on it.

        Known gap (documented, not closed, and not new to this exemption):
        ``QuerySet.update()``/``bulk_create()`` bypass ``Model.save()``
        entirely and are unguarded by this — the same pre-existing gap
        ``_check_locked_fields_unchanged()`` already documents for every
        other locked field on this and every other model in this module.
        """
        if self.pk is None:
            return False
        attname_to_name = {
            field.attname: field.name
            for field in NetworkDeviceTypePort._meta.concrete_fields
            if field.name not in ("id", "hostname_suffix")
        }
        normalized_update_fields = _normalize_update_fields(NetworkDeviceTypePort, update_fields)
        if normalized_update_fields is not None:
            attname_to_name = {
                attname: name for attname, name in attname_to_name.items() if name in normalized_update_fields
            }
        if not attname_to_name:
            return True  # this write touches none of the compared fields at all
        persisted = NetworkDeviceTypePort._default_manager.filter(pk=self.pk).values(*attname_to_name).first()
        if persisted is None:
            return False
        return all(getattr(self, attname) == value for attname, value in persisted.items())

    def _profile_locked(self) -> bool:
        device_type = _get_related(self, "device_type")
        return device_type is not None and device_type.pk is not None and device_type.devices.exists()

    def _persisted_device_type_id(self) -> int | None:
        if self.pk is None:
            return None
        return (
            NetworkDeviceTypePort._default_manager.filter(pk=self.pk)
            .values_list("device_type_id", flat=True)
            .first()
        )

    def _persisted_profile_locked(self, original_device_type_id: int | None) -> bool:
        """Whether this row's *persisted* parent (before any in-memory
        reassignment) is locked — see ``NetworkSwitchTypePort``'s version
        for why ``_profile_locked()`` alone isn't enough. Takes the
        persisted parent id explicitly — every caller already needs it for
        ``_lock_type_rows()``.
        """
        if original_device_type_id is None:
            return False
        return NetworkDeviceType._default_manager.filter(
            pk=original_device_type_id, devices__isnull=False
        ).exists()

    def _assign_ordinal_if_unset(self, *, force: bool = False) -> None:
        # ``_state.adding``, not ``self.pk is not None`` — see
        # ``NetworkSwitch.save()`` for why a pre-assigned pk (fixtures,
        # scripted inserts) must not be mistaken for an existing row.
        if not self._state.adding or (self.ordinal and not force):
            return
        device_type = _get_related(self, "device_type")
        if device_type is None or device_type.pk is None:
            return
        existing_max = device_type.type_ports.aggregate(models.Max("ordinal"))["ordinal__max"] or 0
        self.ordinal = existing_max + 1


class NetworkDevice(RackSlotAssignmentMixin, AuditedModel):
    """An end-point device instance. Unracked (rack is null) = spare pool.

    ``device_type`` is fixed at creation — see ``NetworkSwitch`` for why
    (ADR 0010): re-typing a device (e.g. adding a Dante card to an amp)
    means removing and recreating it, not editing this field. This is
    expected to be rare (DESIGN.md's "Concrete Device Examples").
    """

    device_type = models.ForeignKey(NetworkDeviceType, on_delete=models.PROTECT, related_name="devices")
    hostname = models.CharField(max_length=63, blank=True)
    serial_number = models.CharField(max_length=100, blank=True)
    rack = models.ForeignKey(Rack, on_delete=models.PROTECT, null=True, blank=True, related_name="devices")
    rack_slot = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])
    owner = models.ForeignKey(
        Owner,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="devices",
        help_text="Hostname component 1 (ADR 0023) — defaults from this device's rack at creation.",
    )
    hostname_purpose = models.CharField(
        max_length=63,
        blank=True,
        validators=[validate_dns_label],
        help_text='Hostname component 4, e.g. "sub" or "midhi-01-04" — a non-numeric qualifier '
        "belongs here, not in the sequence below (ADR 0023).",
    )
    hostname_sequence = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Hostname component 5 — an integer distinguishing otherwise-identical names, "
        "e.g. 1 or 2 for mps-avio-aes-1 and mps-avio-aes-2 (ADR 0023).",
    )
    dante_unit_id = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(dante.DANTE_UNIT_ID_MIN), MaxValueValidator(dante.DANTE_UNIT_ID_MAX)],
        help_text=(
            "Yamaha consoles find and control this device by this number. Must be unique across "
            "every Yamaha-controlled device on the network — stage boxes and wireless receivers "
            "share one range. 1–127. Leave blank for equipment that is not controlled by a Yamaha "
            "console, including the consoles themselves."
        ),
    )
    host = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="installed_cards",
        help_text=(
            "The device this add-in card is currently fitted inside, if any (ADR 0022). A soft "
            "'currently fitted to' pointer with no addressing meaning whatsoever — it does not "
            "derive an address, constrain a VLAN, share an ordinal, or move anything. A pulled "
            "card keeps its own rack slot and addresses. Only meaningful when this device's own "
            "type is an add-in card; fitting happens through the dedicated 'Fit a card' flow, "
            "never this field directly."
        ),
    )

    #: Class-level default for the ``port_addressing`` property below —
    #: never a plain class attribute, since Django's ``Model.__init__``
    #: only accepts unknown kwargs (``objects.create(port_addressing=...)``)
    #: when the name is a field or a property (ADR 0013).
    _port_addressing: str = PortAddressing.STATIC

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["rack", "rack_slot"], name="unique_device_rack_slot"),
            models.CheckConstraint(
                condition=models.Q(rack_slot__isnull=True) | models.Q(rack_slot__gte=1),
                name="networkdevice_rack_slot_gte_1",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(rack__isnull=True, rack_slot__isnull=True)
                    | models.Q(rack__isnull=False, rack_slot__isnull=False)
                ),
                name="networkdevice_rack_and_slot_together",
            ),
            # ADR 0022 PR 3, decision 6's self-host edge was *planned* as a
            # database CheckConstraint (~Q(host=F("id"))) — cheap, and the
            # one of the three edges that depends only on this row's own
            # columns rather than a related row's. It isn't one: MariaDB
            # categorically refuses any CHECK constraint that references an
            # AUTO_INCREMENT column (error 1901, "Function or expression
            # 'AUTO_INCREMENT' cannot be used in the CHECK clause of `id`"),
            # verified directly against this project's MariaDB with a bare
            # `CHECK (id > 0)` on an unrelated table — not a comparison
            # quirk, a blanket refusal to let `id` appear in a CHECK at all.
            # All three edges — including this one — are therefore enforced
            # only in _check_host_invariants(), called unconditionally from
            # both save() and clean() so objects.create() is covered too;
            # this narrows to the same "ORM-only, not DB-level" posture the
            # other two edges already had, and to the same documented gap
            # models.py:203-206 already names for a raw bulk write that
            # bypasses save() entirely.
            # ADR 0024 plan settled decision 4 — a backstop for paths that
            # never call full_clean() (objects.create(), the recompute
            # action's save(), any future importer), on top of
            # _clean_dante_fields()'s plain-language error below. Safe
            # unconditionally: the column is born entirely null and
            # MariaDB does not collide NULLs in a unique index, the same
            # property unique_device_rack_slot already relies on over
            # nullable rack/rack_slot.
            models.UniqueConstraint(fields=["dante_unit_id"], name="unique_device_dante_unit_id"),
            # ADR 0024 plan settled decision 5 — matches
            # networkdevice_rack_slot_gte_1's shape rather than inventing
            # a second enforcement posture on the same model.
            models.CheckConstraint(
                condition=(
                    models.Q(dante_unit_id__isnull=True)
                    | models.Q(dante_unit_id__range=(dante.DANTE_UNIT_ID_MIN, dante.DANTE_UNIT_ID_MAX))
                ),
                name="networkdevice_dante_unit_id_range",
            ),
        ]
        ordering = ["hostname"]

    def __str__(self) -> str:
        return self.hostname or f"Device #{self.pk}"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        # Stripped/lowercased here too, not just clean() — Model.save()
        # never calls clean() (Department.save()'s reasoning). hostname
        # joins hostname_purpose here (ADR 0023 decision 8, amended) — see
        # NetworkSwitch.save()'s identical comment.
        if self.hostname:
            self.hostname = self.hostname.strip().lower()
        if self.hostname_purpose:
            self.hostname_purpose = self.hostname_purpose.strip().lower()
        # ``self.pk is None or self._state.adding`` — see NetworkSwitch.save().
        is_new = self.pk is None or self._state.adding
        with transaction.atomic():
            if not is_new:
                _check_locked_fields_unchanged(
                    NetworkDevice, self.pk, self._locked_fields(), update_fields=update_fields
                )
            elif self.device_type_id is not None:
                # Locks the type row so a concurrent edit to its port
                # templates/count can't interleave with this materialization.
                _lock_type_rows(NetworkDeviceType, self.device_type_id)
                # Re-read from the DB, not the cached relation this
                # instance loaded earlier (Codex review round 5,
                # finding 2) — locking a row and then continuing to
                # trust a stale in-memory copy of it defeats the point
                # of the lock.
                self.device_type = NetworkDeviceType._default_manager.get(pk=self.device_type_id)
            self._check_host_invariants()
            try:
                super().save(
                    force_insert=force_insert,
                    force_update=force_update,
                    using=using,
                    update_fields=update_fields,
                )
                if is_new:
                    self._materialize_ports()
            except Exception:
                if is_new and self._state.adding is False and self.pk is not None:
                    # The atomic block below still rolls back the INSERT
                    # above, but Django has already set self.pk and
                    # cleared self._state.adding on this in-memory object
                    # — a DB rollback doesn't undo those Python attributes
                    # (Codex review round 5, finding 1). Left as-is, a
                    # caller that catches this, fixes whatever was wrong,
                    # and retries the *same* instance would compute
                    # is_new = False on the next call, take the update
                    # path, have its UPDATE affect zero rows (the row was
                    # rolled back), fall back to Django's own zero-row-
                    # UPDATE→INSERT behavior, and skip materialization.
                    # Restored so a retry takes the creation path again,
                    # in full, exactly as a fresh instance would.
                    self.pk = None
                    self._state.adding = True
                raise

    def clean_fields(self, exclude=None) -> None:
        # Normalize *before* the field validators run — see
        # Owner.clean_fields() for why this can't wait for clean().
        if self.hostname:
            self.hostname = self.hostname.strip().lower()
        if self.hostname_purpose:
            self.hostname_purpose = self.hostname_purpose.strip().lower()
        super().clean_fields(exclude=exclude)

    def clean(self) -> None:
        super().clean()
        if self.hostname:
            self.hostname = self.hostname.strip().lower()
        if self.hostname_purpose:
            self.hostname_purpose = self.hostname_purpose.strip().lower()
        if self.pk is None or self._state.adding:
            device_type = _get_related(self, "device_type")
            if device_type is not None and device_type.pk is not None:
                _validate_device_type_port_profile(device_type)
                if self._materializes_static():
                    self._check_static_materialization_possible()
        else:
            _check_locked_fields_unchanged(NetworkDevice, self.pk, self._locked_fields(), update_fields=None)
            # Rename-only (ADR 0023 decision 6, amended twice) — see
            # NetworkSwitch.clean()'s identical comment.
            stored_hostname = _stored_hostname(NetworkDevice, self.pk)
            if stored_hostname is not None and self.hostname != stored_hostname:
                _validate_hostname_unique(self.hostname, exclude_switch_pk=None, exclude_device_pk=self.pk)
        self._check_host_invariants()
        self._clean_dante_fields()

    def _clean_dante_fields(self) -> None:
        """The Dante block of ``clean()`` (ADR 0024 plan settled decisions
        4 and 5) — uniqueness and the 31-character assembled-name limit,
        both gated on ``dante_unit_id`` being set (a blank ID is exempt
        from both, decisions 1/2's "opt-in" framing), accumulated into
        **one** error dict so an operator who has both problems in a
        single submission sees both rather than being told about the
        first and, on their next attempt, the second.

        Uniqueness excludes ``self.pk`` only when it is not ``None``,
        mirroring ``_validate_hostname_unique()`` rather than leaning on
        ``exclude(pk=None)``'s isnull rewrite — and, unlike that rule,
        runs on **creation as well as rename**: no importer ever writes a
        unit ID, so there is no bulk rebuild this could break by
        enforcing unconditionally (decision 3).
        """
        if self.dante_unit_id is None:
            return
        errors: dict[str, list[str]] = {}
        conflicts = NetworkDevice.objects.filter(dante_unit_id=self.dante_unit_id)
        if self.pk is not None:
            conflicts = conflicts.exclude(pk=self.pk)
        conflict = conflicts.first()
        if conflict is not None:
            errors["dante_unit_id"] = [f"Dante unit ID {self.dante_unit_id} is already used by {conflict}."]
        # Raised on the hostname field (decision 2) — it lands where the
        # operator is typing, not on the field that merely opted them in.
        message = dante.length_error(self.dante_unit_id, self.hostname)
        if message is not None:
            errors["hostname"] = [message]
        if errors:
            raise ValidationError(errors)

    @property
    def dante_device_name(self) -> str | None:
        """The name to set in Dante Controller (ADR 0024). Read-only and
        never stored: a stored copy would have nothing keeping it in step
        with the hostname it's built from (ADR 0022 decision 4's
        reasoning, applied to a second derived field). Delegates to
        ``inventory.dante.dante_device_name()``, the one place this
        derivation is computed.
        """
        return dante.dante_device_name(self.dante_unit_id, self.hostname)

    @property
    def hostname_diverges(self) -> bool:
        """See ``NetworkSwitch.hostname_diverges`` — identical shape, over
        ``device_type`` rather than ``switch_type``.
        """
        if not self.hostname:
            return False
        owner = _get_related(self, "owner")
        rack = _get_related(self, "rack")
        device_type = _get_related(self, "device_type")
        # ADR 0026 PR 2 — hostname_slug moved from device_type onto its
        # device_model FK, so this reads one hop further than
        # NetworkSwitch.hostname_diverges' switch_type.hostname_slug.
        device_model = _get_related(device_type, "device_model") if device_type is not None else None
        computed = _assemble_hostname_stem(
            owner_slug=owner.slug if owner is not None else None,
            location_slug=rack.location_slug if rack is not None else None,
            type_slug=device_model.hostname_slug if device_model is not None else None,
            purpose=self.hostname_purpose,
            sequence=self.hostname_sequence,
        )
        return computed is not None and computed != self.hostname

    @property
    def port_addressing(self) -> str:
        """Creation-time-only choice of DHCP vs. static port materialization
        (ADR 0013). Never stored — the materialized ``NetworkDevicePort``
        rows are the record of what was chosen; setting this after creation
        has no effect since ``_materialize_ports()`` only runs once.
        """
        return self._port_addressing

    @port_addressing.setter
    def port_addressing(self, value: str) -> None:
        if value not in PortAddressing.values:
            raise ValidationError(
                f"{value!r} is not a valid port_addressing — must be one of {PortAddressing.values}."
            )
        self._port_addressing = value

    @property
    def slot_span(self) -> int:
        """Delegates to ``device_type.slot_span`` (ADR 0017) — overrides
        ``RackSlotAssignmentMixin``'s default of 1. Reads ``device_type``
        via ``_get_related()`` so an unsaved device with no type assigned
        yet still cleans (mirrors the same defensive pattern used
        throughout this module for a possibly-unset FK on an in-progress
        instance). See ``NetworkDeviceType.slot_span`` — kept only for the
        ``slot_count`` bound; see ``claimed_offsets`` below for occupancy.
        """
        device_type = _get_related(self, "device_type")
        if device_type is None or device_type.pk is None:
            return 1
        return device_type.slot_span

    @property
    def claimed_offsets(self) -> frozenset[int]:
        """Delegates to ``device_type.claimed_offsets`` (ADR 0027) —
        ``{0}`` for an unsaved device with no type assigned yet, mirroring
        ``slot_span``'s identical defensive fallback above.
        """
        device_type = _get_related(self, "device_type")
        if device_type is None or device_type.pk is None:
            return frozenset({0})
        return device_type.claimed_offsets

    def _materializes_static(self) -> bool:
        """Whether this (not-yet-materialized) device will get static
        addresses — only when racked (decision 3: unracked is always DHCP,
        spare pool by definition) and the static choice is in effect
        (ADR 0013).
        """
        return self.rack is not None and self.port_addressing == PortAddressing.STATIC

    def _check_static_materialization_possible(self) -> None:
        """Pre-flight over ``self.device_type``'s Network Device Type Ports
        for whether static materialization can succeed — pure, needs no
        device pk, so it can run from both ``clean()`` (admin form errors)
        and ``_materialize_ports()`` (the ``objects.create()`` path, which
        never calls ``clean()``).

        Skips L2-only-VLAN ports entirely (decision 4: those always
        materialize DHCP and that's not a failure). Of what's left, ports
        are grouped by ``(vlan, slot_offset)``: any VLAN shared by more
        than one port **at the same slot_offset** can't be addressed by
        ``suggest_slot_address()``'s one-address-per-(slot, VLAN) model
        (decision 5, Switched Mode devices — ADR 0017 narrows this from
        "same VLAN" to "same VLAN and same offset", so it still catches
        Switched Mode but not two ports sharing a VLAN at different
        offsets — a Yamaha console's Dante Primary and Device Control, ADR
        0027 decision 3, retiring ADR 0022's ``OPERATOR`` exemption for
        exactly this case), and each remaining port's suggested address
        must actually be usable (``_validate_static_address``).

        Every candidate address is also checked against every other
        candidate on **this same device** as it's produced (Codex review
        of PR 1, P1) — two ports whose independently-derived addresses
        happen to coincide would otherwise sail past ``_validate_static_
        address()``'s uniqueness check, since neither row exists in the
        database yet for that check to find. Without this, the pre-flight
        passes, the admin form validates, and materialization fails
        partway through on the second port — an inconsistent, confusing
        failure this catches in one pass instead, before either address is
        chosen as final.

        Also enforces the ``.255`` bound here (ADR 0017 plan review, note
        3), not only in ``RackSlotAssignmentMixin.clean()`` — this method
        runs on the ``objects.create()`` path via ``_materialize_ports()``,
        which never calls ``clean()`` at all, so without a copy of the
        bound here a device created directly past a rack's slot_count
        would still materialize an address that reads as that block's
        broadcast address (see ``required_block_size()``/ADR 0015).
        """
        addressable = [tp for tp in self.device_type.type_ports.select_related("vlan") if tp.vlan.subnet]
        by_vlan_offset: dict[tuple[int, int], list[NetworkDeviceTypePort]] = {}
        for type_port in addressable:
            by_vlan_offset.setdefault((type_port.vlan_id, type_port.slot_offset), []).append(type_port)
        for type_port_group in by_vlan_offset.values():
            if len(type_port_group) > 1:
                names = ", ".join(tp.description for tp in type_port_group)
                raise ValidationError(
                    f"{self.device_type} has more than one port on {type_port_group[0].vlan} "
                    f"({names}) — static materialization needs one address per VLAN, and this "
                    "device has no way to give one address to all of them. Use DHCP for this device."
                )
        if self.rack is not None and self.rack_slot is not None:
            span = self.device_type.slot_span
            if self.rack_slot + span - 1 > self.rack.slot_count:
                raise ValidationError(
                    f"rack_slot {self.rack_slot} plus {self.device_type}'s span ({span}, ending "
                    f"at ordinal {self.rack_slot + span - 1}) exceeds {self.rack}'s slot_count "
                    f"({self.rack.slot_count})."
                )
        # (vlan_id, address) -> the type port already claiming it, so a
        # later candidate that collides with an earlier one in this same
        # pass is caught here rather than left for the DB uniqueness check
        # neither row has been written for yet.
        proposed: dict[tuple[int, str], NetworkDeviceTypePort] = {}

        def _check_no_self_collision(type_port: "NetworkDeviceTypePort", address: str) -> None:
            key = (type_port.vlan_id, address)
            earlier = proposed.get(key)
            if earlier is not None:
                raise ValidationError(
                    f"{earlier.description!r} and {type_port.description!r} would both be given "
                    f"{address} on {type_port.vlan} — {self.device_type} can't materialize two "
                    "ports onto the same address."
                )
            proposed[key] = type_port

        for type_port in addressable:
            address = _suggest_rack_slot_address(
                self.rack, self.rack_slot, type_port.vlan_id, type_port.slot_offset
            )
            if address is None:
                raise ValidationError(
                    f"No usable address range for {type_port.vlan} in {self.rack} — assign a "
                    f"Rack VLAN Range for this VLAN before creating a static {self.device_type} "
                    "device here, or use DHCP."
                )
            _check_no_self_collision(type_port, address)
            try:
                _validate_static_address(
                    address,
                    type_port.vlan,
                    self.rack,
                    self.rack_slot,
                    exclude_switch_address_pk=None,
                    exclude_device_port_pk=None,
                )
            except ValidationError as exc:
                # _validate_static_address() raises keyed on "address" — the
                # right shape for NetworkDevicePort.clean() (which has that
                # field), but this call site is NetworkDevice.clean(), which
                # doesn't. A dict-keyed ValidationError for a nonexistent
                # form field crashes Django's admin add_error() with a raw
                # ValueError instead of rendering a form error, so re-raise
                # as a plain, non-field error here.
                raise ValidationError(exc.messages) from exc

    def _derive_addresses(self, ports: "Iterable[NetworkDevicePort]") -> None:
        """Sets ``port.address`` from ``range_base + self.rack_slot +
        port.slot_offset`` for every static port in ``ports`` (ADR 0027) —
        the single formula every static address in this system now goes
        through, replacing both ADR 0017's offset-0-derives-then-cascades
        shape (``NetworkDevicePort._derive_offset_siblings()``, deleted)
        and the per-port fill-if-blank suggestion that used to live in
        ``NetworkDevicePort.clean()``. Each port derives independently
        from this device's own slot, so — unlike the cascade it replaces —
        no offset-0-first ordering is required among ``ports``.

        A DHCP port in ``ports`` is left untouched (its ``address`` stays
        whatever it already was — ``None``, materialized elsewhere).
        Callers are responsible for persisting each mutated port
        afterward — this only computes and assigns the value, matching
        every other suggestion helper in this module (``_suggest_rack_
        slot_address`` itself included) staying a pure setter of state,
        not a writer.

        Raises ``ValidationError``, naming the VLAN, if no usable rack
        VLAN range exists for a static port's VLAN — reachable only via a
        path that skipped ``_check_static_materialization_possible()``'s
        own identical check (a bypassed ``objects.create()``), matching
        that method's own error text.
        """
        for port in ports:
            if port.is_dhcp:
                continue
            address = _suggest_rack_slot_address(self.rack, self.rack_slot, port.vlan_id, port.slot_offset)
            if address is None:
                raise ValidationError(
                    f"No usable address range for {port.vlan} in {self.rack} — assign a Rack "
                    f"VLAN Range for this VLAN before deriving {self}'s addresses."
                )
            port.address = address

    def _materialize_ports(self) -> None:
        """One-time copy of ``device_type``'s Network Device Type Ports into
        real ``NetworkDevicePort`` rows — static by default when racked, or
        DHCP when unracked or explicitly chosen (ADR 0013, revising ADR
        0010's always-DHCP rule). Runs inside the same transaction as this
        device's insert (see ``save()``), so any failure here rolls back
        the device and every port materialized before it.

        Each port's ``slot_offset`` is copied from its type port (ADR
        0017); every static port's ``address`` is computed by
        ``_derive_addresses()`` (ADR 0027) before any of them are written
        — one whole-device pass rather than the derive-on-edit cascade
        (``NetworkDevicePort._derive_offset_siblings``, deleted) this
        replaces, so no offset-0-first ordering is required and the loop
        below stays ordered by ``ordinal`` as it always has.
        """
        _validate_device_type_port_profile(self.device_type)
        static = self._materializes_static()
        if static:
            self._check_static_materialization_possible()
        pending: list[NetworkDevicePort] = []
        for type_port in self.device_type.type_ports.select_related("vlan").order_by("ordinal"):
            if static and type_port.vlan.subnet:
                pending.append(
                    NetworkDevicePort(
                        device=self,
                        port_number=type_port.port_number,
                        description=type_port.description,
                        vlan=type_port.vlan,
                        port_type=type_port.port_type,
                        ordinal=type_port.ordinal,
                        slot_offset=type_port.slot_offset,
                        source_type_port=type_port,
                        is_dhcp=False,
                        address=None,
                        created_by=self.created_by,
                    )
                )
            else:
                # DHCP path — either the overall choice, or an L2-only VLAN
                # under a static choice (decision 4), which always
                # materializes DHCP and isn't a failure.
                NetworkDevicePort.objects.create(
                    device=self,
                    port_number=type_port.port_number,
                    description=type_port.description,
                    vlan=type_port.vlan,
                    port_type=type_port.port_type,
                    ordinal=type_port.ordinal,
                    slot_offset=type_port.slot_offset,
                    source_type_port=type_port,
                    is_dhcp=True,
                    address=None,
                    created_by=self.created_by,
                )
        self._derive_addresses(pending)
        for port in pending:
            port.full_clean()
            port.save()

    def _check_rack_slot_not_occupied(self) -> None:
        """ADR 0027: overlap is checked against the exact **set** of
        ordinals each occupant claims (``claimed_offsets``), not a
        contiguous span — the write-path half of the fix for #83, whose
        whole point is that a device with offsets ``{0, 64}`` must not
        block the 63 ordinals between them for some other occupant.

        The queries below still narrow candidates to those whose own
        contiguous *envelope* (``rack_slot .. rack_slot + slot_span - 1``)
        could possibly overlap mine — cheap, and sufficient as a
        prefilter, since any *actual* claimed ordinal is necessarily
        inside that envelope — and the exact, set-based check happens in
        Python afterward, the same "SQL can bound, only Python can express
        a set" split ``occupied_rack_slot_ordinals()`` documents.

        ``claimed_offsets`` for every candidate is resolved once, in bulk,
        before the loop (``_bulk_claimed_offsets()``) rather than read
        off each candidate's own property — that property recomputes on
        every access, so reading it per candidate here is an N+1 (ADR 0027
        PR 1 review; measured at 5 queries flat on ``main`` vs. 39 on this
        branch for a 39-device rack before this fix).
        """
        if self.rack_slot is None:
            return  # only ever called from clean() once rack/rack_slot are both set
        my_ordinals = {self.rack_slot + offset for offset in self.claimed_offsets}
        my_start = min(my_ordinals)
        my_end = max(my_ordinals)
        switch_conflict = NetworkSwitch.objects.filter(rack=self.rack, rack_slot__in=my_ordinals).first()
        if switch_conflict is not None:
            raise ValidationError(
                f"Rack slot {switch_conflict.rack_slot} in {self.rack} is already occupied by a switch."
            )
        # Devices only — unique(rack, rack_slot) already catches an equal
        # starting ordinal at the DB level; this catches the case that
        # constraint can't: another device's claim overlapping ours
        # without sharing a starting ordinal (a device at 7 with offsets
        # {0, 1} occupying 7-8, a new one at 8). Annotates every other
        # device's own end ordinal from its type's slot_span (a switch
        # always spans 1, so only this side needs the aggregate — plan
        # review note 6) to narrow candidates cheaply in SQL; the exact,
        # ordinal-exact test happens in Python via set intersection.
        candidates = list(
            NetworkDevice.objects.filter(rack=self.rack, rack_slot__lte=my_end)
            .exclude(pk=self.pk)
            .annotate(_span=Coalesce(models.Max("device_type__type_ports__slot_offset"), 0) + 1)
            .annotate(_end=models.F("rack_slot") + models.F("_span") - 1)
            .filter(_end__gte=my_start)
            .select_related("device_type")
        )
        claimed_offsets = _bulk_claimed_offsets(candidate.device_type_id for candidate in candidates)
        for candidate in candidates:
            assert candidate.rack_slot is not None  # DB constraint: rack and rack_slot are all-or-neither
            offsets = claimed_offsets.get(candidate.device_type_id, frozenset({0}))
            other_ordinals = {candidate.rack_slot + offset for offset in offsets}
            overlap = sorted(my_ordinals & other_ordinals)
            if overlap:
                raise ValidationError(
                    f"Rack slot(s) {overlap} in {self.rack} "
                    f"{'is' if len(overlap) == 1 else 'are'} already occupied by {candidate} "
                    f"(claiming {sorted(other_ordinals)})."
                )

    def _validate_existing_addresses_still_fit(self) -> None:
        for port in self.ports.filter(address__isnull=False):
            if port.address is None:
                continue  # filtered out above; satisfies mypy
            if self.rack is None:
                raise ValidationError(
                    f"Cannot unrack {self}: it still has a static address ({port.address} on "
                    f"{port.vlan}); remove or reassign its addresses first."
                )
            error = _address_containment_error(port.address, port.vlan, self.rack, self.rack_slot)
            if error:
                raise ValidationError(f"Moving {self} would leave an existing address invalid: {error}")

    def _locked_fields(self) -> dict[str, Any]:
        return {"device_type": self.device_type_id}

    def _check_host_invariants(self) -> None:
        """The three edges ADR 0022 decision 6 draws around ``host`` —
        enforced here, in ``save()``, as well as in ``clean()`` (called from
        both), because ``objects.create()`` never calls ``clean()`` and this
        module enforces every other invariant on the save path too.

        - A device with a ``host`` must itself be an add-in card
          (``device_type.is_add_in_card``) — the "fit a card" flow's type
          picker only ever offers card types, but a direct ORM/API write has
          no picker to lean on.
        - ``host`` must not itself be an add-in card — no nesting.
        - ``host`` may not be ``self``. This one was planned as a database
          ``CheckConstraint`` too (it depends only on this row's own
          columns, unlike the other two) — MariaDB refuses it outright, so
          it's ORM-only like its neighbours. See ``Meta.constraints``' own
          comment for the verified error.

        Cross-rack is deliberately *not* checked here — a card may sit in a
        different rack from its host (ADR 0022 decision 6) — and neither is
        a hostless card, which is simply the ordinary ``host is None`` case
        this whole method skips.

        The self-host comparison reads ``self.host_id`` — the raw column —
        rather than dereferencing ``self.host`` first (Codex review, P2). A
        crafted ``objects.create(pk=42, host_id=42, ...)`` makes ``self.host``
        resolve against a row that doesn't exist *yet* (this instance hasn't
        been inserted), so ``_get_related()`` catches the resulting
        ``DoesNotExist`` and returns ``None`` — and the old version bailed
        out at "host is None" before ever comparing IDs, letting a
        self-referencing row insert. ``host_id`` needs no dereference at all,
        so it can't be fooled by that gap.
        """
        if self.host_id is not None and self.pk is not None and self.host_id == self.pk:
            raise ValidationError("A device cannot be fitted to itself.")
        host = _get_related(self, "host")
        if host is None:
            return
        device_type = _get_related(self, "device_type")
        if device_type is not None and device_type.pk is not None and not device_type.is_add_in_card:
            raise ValidationError(
                f"{self} cannot have a host — {device_type} is not marked as an add-in card (ADR 0022)."
            )
        host_type = _get_related(host, "device_type")
        if host_type is not None and host_type.pk is not None and host_type.is_add_in_card:
            raise ValidationError(f"{host} is itself an add-in card and cannot host another (ADR 0022).")


def _lock_devices_by_pk(*pks: "int | None") -> "dict[int, NetworkDevice]":
    """Acquire ``SELECT ... FOR UPDATE`` locks on the given ``NetworkDevice``
    rows, in one query, ordered by ascending primary key — the deterministic
    lock order ADR 0022 PR 3's Concurrency section requires. Both the "fit a
    card" admin view and ``_clear_installed_cards_before_delete()`` below use
    this, so the two can never deadlock against each other: whichever
    acquires a contested host row first wins, and the other observes the
    outcome once its own lock is granted. Must run inside
    ``transaction.atomic()``.

    Returns only the rows that still exist, keyed by pk — a caller tells
    "locked" apart from "already gone" (e.g. deleted by a transaction that
    won the race) by membership, not by a placeholder ``None``.
    """
    ids = sorted({pk for pk in pks if pk is not None})
    if not ids:
        return {}
    return {
        obj.pk: obj
        for obj in NetworkDevice._default_manager.select_for_update().filter(pk__in=ids).order_by("pk")
    }


def _clear_installed_cards_before_delete(
    sender: type[models.Model], instance: "NetworkDevice", using: "str | None", **kwargs: Any
) -> None:
    """Clears every add-in card fitted to ``instance`` through an ordinary,
    audited ``save()`` per row, before the deletion collector's own
    ``QuerySet.update()`` would otherwise silently null them out with no
    trace on the card's own history (ADR 0022 PR 3's Audit section).

    Django's deletion collector clears ``SET_NULL`` reverse FKs (every
    ``NetworkDevice`` whose ``host`` is ``instance``) with a bulk
    ``update()`` that bypasses ``Model.save()`` and therefore every
    auditlog signal — adding ``host`` to ``AUDITLOG_INCLUDE_TRACKING_
    MODELS`` is necessary but not sufficient. This fires from ``pre_delete``,
    which the collector sends *before* it performs its own field-clearing
    and row-deletion steps, and inside the same atomic block the collector
    already wraps its own work in (see
    ``django.db.models.deletion.Collector.delete()``) — and, because Django
    sends this signal per object for **every** deletion path (instance
    ``.delete()``, a queryset ``.delete()``, and the admin's bulk delete
    action all route through the same ``Collector``), one receiver covers
    all three without a custom manager/queryset on this model.

    Locks ``instance`` itself *first* — a single-row lock, via the same
    ``_lock_devices_by_pk()`` the fit view uses — and only *then* reads
    which cards currently point to it (Codex review, P1; the earlier
    version read the card list with a plain, unlocked query *before*
    taking any lock, so a concurrent fit that hadn't committed yet was
    invisible to it: the receiver would go on to lock the host, the fit
    would then commit, and Django's own lazy ``SET_NULL`` field-update
    step — not this method — would be the one to clear that card, with no
    audited save at all).

    Once the host lock is held, no concurrent fit can complete a *new*
    assignment onto this host — fitting always locks the host row too, as
    part of the same combined ``_lock_devices_by_pk(host_pk, card_pk)``
    call — so it is either already committed and visible to the read below,
    or blocked on this same lock and cannot be. That read is deliberately
    a plain, unlocked ``filter()`` rather than a second ``_lock_devices_by_
    pk()`` call spanning the discovered cards: two *separate* multi-row
    lock acquisitions (host, then cards) can deadlock against the fit
    view's *single* combined one whenever a card's pk sorts below the
    host's — the fit call would lock the card first while waiting on the
    host this method already holds, while this method waits on the card the
    fit call now holds. Each card found here is instead cleared through its
    own ordinary ``save()``, whose implicit per-row lock only ever contends
    with another writer of *that* row, never with a blocked multi-row
    acquisition.
    """
    with transaction.atomic(using=using):
        locked_host = _lock_devices_by_pk(instance.pk).get(instance.pk)
        if locked_host is None:
            return  # already gone — a concurrent delete won the race
        card_pks = list(
            NetworkDevice._default_manager.filter(host_id=instance.pk).values_list("pk", flat=True)
        )
        for card in NetworkDevice._default_manager.filter(pk__in=card_pks):
            if card.host_id != instance.pk:
                continue  # a concurrent Pull already cleared it
            card.host = None
            card.save()


models.signals.pre_delete.connect(_clear_installed_cards_before_delete, sender=NetworkDevice)


class NetworkDevicePortQuerySet(models.QuerySet):
    """Blocks bulk ``QuerySet.delete()`` from leaving a device's ports out
    of step with its type's declared port profile — the model's own
    ``delete()`` override below only guards a single ``instance.delete()``;
    a queryset delete bypasses ``Model.delete()`` for every row, the same
    reason ``NetworkDeviceTypePortQuerySet``/``NetworkSwitchTypePortQuerySet``
    already carry their own ``delete()`` override alongside the model's.

    Originally (ADR 0017) this protected an offset sibling's derivation —
    it read its address from the offset-0 row on its VLAN, so deleting
    that row would strand it. Under ADR 0027 every port derives
    independently from the *device's own* rack slot, not from a sibling
    port, so that reasoning no longer holds. What the guard still protects
    (see the model's own ``delete()`` for the full account): ``_validate_
    device_type_port_profile()`` requires every offset-carrying VLAN on
    the device's *type* to also carry an offset-0 port, and nothing
    revalidates that shape on an *instance* after a delete — dropping the
    offset-0 row while its offset sibling(s) survive would leave a live
    device silently violating its own type's declared profile.

    Unlike the model's ``delete()``, this has no in-memory-identity hole to
    guard against: ``self`` here is the queryset being deleted, so
    ``.values("device_id", "vlan_id")`` below reads straight from the
    database for whichever rows actually match the delete, not from any
    caller-supplied Python instance's (possibly tampered) attributes.

    Deliberately does **not** guard ``NetworkDevice``'s own cascade delete
    (``on_delete=CASCADE`` on ``NetworkDevicePort.device``) — Django's
    deletion ``Collector`` issues the cascaded rows' DELETE directly and
    never routes through a related model's custom manager/queryset, so
    removing a whole device (control, engine, and everything else) still
    works in one step, as it always has.
    """

    def delete(self):
        with transaction.atomic():
            offset_zero_rows = list(self.filter(slot_offset=0).values("device_id", "vlan_id"))
            for row in offset_zero_rows:
                if NetworkDevicePort._default_manager.filter(
                    device_id=row["device_id"], vlan_id=row["vlan_id"], slot_offset__gt=0
                ).exists():
                    raise ValidationError(
                        "Cannot delete an offset-0 Network Device Port while its offset "
                        "sibling(s) on this VLAN still exist — that would leave this device "
                        "violating its own type's declared port profile (ADR 0010/0017); "
                        "delete the offset ports first, or delete the whole device."
                    )
            return super().delete()


class NetworkDevicePort(AuditedModel):
    """A device port: one purpose (VLAN), one static address or DHCP.

    Materialized exactly once from the device's ``device_type`` when the
    device is first created (``NetworkDevice._materialize_ports``) —
    static by default when racked, computed rack-range-base + rack-slot +
    ``slot_offset`` (ADR 0027, generalizing ADR 0017's offset-0-vs-offset
    split to every port alike) or DHCP-configured (``is_dhcp=True``,
    ``address=None``) when unracked or explicitly chosen (ADR 0013,
    revising ADR 0010's always-DHCP rule). ``description`` (this port's
    purpose), ``vlan``, ``port_type``, and ``slot_offset`` are locked
    hardware/purpose facts copied from the type port (ADR 0010, ADR 0017).
    ``address`` is likewise locked once persisted — derived and
    system-written only (ADR 0027 decision 1; see ``_locked_fields()``),
    superseding ADR 0003. Moving the device is the only way to change an
    *existing static* port's address; the one other legal edge is the ADR
    0013 DHCP-to-static flip (``is_dhcp`` ``True`` -> ``False`` on a
    persisted row), which derives the address itself from the same
    formula rather than accepting one typed in
    (``_derive_address_on_flip_to_static()``) — the operator still never
    types an address directly. ``switch_port`` stays freely editable.
    Identity is ``(device, description)`` — ``port_number``, when present
    at all, is neither required nor unique.

    ``switch`` is not stored directly — it's redundant with (and could
    contradict) ``switch_port``, so it's derived from it via a property.
    ``switch_port`` is a one-to-one: a physical switch port can be claimed
    by at most one device port.

    Known limitation (deferred, see DESIGN.md and ADR 0010): a device port
    is always single-VLAN/single-address. Hardware where two physical jacks
    are bridged into one logical interface (e.g. Shure ULXD4Q/D "Switched"
    mode) can be tracked as two ordinary ports here, but the system won't
    stop them from being given two different addresses, which wouldn't
    reflect the real, single-IP hardware — ``slot_offset`` (ADR 0017) does
    not cover this case, and is deliberately narrower: it's for a port
    whose address is computed from the device's own rack slot at a fixed
    offset, not general multi-jack/multi-part hardware. See ADR 0017's
    scope-boundary section.
    """

    device = models.ForeignKey(NetworkDevice, on_delete=models.CASCADE, related_name="ports")
    port_number = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])
    description = models.CharField(
        max_length=255, help_text='Required — this port\'s purpose, e.g. "Dante Primary".'
    )
    vlan = models.ForeignKey(VLAN, on_delete=models.PROTECT, related_name="device_ports")
    port_type = models.CharField(
        max_length=20,
        choices=PortType.choices,
        blank=True,
        help_text="Physical hardware fact, copied from the device's type — locked after creation.",
    )
    slot_offset = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Address offset from the device's own rack slot, copied from the device's type — "
            "locked after creation. This port's address is range_base + rack_slot + slot_offset "
            "(ADR 0027) and can't be edited directly."
        ),
    )
    ordinal = models.PositiveIntegerField(editable=False, default=0)
    # Materialization always passes is_dhcp explicitly, one way or the
    # other, regardless of this default (ADR 0013) — this stays False so
    # directly-constructed ports (tests, or any future non-materialization
    # path) keep defaulting to static, matching every other static-address
    # model in this file.
    is_dhcp = models.BooleanField(default=False)
    address = models.GenericIPAddressField(protocol="IPv4", null=True, blank=True)
    switch_port = models.OneToOneField(
        NetworkSwitchPort,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="connected_device_port",
    )
    source_type_port = models.ForeignKey(
        NetworkDeviceTypePort,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="materialized_ports",
        help_text="Provenance only — never used to re-derive this port's fields (seed-once).",
    )

    objects = NetworkDevicePortQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["device", "description"], name="unique_device_port_description"),
            models.UniqueConstraint(fields=["device", "ordinal"], name="unique_device_port_ordinal"),
            models.UniqueConstraint(fields=["vlan", "address"], name="unique_device_port_vlan_address_value"),
            models.CheckConstraint(
                condition=~models.Q(description=""), name="networkdeviceport_description_not_blank"
            ),
            models.CheckConstraint(
                condition=models.Q(port_number__isnull=True) | models.Q(port_number__gte=1),
                name="networkdeviceport_port_number_gte_1",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(is_dhcp=True, address__isnull=True)
                    | models.Q(is_dhcp=False, address__isnull=False)
                ),
                name="device_port_dhcp_xor_static_address",
            ),
        ]
        ordering = ["device", "ordinal"]

    def __str__(self) -> str:
        return f"{self.device} — {self.description}"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        with transaction.atomic():
            flipping_to_static = False
            flipping_to_dhcp = False
            if self.pk is not None:
                persisted_is_dhcp = self._persisted_is_dhcp()
                flipping_to_static = persisted_is_dhcp is True and not self.is_dhcp
                flipping_to_dhcp = persisted_is_dhcp is False and self.is_dhcp
                if flipping_to_static:
                    # ADR 0013's DHCP-to-static conversion, restored under
                    # ADR 0027 decision 1 (see the method's own docstring):
                    # derives self.address before the locked-field check
                    # below runs, and validates it here directly — a bare
                    # save() with no full_clean() is exactly the case
                    # _check_locked_fields_unchanged() itself exists for,
                    # so this can't lean on clean() having already done
                    # either half.
                    derived_address = self._derive_address_on_flip_to_static()
                    vlan = _get_related(self, "vlan")
                    device = _get_related(self, "device")
                    if vlan is not None and device is not None:
                        _validate_static_address(
                            derived_address,
                            vlan,
                            device.rack,
                            device.rack_slot,
                            exclude_switch_address_pk=None,
                            exclude_device_port_pk=self.pk,
                        )
                elif flipping_to_dhcp:
                    # The symmetric edge: ADR 0013's static-to-DHCP
                    # direction was always meant to "already clear the
                    # address" (ADR 0027 decision 1's own words), but
                    # nothing ever did — ``_locked_fields()`` locks
                    # ``address`` unconditionally and the only exemption
                    # was the opposite flip above, so this transition was
                    # dead both ways (clear it yourself and the lock
                    # rejects the change; leave it and clean() refuses a
                    # DHCP port with a static address). Clearing it here,
                    # server-side, doesn't depend on the admin form
                    # actually submitting ``None`` — the form's own
                    # ``address`` field stays ``disabled`` on every
                    # persisted row, so it always resubmits the old
                    # value regardless of which way ``is_dhcp`` flips.
                    self.address = None
                locked_fields = self._locked_fields()
                if flipping_to_static or flipping_to_dhcp:
                    # The one field this transition is allowed to change —
                    # already derived/cleared and validated above, not
                    # compared against the persisted value.
                    del locked_fields["address"]
                    if update_fields is not None and "address" not in update_fields:
                        # Either flip changes self.address in memory (derived
                        # above, or cleared) — a caller-supplied update_fields
                        # that omits it would otherwise silently drop that
                        # write, leaving is_dhcp and address disagreeing on
                        # what's persisted and tripping the DB's
                        # device_port_dhcp_xor_static_address CHECK.
                        update_fields = [*update_fields, "address"]
                _check_locked_fields_unchanged(
                    NetworkDevicePort, self.pk, locked_fields, update_fields=update_fields
                )
            # Lock both the old and new NetworkSwitchPort (connecting,
            # disconnecting, or reassigning switch_port) — the other half of
            # the race NetworkSwitchPort.save() guards against when its
            # profile changes; see _lock_switch_port_rows().
            persisted_switch_port_id = self._persisted_switch_port_id()
            _lock_switch_port_rows(self.switch_port_id, persisted_switch_port_id)

            super().save(
                force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
            )

    def delete(self, using=None, keep_parents=False) -> tuple[int, dict[str, int]]:
        # Blocks deleting an offset-0 port that still has offset siblings
        # on its VLAN. Originally (ADR 0017) this protected the sibling's
        # own derivation — it read its address from this row, and losing
        # it would leave it locked, persisted, and permanently pointed at
        # nothing. Under ADR 0027 that reasoning is stale: every port now
        # derives independently from the *device's own* rack slot, not
        # from a sibling port, so no sibling is left "pointed at nothing"
        # by this delete any more.
        #
        # What the guard still protects: _validate_device_type_port_
        # profile() requires every offset-carrying VLAN on the device's
        # *type* to also carry an offset-0 port, and nothing revalidates
        # that shape on an *instance* after a delete — dropping this row
        # while its offset sibling(s) survive would leave a live device
        # silently violating its own type's declared port profile. That's
        # a real invariant regardless of whether anything still derives
        # from this row, so the guard stays even though its original
        # justification doesn't.
        #
        # Deliberately does *not* run for a whole-device delete
        # (device.delete() cascades to every port, including these, via
        # on_delete=CASCADE) — Django's deletion Collector issues that
        # DELETE directly, bypassing both this override and
        # NetworkDevicePortQuerySet.delete() (see that queryset's
        # docstring), so removing a device still works in one step.
        #
        # Reads the *persisted* slot_offset/device_id/vlan_id
        # (_persisted_delete_guard_fields()), not self's in-memory ones —
        # see that method's docstring for why: delete() has no
        # locked-field validation at all, so self's identity fields are
        # untrusted here in a way they aren't in save()/clean().
        with transaction.atomic():
            persisted = self._persisted_delete_guard_fields()
            if (
                persisted is not None
                and persisted["slot_offset"] == 0
                and NetworkDevicePort.objects.filter(
                    device_id=persisted["device_id"], vlan_id=persisted["vlan_id"], slot_offset__gt=0
                ).exists()
            ):
                raise ValidationError(
                    "Cannot delete an offset-0 Network Device Port while its offset "
                    "sibling(s) on this VLAN still exist — that would leave this device "
                    "violating its own type's declared port profile (ADR 0010/0017); delete "
                    "the offset ports first, or delete the whole device."
                )
            return super().delete(using=using, keep_parents=keep_parents)

    def clean(self) -> None:
        super().clean()
        persisted_is_dhcp = self._persisted_is_dhcp() if self.pk is not None else None
        flipping_to_static = self.pk is not None and persisted_is_dhcp is True and not self.is_dhcp
        flipping_to_dhcp = self.pk is not None and persisted_is_dhcp is False and self.is_dhcp
        if self.pk is not None:
            locked_fields = self._locked_fields()
            if flipping_to_static or flipping_to_dhcp:
                # See save()'s identical carve-out — the ADR 0013
                # DHCP-to-static conversion, restored under ADR 0027
                # decision 1, and its symmetric static-to-DHCP sibling,
                # are the only cases allowed to change ``address`` on a
                # persisted row, and it's derived/cleared (not compared)
                # below.
                del locked_fields["address"]
            _check_locked_fields_unchanged(NetworkDevicePort, self.pk, locked_fields, update_fields=None)
        if flipping_to_dhcp:
            # The reverse of the flip below: static to DHCP was always
            # supposed to "already clear the address" (ADR 0027 decision
            # 1), but nothing did — see save()'s identical carve-out.
            self.address = None
        if self.is_dhcp:
            if self.address:
                raise ValidationError("DHCP ports must not have a static address.")
        else:
            device = _get_related(self, "device")
            vlan = _get_related(self, "vlan")
            if device is not None and device.rack is None:
                raise ValidationError(
                    "Unracked devices are spare pool (DHCP-configured per CONTEXT.md); rack "
                    "the device first, or use is_dhcp for this port instead."
                )
            if flipping_to_static:
                self._derive_address_on_flip_to_static()
            elif self.pk is None and not self.address and device is not None and vlan is not None:
                suggestion = _suggest_rack_slot_address(
                    device.rack, device.rack_slot, vlan.pk, self.slot_offset
                )
                if suggestion:
                    self.address = suggestion
            if not self.address:
                raise ValidationError("Static ports must have an address.")
            if device is not None and vlan is not None:
                _validate_static_address(
                    self.address,
                    vlan,
                    device.rack,
                    device.rack_slot,
                    exclude_switch_address_pk=None,
                    exclude_device_port_pk=self.pk,
                )

    @property
    def hostname(self) -> str | None:
        """This port's derived, read-only name in address lists — ``<device
        hostname>-<suffix>`` — where its ``source_type_port`` declares a
        non-blank ``hostname_suffix`` (ADR 0022 decision 4) *and* the
        device has a hostname of its own; ``None`` otherwise, deliberately
        never ``""`` (settled decision 5) — templates test truthiness, and
        an empty string would render as a stray ``-``. Stored nowhere;
        recomputed on every access.

        Casing is **not** normalised on this half — there is nothing left
        to normalise: the suffix is already lowercased
        (``NetworkDeviceTypePort.save()``/``clean()``), and phase 18 (ADR
        0023 decision 8, amended) now lowercases ``device.hostname`` itself
        on write *and* backfills every existing row, so this consistently
        yields ``dm7c-1-device-control``, matching the addressing sheet. No
        test here may compare this property case-sensitively regardless —
        the guarantee lives on ``hostname``, not here.
        """
        source_type_port = _get_related(self, "source_type_port")
        if source_type_port is None or not source_type_port.hostname_suffix:
            return None
        device = _get_related(self, "device")
        if device is None or not device.hostname:
            return None
        return f"{device.hostname}-{source_type_port.hostname_suffix}"

    def _locked_fields(self) -> dict[str, Any]:
        # ``device``/``port_number``/``ordinal``/``source_type_port``/
        # ``slot_offset`` identify which physical port this row represents
        # (materialized once from the device's type, ADR 0010/0017) — only
        # ``is_dhcp``/``switch_port`` are meant to be editable, so a plain
        # save() must not be able to silently move, renumber, or reorder a
        # materialized port, or change its offset.
        #
        # ``address`` is locked here unconditionally (ADR 0027 decision 1,
        # superseding ADR 0003 and generalizing ADR 0017's offset-only
        # lock to every port): a static address is derived from the
        # device's own rack slot and system-written only, so this dict
        # always compares it, regardless of ``slot_offset``. There is no
        # longer a privileged *writer* to exempt — ``_derive_offset_
        # siblings()`` and its ``_deriving_address`` flag are gone along
        # with the cascade they existed for, and with them the whole
        # reason this method used to have to read the *persisted*
        # ``slot_offset`` (``_persisted_slot_offset()``) rather than trust
        # ``self.slot_offset``: that defended against an in-memory
        # ``slot_offset`` forged to 0 being used to drop ``"address"``
        # from this dict before the comparison ever ran. With the lock
        # unconditional here, there is no conditional branch left in this
        # method for that forgery to exploit — this dict itself never
        # drops ``"address"``.
        #
        # There are exactly two privileged *transitions* that still need
        # to change it, on either side of the DHCP/static boundary: the
        # ADR 0013 DHCP-to-static flip, restored under decision 1
        # (``_derive_address_on_flip_to_static()``), and its symmetric
        # static-to-DHCP sibling, which simply clears it. Neither
        # exemption lives here — ``save()``/``clean()`` delete
        # ``"address"`` from the dict *this method returns*, after
        # deciding for themselves (by reading the persisted ``is_dhcp``
        # fresh from the database, not trusting ``self``) that one of the
        # transitions is genuinely happening. A forged ``self.is_dhcp``
        # can't manufacture that exemption on its own, since the decision
        # never reads it as final without the fresh comparison.
        return {
            "device": self.device_id,
            "port_number": self.port_number,
            "description": self.description,
            "vlan": self.vlan_id,
            "port_type": self.port_type,
            "ordinal": self.ordinal,
            "source_type_port": self.source_type_port_id,
            "slot_offset": self.slot_offset,
            "address": self.address,
        }

    def _persisted_switch_port_id(self) -> int | None:
        if self.pk is None:
            return None
        return (
            NetworkDevicePort._default_manager.filter(pk=self.pk)
            .values_list("switch_port_id", flat=True)
            .first()
        )

    def _persisted_is_dhcp(self) -> bool | None:
        """The persisted ``is_dhcp`` for this row, or ``None`` for an
        unsaved instance — what ``save()``/``clean()`` compare ``self.
        is_dhcp`` against to detect either flip across the DHCP/static
        boundary: the ADR 0013 DHCP-to-static conversion (restored under
        ADR 0027 decision 1's derive-on-flip) and its symmetric
        static-to-DHCP sibling. These are the only two edges still
        allowed to change ``address`` on a persisted row, despite
        ``_locked_fields()`` locking it unconditionally everywhere else.
        """
        if self.pk is None:
            return None
        return NetworkDevicePort._default_manager.filter(pk=self.pk).values_list("is_dhcp", flat=True).first()

    def _derive_address_on_flip_to_static(self) -> str:
        """Sets ``self.address`` (and returns the same value, so a caller
        that needs a definitely-non-``None`` ``str`` — ``save()``'s own
        immediate ``_validate_static_address()`` call, since it runs with
        no narrowing guard on ``self.address`` beforehand — doesn't have
        to re-read it off ``self``) for the ADR 0013 DHCP-to-static
        conversion, restored under ADR 0027 decision 1's system-written
        address (``_locked_fields()``): an existing row whose ``is_dhcp``
        is flipping ``True`` -> ``False``. Same ``range_base + rack_slot +
        slot_offset`` formula ``NetworkDevice._derive_addresses()``
        computes for a freshly materialized port — the operator flips the
        toggle and never types an address (the admin's own ``address``
        field is ``disabled`` for exactly this reason), so this
        unconditionally overwrites whatever ``self.address`` currently
        holds rather than trusting it.

        Derives from the *persisted* ``slot_offset``/``vlan_id``
        (fetched fresh, one query), never ``self.slot_offset``/
        ``self.vlan_id`` directly — the same untrusted-``self`` reasoning
        ``_persisted_delete_guard_fields()`` documents for the delete
        path. Both fields are normally locked (``_locked_fields()``), but
        this flip's own ``save()``/``clean()`` carve-out deletes
        ``"address"`` from that dict and nothing else — a
        ``save(update_fields=["is_dhcp", "address"])`` intersects none of
        the *remaining* locked keys, so ``_check_locked_fields_
        unchanged()`` early-returns without ever comparing ``slot_offset``
        against what's persisted. Deriving from a fresh read closes that
        hole regardless of what ``update_fields`` excludes.

        Raises ``ValidationError`` if the device is unracked — nothing to
        derive from, matching ``_suggest_rack_slot_address()``'s own
        null-rack contract — or if no usable Rack VLAN Range exists yet
        for this port's VLAN. Does *not* itself check for a collision or
        an out-of-range result; callers validate the derived address via
        ``_validate_static_address()`` (``save()``, and the unconditional
        check at the end of ``clean()``), so that a bad derivation fails
        the whole save rather than being persisted half-written.
        """
        device = _get_related(self, "device")
        if device is None or device.rack is None:
            raise ValidationError(
                "Unracked devices are spare pool (DHCP-configured per CONTEXT.md); rack "
                "the device first, or use is_dhcp for this port instead."
            )
        persisted = (
            NetworkDevicePort._default_manager.filter(pk=self.pk).values("slot_offset", "vlan_id").first()
            if self.pk is not None
            else None
        )
        slot_offset = persisted["slot_offset"] if persisted is not None else self.slot_offset
        vlan_id = persisted["vlan_id"] if persisted is not None else self.vlan_id
        address = _suggest_rack_slot_address(device.rack, device.rack_slot, vlan_id, slot_offset)
        if address is None:
            raise ValidationError(
                f"No usable address range for this port's VLAN in {device.rack} — assign a Rack "
                "VLAN Range before converting this port to static."
            )
        self.address = address
        return address

    def _persisted_delete_guard_fields(self):
        """The persisted ``slot_offset``/``device_id``/``vlan_id`` for this
        row, fetched fresh in one query — never ``self.slot_offset``/
        ``self.device_id``/``self.vlan_id`` directly.

        ``delete()`` (below) has no locked-field validation the way
        ``save()``/``clean()`` do — ``_check_locked_fields_unchanged()`` is
        never called on the delete path — so nothing stops a caller from
        mutating this instance's identity fields in memory before calling
        ``.delete()``. A tampered ``slot_offset`` would make the offset-0
        guard below think an offset-0 row isn't one at all (skipping the
        check entirely) or that some other row is (checking the wrong
        VLAN's siblings), and a tampered ``device_id``/``vlan_id`` would
        point the sibling lookup at the wrong device or VLAN — while
        ``super().delete()`` still deletes the real, persisted row by
        ``pk`` regardless of any of that. Reading these three fresh from
        the database is what keeps the guard meaningful against a caller
        that's already tampering with the exact fields it exists to
        protect.
        """
        if self.pk is None:
            return None
        return (
            NetworkDevicePort._default_manager.filter(pk=self.pk)
            .values("slot_offset", "device_id", "vlan_id")
            .first()
        )

    @property
    def switch(self) -> "NetworkSwitch | None":
        switch_port = self.switch_port
        return switch_port.switch if switch_port is not None else None

    @property
    def default_gateway(self) -> str | None:
        """Live-derived from this port's VLAN (ADR 0010) — never stored, so
        it can't go stale against a VLAN gateway that's edited later.
        ``None`` while the port is DHCP-configured, or if the VLAN itself
        has no gateway set.
        """
        if self.is_dhcp:
            return None
        vlan = _get_related(self, "vlan")
        return vlan.default_gateway if vlan is not None else None
