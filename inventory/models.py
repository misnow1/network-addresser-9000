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
from typing import Any, cast

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, router, transaction
from django.db.models.functions import Coalesce

from .suggestions import (
    dhcp_range_overlaps_cidr,
    ranges_overlap,
    required_block_size,
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


class PortAddressSource(models.TextChoices):
    """Where a ``NetworkDeviceTypePort``'s address comes from (ADR 0022).

    ``SLOT`` (the default) is every port that exists today: computed from
    the device's rack slot (plus ``slot_offset``, ADR 0017) and freely
    editable afterwards. ``OPERATOR`` is a second, independent static
    address on a VLAN the device already uses — a Yamaha console's Device
    Control interface (issue #42) — which the system has no way to
    compute and the operator supplies at creation.

    Orthogonal to ``slot_offset``: that field answers *"is this address
    derived from another port's?"*, this one answers *"does the system
    compute this address at all?"*. ``OPERATOR`` combined with
    ``slot_offset > 0`` is rejected (``_validate_device_type_port_profile``)
    since an address the operator sets cannot also be one the hardware
    derives.
    """

    SLOT = "slot", "From the device's rack slot"
    OPERATOR = "operator", "Set by the operator"


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
    ``NetworkDevicePort.save()``'s offset-sibling cascade gate (ADR 0017),
    which needs the identical normalization to decide whether a given
    ``save(update_fields=...)`` actually touched ``address``/``is_dhcp``.
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
    incomplete, a non-zero ``slot_offset`` port has no offset-0 port on
    the same VLAN to derive its address from, or an ``OPERATOR``-sourced
    port declares a non-zero ``slot_offset`` (ADR 0022 decision 3) — an
    address the operator sets cannot also be one the hardware derives.
    Device type ports have no numbering requirement (unlike switch type
    ports) since ``port_number`` is optional for these.

    Called unconditionally from both ``NetworkDevice.clean()`` and
    ``_materialize_ports()`` — every addressing path, not only the static
    one. The offset checks in particular must live here rather than in
    ``_check_static_materialization_possible()`` (ADR 0017 plan review,
    note 4): that method only runs for a racked+static device, so a DHCP
    or unracked device would otherwise sail past it and materialize an
    offset port with nothing to derive an address from — a row that could
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
        if type_port.address_source == PortAddressSource.OPERATOR and type_port.slot_offset > 0:
            raise ValidationError(
                f"{device_type}'s {type_port.description!r} port is operator-addressed and "
                "cannot also have a non-zero slot_offset — an address the operator sets "
                "cannot be one the hardware derives (ADR 0022)."
            )
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


def occupied_rack_slot_ranges(rack: "Rack") -> list[tuple[int, int]]:
    """Every occupied ``(start, end)`` ordinal range in ``rack``, unioning
    both equipment tables (ADR 0019). Public — unlike its ``_``-prefixed
    neighbour above — because ``admin.py`` calls this to feed
    ``suggestions.lowest_free_run()``.

    Switches always span 1 (``RackSlotAssignmentMixin.slot_span``), so
    they need no aggregate. Devices reuse the existing span annotation
    verbatim rather than inventing a second one — the same
    ``Coalesce(Max("device_type__type_ports__slot_offset"), 0) + 1`` used
    by ``NetworkSwitch._check_rack_slot_not_occupied()`` and
    ``NetworkDevice._check_rack_slot_not_occupied()`` — so an already-
    stored spanning device contributes its whole range here too, not just
    its starting ordinal.

    ``rack_slot__isnull=False`` is filtered defensively even though
    ``rack``/``rack_slot`` are all-or-neither (``RackSlotAssignmentMixin.
    clean()``); two bounded queries, no aggregate per row.
    """
    switch_slots = NetworkSwitch.objects.filter(rack=rack, rack_slot__isnull=False).values_list(
        "rack_slot", flat=True
    )
    # cast(int, ...): rack_slot/_end are guaranteed non-null by the
    # isnull=False filter above, but the queryset's own typing can't
    # express that.
    switch_ranges = [(cast(int, slot), cast(int, slot)) for slot in switch_slots]
    device_ranges = [
        (cast(int, start), cast(int, end))
        for start, end in NetworkDevice.objects.filter(rack=rack, rack_slot__isnull=False)
        .annotate(_span=Coalesce(models.Max("device_type__type_ports__slot_offset"), 0) + 1)
        .annotate(_end=models.F("rack_slot") + models.F("_span") - 1)
        .values_list("rack_slot", "_end")
    ]
    return switch_ranges + device_ranges


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

    #: Class-level default for the ``template`` property below — never a
    #: plain class attribute, since Django's ``Model.__init__`` only
    #: accepts unknown kwargs (``objects.create(template=...)``) when the
    #: name is a field or a property (mirrors ADR 0013's
    #: ``NetworkDevice._port_addressing``/``port_addressing``).
    _template: "RackTemplate | None" = None

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(condition=models.Q(slot_count__gte=1), name="rack_slot_count_gte_1"),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        # ``self.pk is None or self._state.adding`` — see NetworkSwitch.save()
        # for why neither check alone is sufficient.
        is_new = self.pk is None or self._state.adding
        with transaction.atomic():
            super().save(
                force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
            )
            if is_new and self._template is not None:
                self._apply_template()

    def clean(self) -> None:
        super().clean()
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

    def _apply_template(self) -> None:
        """One-time copy of ``self.template``'s VLAN list into real
        ``RackVlanRange`` rows (ADR 0014 decision 7). Each range is built
        unsaved with a blank ``address_range`` and put through
        ``full_clean()`` before ``save()`` — ``RackVlanRange`` has no
        ``save()`` override, so a bare ``objects.create()`` would otherwise
        persist an empty string on a NOT NULL column instead of triggering
        ``RackVlanRange.clean()``'s existing suggestion logic. Runs inside
        the same transaction as this rack's insert (see ``save()``), so any
        failure rolls back the rack and every range materialized before it
        (decision 8's all-or-nothing).

        Reads the template's VLAN links exactly once into ``links`` and
        reuses that same snapshot for both the pre-flight and this loop,
        rather than querying twice — this project runs READ COMMITTED (see
        ``clean()``), so two independent reads inside this one transaction
        could observe different membership if a concurrent template edit
        landed in between. No row lock on the template: that would only
        narrow this specific torn-read window, not ADR 0014's already-
        accepted range-allocation race (see that ADR's Known-gap section),
        so a snapshot is the right amount of correctness for what it costs.
        """
        template = self._template
        assert template is not None  # only ever called from save() after that same check
        links = list(template.vlan_links.select_related("vlan").order_by("vlan__vlan_id"))
        self._check_template_application_possible(links)
        for link in links:
            rng = RackVlanRange(rack=self, vlan=link.vlan, address_range="", created_by=self.created_by)
            rng.full_clean()
            rng.save()

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
                suggestion = suggest_rack_vlan_range(vlan.subnet, rack.slot_count, used_ranges, dhcp_range)
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
    can't both claim an occupied ordinal — since ADR 0017 lets a device
    span several ordinals (``slot_span`` > 1), this is a range-overlap
    check, not just an exact-match one; see ``_check_rack_slot_not_
    occupied()`` on each subclass. This is an interim, form/full_clean-time
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
    """The one signal receiver in this module (see ``SwitchPortVlanProfile``
    docstring for why): ``Model.clean()`` can't validate ``allowed_vlans`` —
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

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["manufacturer", "model", "name"], name="unique_switch_type"),
            models.CheckConstraint(condition=~models.Q(name=""), name="networkswitchtype_name_not_blank"),
        ]
        ordering = ["manufacturer", "model", "name"]

    def __str__(self) -> str:
        return f"{self.manufacturer} {self.model} — {self.name}"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
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

    def clean(self) -> None:
        super().clean()
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
    hostname = models.CharField(max_length=255, blank=True)
    serial_number = models.CharField(max_length=100, blank=True)
    rack = models.ForeignKey(Rack, on_delete=models.PROTECT, null=True, blank=True, related_name="switches")
    rack_slot = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])
    dhcp_server_enabled = models.BooleanField(default=False)

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

    def clean(self) -> None:
        super().clean()
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
        # A plain rack_slot=self.rack_slot equality test (pre-ADR-0017)
        # can't see a device that starts at an earlier ordinal and spans
        # through this switch's slot (e.g. a device at 7 with slot_span 2
        # occupying 7-8, and a switch trying to claim 8) — so this is a
        # range-overlap query against the annotated device end, not an
        # equality test, even though a switch itself always spans exactly
        # one ordinal (plan review note 6).
        conflict = (
            NetworkDevice.objects.filter(rack=self.rack, rack_slot__lte=self.rack_slot)
            .annotate(_span=Coalesce(models.Max("device_type__type_ports__slot_offset"), 0) + 1)
            .annotate(_end=models.F("rack_slot") + models.F("_span") - 1)
            .filter(_end__gte=self.rack_slot)
            .first()
        )
        if conflict is not None:
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


class NetworkDeviceType(AuditedModel):
    """A device make/model *profile* (ADR 0010) — see ``NetworkSwitchType``
    for what "profile" means here. E.g. "Martin Audio IK-42 — with Dante
    Card" vs "— without Dante Card", or "Shure ULXD4Q — Split Mode" vs
    "— Redundant Mode": identical hardware, different port sets/purposes.
    """

    manufacturer = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    name = models.CharField(
        max_length=100,
        help_text='Profile label, e.g. "with Dante Card", or "Default" for a single-profile model.',
    )
    port_count = models.PositiveIntegerField(
        help_text="Must equal the number of Network Device Type Ports defined for this profile."
    )
    companion_type = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="companion_of",
        help_text=(
            "The type this type's instances materialize as an inseparable companion device at "
            "creation (ADR 0018) — e.g. a Yamaha DM7C's Device Control Interface. Leave blank "
            "for an ordinary type with no companion."
        ),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["manufacturer", "model", "name"], name="unique_device_type"),
            models.CheckConstraint(condition=~models.Q(name=""), name="networkdevicetype_name_not_blank"),
        ]
        ordering = ["manufacturer", "model", "name"]

    def __str__(self) -> str:
        return f"{self.manufacturer} {self.model} — {self.name}"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        with transaction.atomic():
            # Both rows, one call — _lock_type_rows() sorts ids, so a
            # concurrent A.companion_type=B / B.companion_type=A pair
            # acquires locks in the same order instead of deadlocking
            # (ADR 0018 review note 7).
            _lock_type_rows(NetworkDeviceType, self.pk, self.companion_type_id)
            self._validate_companion_type()
            if self.pk is not None and self.devices.exists():
                _check_locked_fields_unchanged(
                    NetworkDeviceType,
                    self.pk,
                    {
                        "manufacturer": self.manufacturer,
                        "model": self.model,
                        "name": self.name,
                        "port_count": self.port_count,
                        "companion_type": self.companion_type_id,
                    },
                    update_fields=update_fields,
                )
            super().save(
                force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
            )

    @property
    def slot_span(self) -> int:
        """Ordinal range an instance of this type occupies:
        ``max(slot_offset) + 1`` across its type ports (ADR 0017), or ``1``
        for a type with no offset ports (or no ports at all yet).

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

    def clean(self) -> None:
        super().clean()
        self._validate_companion_type()
        if self.pk is not None and self.devices.exists():
            _check_locked_fields_unchanged(
                NetworkDeviceType,
                self.pk,
                {
                    "manufacturer": self.manufacturer,
                    "model": self.model,
                    "name": self.name,
                    "port_count": self.port_count,
                    "companion_type": self.companion_type_id,
                },
                update_fields=None,
            )

    def _validate_companion_type(self) -> None:
        """Refuse a self-reference, a chain downward (the chosen companion
        itself already declares its own ``companion_type``), or a chain
        upward (``self`` is already some other type's ``companion_type``
        and is now declaring one of its own) — ADR 0018 decision 5.

        Must run after ``save()``'s ``_lock_type_rows()`` holds both this
        row's and ``companion_type``'s row locks, and reads every value
        fresh from the database rather than trusting any cached/in-memory
        related object — otherwise two concurrent saves
        (``A.companion_type = B`` racing ``B.companion_type = A``) could
        each validate against stale state and together commit a cycle
        (ADR 0018 review note 7). ``clean()`` calls this too, unlocked,
        as an advisory pre-flight — same trade-off every other ``clean()``
        check in this module makes.
        """
        if self.companion_type_id is None:
            return
        if self.companion_type_id == self.pk:
            raise ValidationError("A Network Device Type cannot declare itself as its own companion_type.")
        companion_declares_its_own = (
            NetworkDeviceType._default_manager.filter(pk=self.companion_type_id)
            .exclude(companion_type__isnull=True)
            .exists()
        )
        if companion_declares_its_own:
            raise ValidationError(
                f"{self.companion_type} already declares its own companion_type — a companion "
                "chain is not allowed (ADR 0018)."
            )
        if self.pk is not None:
            self_is_already_a_companion = NetworkDeviceType._default_manager.filter(
                companion_type_id=self.pk
            ).exists()
            if self_is_already_a_companion:
                raise ValidationError(
                    f"{self} is already another type's companion_type — a companion chain is "
                    "not allowed (ADR 0018)."
                )


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

    ``slot_offset`` (ADR 0017) is the mechanism for hardware that computes
    a second port's address from a first one and refuses to let anyone
    change it (a DiGiCo console's audio engine, always control address +
    1) — every type port defaults to offset 0 (its own slot), and a VLAN
    with any non-zero-offset port must also carry an offset-0 port on that
    VLAN (``_validate_device_type_port_profile``). This is a narrow,
    mechanism-only carve-out, not a general multi-part-hardware feature —
    see ADR 0017's scope-boundary section for the test ("does the hardware
    compute the second address from the first and refuse to let anyone
    change it?") that keeps ordinary multi-device hardware (a console plus
    a separately-addressed extender, an add-in card) as separate,
    independently-addressed ``NetworkDevice`` rows instead.
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
            "Address offset from the device's slot. Leave at 0 unless the hardware itself "
            "derives this port's address from another port's (e.g. a console engine at "
            "control + 1)."
        ),
    )
    address_source = models.CharField(
        max_length=10,
        choices=PortAddressSource.choices,
        default=PortAddressSource.SLOT,
        help_text=(
            "Where this port's address comes from. Leave at the default unless this port is a "
            "second address on a VLAN this device already uses — a Yamaha console's Device "
            "Control interface — which the system cannot compute and the operator must supply."
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
            ) and not self._hostname_suffix_only_edit():
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

    def _hostname_suffix_only_edit(self) -> bool:
        """Whether this write to an *existing* row changes only
        ``hostname_suffix`` — ADR 0022 decision 4's exemption from the
        profile lock. A derived port label has no materialized
        counterpart to disagree with, so it must stay fixable without
        creating a new named profile for every device of this type.

        ``False`` for a brand new row (``self.pk is None``) — the
        exemption is for editing an already-materialized port, never for
        adding or removing one. Compares against the *persisted* row, not
        the in-memory one, so a write can't smuggle another field's edit
        through by also touching ``hostname_suffix`` in the same call —
        this is the first exemption the profile lock has ever had (plan
        Risks section), and both this method and the admin inline's
        ``has_change_permission()``/``get_readonly_fields()`` must agree
        on it.
        """
        if self.pk is None:
            return False
        persisted = (
            NetworkDeviceTypePort._default_manager.filter(pk=self.pk)
            .values(
                "device_type_id",
                "port_number",
                "description",
                "port_type",
                "vlan_id",
                "slot_offset",
                "address_source",
                "ordinal",
            )
            .first()
        )
        if persisted is None:
            return False
        current = {
            "device_type_id": self.device_type_id,
            "port_number": self.port_number,
            "description": self.description,
            "port_type": self.port_type,
            "vlan_id": self.vlan_id,
            "slot_offset": self.slot_offset,
            "address_source": self.address_source,
            "ordinal": self.ordinal,
        }
        return current == persisted

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


class NetworkDeviceQuerySet(models.QuerySet):
    """Blocks bulk ``QuerySet.delete()`` from removing a companion device
    on its own (ADR 0018) — the model's own ``delete()`` override below
    only guards a single ``instance.delete()``; a queryset delete bypasses
    ``Model.delete()`` for every row, the same reason
    ``NetworkDevicePortQuerySet`` carries its own ``delete()`` override
    alongside the model's (mirrored verbatim here, one table up).

    Deliberately does **not** guard the host's own cascade delete
    (``on_delete=CASCADE`` on ``NetworkDevice.host``) — Django's deletion
    ``Collector`` issues the cascaded companion row's DELETE directly and
    never routes it through a related model's custom manager/queryset, so
    removing a host still removes its companion in one step, as
    ``NetworkDevicePortQuerySet`` already documents for the identical
    reason one table up.

    Known gap (documented, not closed), same root cause as
    ``_check_locked_fields_unchanged()``'s own docstring: a raw
    ``bulk_create()`` against this table is unsupported for the companion
    invariants above.
    """

    def delete(self):
        with transaction.atomic():
            # Only a hosted row whose host is *not also* part of this same
            # selection is "alone" (Codex review round 2, finding 5) — the
            # original version flagged every hosted row unconditionally, so
            # selecting both halves of a pair (or "select all") refused a
            # deletion the host's own cascade would have carried out safely
            # anyway. ``pks`` is evaluated once, up front, so the exclusion
            # reads a stable snapshot of the selection rather than a
            # subquery re-evaluated against a queryset that's mid-delete.
            pks = set(self.values_list("pk", flat=True))
            hosted = list(
                self.exclude(host__isnull=True).exclude(host_id__in=pks).values("pk", "host__hostname")
            )
            if hosted:
                names = ", ".join(f"pk={row['pk']} (host {row['host__hostname']!r})" for row in hosted)
                raise ValidationError(
                    f"Cannot delete a companion device on its own (ADR 0018): {names} — delete "
                    "the host instead."
                )
            return super().delete()


class NetworkDevice(RackSlotAssignmentMixin, AuditedModel):
    """An end-point device instance. Unracked (rack is null) = spare pool.

    ``device_type`` is fixed at creation — see ``NetworkSwitch`` for why
    (ADR 0010): re-typing a device (e.g. adding a Dante card to an amp)
    means removing and recreating it, not editing this field. This is
    expected to be rare (DESIGN.md's "Concrete Device Examples").

    ``host`` (ADR 0018) is set only on a *companion* device — hardware
    that cannot exist without another ``NetworkDevice`` (a Yamaha DM7C's
    Device Control Interface). A host materializes its companion in the
    same transaction as its own ports (``_materialize_companion()``); the
    companion's ``rack``/``rack_slot`` are host-managed (locked at the
    model layer, see ``_locked_fields()``) and move with the host
    (``_move_companion()``); deleting the host cascades to the companion,
    deleting the companion alone is refused (``delete()``,
    ``NetworkDeviceQuerySet.delete()``). Addresses are never derived —
    only existence and lifecycle are linked; see ADR 0018.
    """

    device_type = models.ForeignKey(NetworkDeviceType, on_delete=models.PROTECT, related_name="devices")
    hostname = models.CharField(max_length=255, blank=True)
    serial_number = models.CharField(max_length=100, blank=True)
    rack = models.ForeignKey(Rack, on_delete=models.PROTECT, null=True, blank=True, related_name="devices")
    rack_slot = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])
    host = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="companion",
        help_text=(
            "Set only for a companion device (ADR 0018) — the device this one cannot exist "
            "without. Never set directly; a host materializes its own companion at creation."
        ),
    )

    #: Class-level default for the ``port_addressing`` property below —
    #: never a plain class attribute, since Django's ``Model.__init__``
    #: only accepts unknown kwargs (``objects.create(port_addressing=...)``)
    #: when the name is a field or a property (ADR 0013).
    _port_addressing: str = PortAddressing.STATIC

    #: Class-level default for the ``operator_addresses`` property below —
    #: exactly the ``_port_addressing`` pattern above, for the same reason
    #: (ADR 0022 settled decision 2). Creation-time-only input keyed by
    #: type port ``description``, e.g. ``{"Device Control": "10.201.6.4"}``
    #: — never stored; the materialized ``NetworkDevicePort.address`` is
    #: the record of what was chosen.
    _operator_addresses: dict[str, str] = {}

    #: Creation/move-time inputs for the companion device — not fields,
    #: following ``_port_addressing`` exactly, so ``objects.create(...)``
    #: accepts them as ordinary kwargs (ADR 0018).
    _companion_rack_slot: int | None = None
    _companion_hostname: str | None = None

    #: Set only by ``_park_companion_if_colliding()``/``_finish_companion_
    #: move()`` while they write a companion's host-managed ``rack``/
    #: ``rack_slot`` — modelled exactly on
    #: ``NetworkDevicePort._deriving_address`` (ADR 0017). The single
    #: legitimate writer of an otherwise-locked companion's placement.
    _host_managed_move: bool = False

    objects = NetworkDeviceQuerySet.as_manager()

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
        ]
        ordering = ["hostname"]

    def __str__(self) -> str:
        return self.hostname or f"Device #{self.pk}"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        # ``self.pk is None or self._state.adding`` — see NetworkSwitch.save().
        is_new = self.pk is None or self._state.adding
        with transaction.atomic():
            pending_move: dict[str, Any] | None = None
            if not is_new:
                _check_locked_fields_unchanged(
                    NetworkDevice, self.pk, self._locked_fields(), update_fields=update_fields
                )
                self._check_companion_type_compatibility()
                if self.host_id is None:
                    pending_move = self._plan_companion_move(update_fields)
                    # Reset once _plan_companion_move() has run for THIS
                    # save() call, regardless of what it returned (Codex
                    # review round 5, finding 4) — moved here from inside
                    # _finish_companion_move(), which never runs when the
                    # host and the companion's *explicit* target both
                    # already match what's persisted (a true no-op, e.g.
                    # pair at 5/4, explicit companion_rack_slot=4 given
                    # again). The input was still consulted this call;
                    # leaving it set would let a *later* save() on the
                    # same instance misread it as a fresh explicit target
                    # rather than a stale leftover. This is the single
                    # site covering every exit from this branch, not a
                    # per-branch patch.
                    self._companion_rack_slot = None
                    if pending_move is not None:
                        # Lock order (Codex review round 4, finding 5) —
                        # both pair rows, sorted by pk, before either
                        # row's own UPDATE runs. _lock_type_rows() already
                        # solves the identical problem for type rows
                        # (:2650). Without this, a colliding move (which
                        # parks the companion first — a real save() on the
                        # companion row, i.e. UPDATE companion, then UPDATE
                        # host below) and a non-colliding move (UPDATE
                        # host below, then UPDATE companion in
                        # _finish_companion_move()) acquire the pair in
                        # opposite orders. Two such moves running
                        # concurrently can each hold one row and wait on
                        # the other — a real deadlock, not just contention.
                        # Locked only when an actual move is planned, not
                        # on every save() of a host-with-companion, so a
                        # hostname-only edit doesn't needlessly widen its
                        # lock window over a row it's never going to write.
                        #
                        # Note this still doesn't lock *before*
                        # _plan_companion_move()'s own reads run (Codex
                        # review round 5, finding 3) — that method also
                        # runs unlocked from clean()'s pre-flight, so
                        # locking ahead of it isn't free; see
                        # _plan_companion_move()'s own handling of a
                        # companion that's vanished by the time its
                        # (unlocked) read runs, immediately below this
                        # comment block's call.
                        _lock_type_rows(NetworkDevice, self.pk, pending_move["companion_pk"])
                        # Unconditional, not just clean()'s pre-flight
                        # (Codex review round 3, finding 1) — a bare
                        # save() never calls clean(), and nothing else on
                        # the save path checks the pair's ranges against
                        # each other; see _check_pending_move_no_overlap()'s
                        # docstring for why _finish_companion_move()'s own
                        # full_clean() structurally can't catch this.
                        self._check_pending_move_no_overlap(pending_move)
                        self._park_companion_if_colliding(pending_move)
            elif self.device_type_id is not None:
                # Locks the type row so a concurrent edit to its port
                # templates/count can't interleave with this materialization.
                _lock_type_rows(NetworkDeviceType, self.device_type_id)
                # Re-read from the DB, not the cached relation this
                # instance loaded earlier (Codex review round 5,
                # finding 2) — locking a row and then continuing to
                # trust a stale in-memory copy of it defeats the point
                # of the lock. Without this, a concurrent, already-
                # committed edit to companion_type is invisible to both
                # the compatibility check below and
                # _materialize_companion() further down (both read via
                # _get_related(self, "device_type"), i.e. off this same
                # cached object) — a host could materialize a companion
                # of the type's *old* companion_type while its own,
                # now-locked type row says something else.
                self.device_type = NetworkDeviceType._default_manager.get(pk=self.device_type_id)
                self._check_companion_type_compatibility()
            try:
                super().save(
                    force_insert=force_insert,
                    force_update=force_update,
                    using=using,
                    update_fields=update_fields,
                )
                if is_new:
                    self._materialize_ports()
                    self._materialize_companion()
                elif pending_move is not None:
                    self._finish_companion_move(pending_move)
            except Exception:
                if is_new and self._state.adding is False and self.pk is not None:
                    # The atomic block below still rolls back the INSERT
                    # above, but Django has already set self.pk and
                    # cleared self._state.adding on this in-memory object
                    # — a DB rollback doesn't undo those Python attributes
                    # (Codex review round 5, finding 1). Left as-is, a
                    # caller that catches this, fixes whatever was wrong
                    # (e.g. an unavailable companion slot), and retries
                    # the *same* instance would compute is_new = False on
                    # the next call, take the update path, have its
                    # UPDATE affect zero rows (the row was rolled back),
                    # fall back to Django's own zero-row-UPDATE→INSERT
                    # behavior, and skip both materializers — reproducing
                    # this finding's own bug (a host with no ports and no
                    # companion) by a different route than finding 3
                    # below. Restored so a retry takes the creation path
                    # again, in full, exactly as a fresh instance would.
                    self.pk = None
                    self._state.adding = True
                raise

    def clean(self) -> None:
        super().clean()
        self._check_companion_type_compatibility()
        if self.pk is None or self._state.adding:
            device_type = _get_related(self, "device_type")
            if device_type is not None and device_type.pk is not None:
                _validate_device_type_port_profile(device_type)
                if self._materializes_static():
                    self._check_static_materialization_possible()
                self._check_companion_creation_possible(device_type)
        else:
            _check_locked_fields_unchanged(NetworkDevice, self.pk, self._locked_fields(), update_fields=None)
            if self.host_id is None:
                self._check_companion_move_possible()

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
    def operator_addresses(self) -> dict[str, str]:
        """Creation-time-only input for ``OPERATOR``-sourced ports (ADR
        0022 settled decision 2), keyed by the type port's ``description``
        — e.g. ``{"Device Control": "10.201.6.4"}``. Never stored; exactly
        the ``port_addressing`` pattern above, so
        ``objects.create(operator_addresses={...})`` works. Setting this
        after creation has no effect since ``_materialize_ports()`` only
        runs once.
        """
        return self._operator_addresses

    @operator_addresses.setter
    def operator_addresses(self, value: dict[str, str]) -> None:
        self._operator_addresses = value

    @property
    def companion_rack_slot(self) -> int | None:
        """Creation/move-time input: the companion's own rack slot (ADR
        0018). Not a field — see ``port_addressing`` above for the
        pattern. ``None`` (the default) means "preserve the current
        relative offset" on an existing host's move (decision 1); a
        companion-declaring type's *new* host requires this whenever it's
        being created racked, since there's no existing offset yet to
        preserve.
        """
        return self._companion_rack_slot

    @companion_rack_slot.setter
    def companion_rack_slot(self, value: int | None) -> None:
        if value is not None and value < 1:
            raise ValidationError(f"companion_rack_slot must be >= 1 (got {value}).")
        self._companion_rack_slot = value

    @property
    def companion_hostname(self) -> str | None:
        """Creation-time input: the companion's own hostname (ADR 0018).
        Blank/``None`` copies the host's own hostname verbatim (decision
        3) — duplicate hostnames are already legal in this model.
        """
        return self._companion_hostname

    @companion_hostname.setter
    def companion_hostname(self, value: str | None) -> None:
        self._companion_hostname = value

    @property
    def slot_span(self) -> int:
        """Delegates to ``device_type.slot_span`` (ADR 0017) — overrides
        ``RackSlotAssignmentMixin``'s default of 1. Reads ``device_type``
        via ``_get_related()`` so an unsaved device with no type assigned
        yet still cleans (mirrors the same defensive pattern used
        throughout this module for a possibly-unset FK on an in-progress
        instance).
        """
        device_type = _get_related(self, "device_type")
        if device_type is None or device_type.pk is None:
            return 1
        return device_type.slot_span

    def _materializes_static(self) -> bool:
        """Whether this (not-yet-materialized) device will get static
        addresses — only when racked (decision 3: unracked is always DHCP,
        spare pool by definition) and the static choice is in effect
        (ADR 0013).
        """
        return self.rack is not None and self.port_addressing == PortAddressing.STATIC

    def _operator_address_for(self, type_port: "NetworkDeviceTypePort") -> str:
        """The operator-supplied address for an ``OPERATOR``-sourced type
        port (ADR 0022), keyed by ``description`` in
        ``self.operator_addresses``. Raises, naming the port, if the
        operator didn't supply one — called from both
        ``_check_static_materialization_possible()`` (the pre-flight that
        also covers the ``objects.create()`` path) and
        ``_materialize_ports()`` itself.
        """
        try:
            return self.operator_addresses[type_port.description]
        except KeyError:
            raise ValidationError(
                f"{self.device_type}'s {type_port.description!r} port is operator-addressed "
                "(ADR 0022) and needs an address — set "
                f"operator_addresses[{type_port.description!r}]."
            ) from None

    def _check_static_materialization_possible(self) -> None:
        """Pre-flight over ``self.device_type``'s Network Device Type Ports
        for whether static materialization can succeed — pure, needs no
        device pk, so it can run from both ``clean()`` (admin form errors)
        and ``_materialize_ports()`` (the ``objects.create()`` path, which
        never calls ``clean()``).

        Skips L2-only-VLAN ports entirely (decision 4: those always
        materialize DHCP and that's not a failure). Of what's left,
        ``SLOT``-sourced ports are grouped by ``(vlan, slot_offset)``: any
        VLAN shared by more than one port **at the same slot_offset**
        can't be addressed by ``suggest_slot_address()``'s
        one-address-per-(slot, VLAN) model (decision 5, Switched Mode
        devices — ADR 0017 narrows this from "same VLAN" to "same VLAN and
        same offset", so it still catches Switched Mode but no longer
        catches a console's derived engine port), and each remaining
        port's suggested address must actually be usable
        (``_validate_static_address``).

        ``OPERATOR``-sourced ports (ADR 0022) are exempt from that
        grouping refusal entirely — that exemption is the fix for issue
        #42, letting a second, independently-addressed port share a VLAN
        with a ``SLOT`` port. Each still needs its own pre-flight: the
        operator must have supplied an address for it
        (``_operator_address_for``, a ``ValidationError`` naming the port
        if not — this is what makes the ``objects.create()`` path fail
        before writing anything, matching ``_materialize_ports()`` below),
        and that address must validate the same as any other static one.

        Also enforces the ``.255`` bound here (ADR 0017 plan review, note
        3), not only in ``RackSlotAssignmentMixin.clean()`` — this method
        runs on the ``objects.create()`` path via ``_materialize_ports()``,
        which never calls ``clean()`` at all, so without a copy of the
        bound here a device created directly past a rack's slot_count
        would still materialize an address that reads as that block's
        broadcast address (see ``required_block_size()``/ADR 0015).
        """
        addressable = [tp for tp in self.device_type.type_ports.select_related("vlan") if tp.vlan.subnet]
        slot_ports = [tp for tp in addressable if tp.address_source == PortAddressSource.SLOT]
        operator_ports = [tp for tp in addressable if tp.address_source == PortAddressSource.OPERATOR]
        by_vlan_offset: dict[tuple[int, int], list[NetworkDeviceTypePort]] = {}
        for type_port in slot_ports:
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
        for type_port in slot_ports:
            address = _suggest_rack_slot_address(
                self.rack, self.rack_slot, type_port.vlan_id, type_port.slot_offset
            )
            if address is None:
                raise ValidationError(
                    f"No usable address range for {type_port.vlan} in {self.rack} — assign a "
                    f"Rack VLAN Range for this VLAN before creating a static {self.device_type} "
                    "device here, or use DHCP."
                )
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
        for type_port in operator_ports:
            address = self._operator_address_for(type_port)
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
                raise ValidationError(exc.messages) from exc

    def _materialize_ports(self) -> None:
        """One-time copy of ``device_type``'s Network Device Type Ports into
        real ``NetworkDevicePort`` rows — static by default when racked, or
        DHCP when unracked or explicitly chosen (ADR 0013, revising ADR
        0010's always-DHCP rule). Runs inside the same transaction as this
        device's insert (see ``save()``), so any failure here rolls back
        the device and every port materialized before it.

        Each port's ``slot_offset`` is copied from its type port (ADR
        0017), and each port's own address is computed from its own
        offset independently (``_suggest_rack_slot_address`` inside
        ``NetworkDevicePort.clean()``) — so, unlike the derive-on-edit
        cascade (``NetworkDevicePort._derive_offset_siblings``), no
        offset-0-first ordering is required here; the loop stays ordered
        by ``ordinal`` as it always has.

        An ``OPERATOR``-sourced port (ADR 0022) on a device that
        materializes statically takes its address from
        ``self.operator_addresses`` instead of being derived — set
        explicitly here so ``NetworkDevicePort.clean()``'s own
        auto-suggest branch (which would otherwise fill in the *slot*
        address, since ``slot_offset`` is always 0 for an operator port)
        never runs for it. On an unracked/DHCP device it materializes DHCP
        like any other port and the mapping is ignored.
        """
        _validate_device_type_port_profile(self.device_type)
        static = self._materializes_static()
        if static:
            self._check_static_materialization_possible()
        for type_port in self.device_type.type_ports.select_related("vlan").order_by("ordinal"):
            if static and type_port.vlan.subnet:
                address = None
                if type_port.address_source == PortAddressSource.OPERATOR:
                    address = self._operator_address_for(type_port)
                port = NetworkDevicePort(
                    device=self,
                    port_number=type_port.port_number,
                    description=type_port.description,
                    vlan=type_port.vlan,
                    port_type=type_port.port_type,
                    ordinal=type_port.ordinal,
                    slot_offset=type_port.slot_offset,
                    source_type_port=type_port,
                    is_dhcp=False,
                    address=address,
                    created_by=self.created_by,
                )
                port.full_clean()
                port.save()
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

    def _check_rack_slot_not_occupied(self) -> None:
        if self.rack_slot is None:
            return  # only ever called from clean() once rack/rack_slot are both set
        my_start = self.rack_slot
        my_end = self.rack_slot + self.slot_span - 1
        if NetworkSwitch.objects.filter(
            rack=self.rack, rack_slot__gte=my_start, rack_slot__lte=my_end
        ).exists():
            raise ValidationError(
                f"Rack slot(s) {my_start}-{my_end} in {self.rack} are already occupied by a switch."
                if my_end != my_start
                else f"Rack slot {self.rack_slot} in {self.rack} is already occupied by a switch."
            )
        # Devices only — unique(rack, rack_slot) already catches an equal
        # starting ordinal at the DB level; this catches the case that
        # constraint can't: another device's span overlapping ours without
        # sharing a starting ordinal (a device at 7 spanning 7-8, a new one
        # at 8). Annotates every other device's own end ordinal from its
        # type's slot_span (a switch always spans 1, so only this side
        # needs the aggregate — plan review note 6) and tests range overlap
        # against it, not equality.
        #
        # Excludes self's whole companion pair (_companion_pair_pks()), not
        # just self.pk (ADR 0018 review note 1) — a vacate-then-place move
        # (host 5→4, companion 4→3) would otherwise have the host's own
        # pre-flight see its companion still sitting at the host's target
        # slot and reject a legal move before save() ever gets a chance to
        # park it. Excluding the partner here does *not* by itself stop a
        # host and its companion from landing on each other — that's
        # checked explicitly, once, by _check_companion_move_possible()/
        # _check_companion_creation_possible().
        conflict = (
            NetworkDevice.objects.filter(rack=self.rack, rack_slot__lte=my_end)
            .exclude(pk__in=self._companion_pair_pks())
            .annotate(_span=Coalesce(models.Max("device_type__type_ports__slot_offset"), 0) + 1)
            .annotate(_end=models.F("rack_slot") + models.F("_span") - 1)
            .filter(_end__gte=my_start)
            .first()
        )
        if conflict is not None:
            assert conflict.rack_slot is not None  # DB constraint: rack and rack_slot are all-or-neither
            conflict_end = conflict.rack_slot + conflict.slot_span - 1
            raise ValidationError(
                f"Rack slot(s) {my_start}-{my_end} in {self.rack} overlap {conflict}'s existing "
                f"occupied range ({conflict.rack_slot}-{conflict_end})."
            )

    def validate_unique(self, exclude=None) -> None:
        """Excludes this device's own companion-pair partner from Django's
        *built-in* ``(rack, rack_slot)`` uniqueness check (the
        ``unique_device_rack_slot`` constraint) — the same partner
        exclusion ``_check_rack_slot_not_occupied()`` applies above, for
        the same reason (ADR 0018 review note 1), but that method is
        this app's *own* occupancy pre-flight; ``Model.validate_unique()``
        is a separate, unrelated step ``full_clean()`` also runs, whose
        own uniqueness query only ever excludes ``self.pk``. Without this,
        a vacate-then-place move's target slot — briefly still occupied in
        the database by the other half of the pair, in the window between
        ``clean()`` and ``save()``'s park — trips Django's own "already
        exists" error before this app's pair-aware logic ever gets a
        chance to run.

        Skips Django's exact-match check only when both are set (nothing
        to skip for a spare-pool device); every other unique check on this
        model (there are none today, but a future one) still runs via
        ``super().validate_unique()`` unaffected.
        """
        skip_rack_slot = self.rack_id is not None and self.rack_slot is not None
        excluded_fields = set(exclude) if exclude is not None else set()
        super().validate_unique(
            exclude=excluded_fields | {"rack", "rack_slot"} if skip_rack_slot else exclude
        )
        if skip_rack_slot:
            conflict = (
                NetworkDevice._default_manager.filter(rack=self.rack, rack_slot=self.rack_slot)
                .exclude(pk__in=self._companion_pair_pks())
                .exists()
            )
            if conflict:
                raise ValidationError("Network device with this Rack and Rack slot already exists.")

    def validate_constraints(self, exclude=None) -> None:
        """``full_clean()`` calls both ``validate_unique()`` above *and*
        ``validate_constraints()`` — the latter re-checks ``Meta.constraints``
        (including ``unique_device_rack_slot``) directly, entirely
        independently of the override above, via each constraint's own
        ``.validate()``. ``UniqueConstraint.validate()`` excludes only
        ``self.pk``, with no knowledge of a companion pair, so a
        vacate-then-place move's target slot — still occupied in the
        database by the pair's other half at ``clean()`` time — trips this
        raw constraint check even though ``validate_unique()``'s pair-aware
        version above already passed moments earlier. (The two failures
        are textually indistinguishable: ``UniqueConstraint``'s default
        violation message and the literal string raised above both read
        "Network device with this Rack and Rack slot already exists.")

        Skips the ``unique_device_rack_slot`` constraint specifically —
        already re-checked, pair-aware, by ``validate_unique()`` — rather
        than excluding the ``rack``/``rack_slot`` *fields* wholesale:
        this model's two ``CheckConstraint``s also reference those field
        names, and ``CheckConstraint.validate()`` skips any constraint
        whose condition references an excluded field, so a field-based
        exclude would silently stop enforcing "rack_slot >= 1" and
        "rack and rack_slot together" too. Every other constraint,
        including both ``CheckConstraint``s, still runs exactly as
        ``Model.validate_constraints()`` would run it.
        """
        using = router.db_for_write(self.__class__, instance=self)
        errors: dict[str, list[Any]] = {}
        for model_class, model_constraints in self.get_constraints():
            for constraint in model_constraints:
                if (
                    isinstance(constraint, models.UniqueConstraint)
                    and constraint.name == "unique_device_rack_slot"
                ):
                    continue
                try:
                    constraint.validate(model_class, self, exclude=exclude, using=using)
                except ValidationError as e:
                    # ``fields`` only exists on UniqueConstraint, not the base
                    # class — getattr(), not a direct attribute access, so a
                    # CheckConstraint's ValidationError (never code=="unique")
                    # can't trip an AttributeError here, matching upstream
                    # Model.validate_constraints()'s reliance on short-circuit
                    # evaluation for the same safety.
                    constraint_fields = getattr(constraint, "fields", None)
                    if (
                        getattr(e, "code", None) == "unique"
                        and constraint_fields is not None
                        and len(constraint_fields) == 1
                    ):
                        errors.setdefault(constraint_fields[0], []).append(e)
                    else:
                        errors = e.update_error_dict(errors)
        if errors:
            raise ValidationError(errors)

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

    # ------------------------------------------------------------------
    # ADR 0018 — device companions
    # ------------------------------------------------------------------

    def delete(self, using=None, keep_parents=False) -> tuple[int, dict[str, int]]:
        """Refuse deleting a companion device on its own (ADR 0018) — the
        queryset twin (``NetworkDeviceQuerySet.delete()``) guards a bulk
        delete; this guards a single ``instance.delete()``. Deliberately
        does **not** fire during the host's own cascade delete: Django's
        deletion ``Collector`` issues the cascaded companion row's DELETE
        directly, bypassing both this override and the queryset's, so
        removing a host still removes its companion in one step (see
        ``NetworkDeviceQuerySet``'s docstring).

        Reads the *persisted* ``host_id`` (``_persisted_host_id()``), not
        ``self``'s in-memory one — ``delete()`` has no locked-field
        validation, so ``self.host_id`` is untrusted here the same way
        ``NetworkDevicePort.delete()`` treats its own identity fields as
        untrusted (``_persisted_delete_guard_fields()``).
        """
        with transaction.atomic():
            persisted_host_id = self._persisted_host_id()
            if persisted_host_id is not None:
                host = NetworkDevice._default_manager.filter(pk=persisted_host_id).first()
                host_label = host if host is not None else f"device #{persisted_host_id}"
                raise ValidationError(
                    f"Cannot delete a companion device on its own (ADR 0018): {self} belongs to "
                    f"{host_label} — delete the host instead."
                )
            return super().delete(using=using, keep_parents=keep_parents)

    def _persisted_host_id(self) -> int | None:
        if self.pk is None:
            return None
        return NetworkDevice._default_manager.filter(pk=self.pk).values_list("host_id", flat=True).first()

    def _locked_fields(self) -> dict[str, Any]:
        # ``device_type``/``host`` identify what this row is and, for a
        # companion, which host it belongs to — neither is ever meant to
        # change after creation (a companion can never be reparented, ADR
        # 0018). ``rack``/``rack_slot`` are additionally locked whenever
        # this row is *persisted* as a companion (``_persisted_host_id()``
        # — not the in-memory ``self.host_id``, same lesson as
        # ``NetworkDevicePort._locked_fields()``: an in-memory ``host =
        # None`` must not unlock the check), unless the write is the one
        # legitimate mover (``_host_managed_move``, set only by
        # ``_park_companion_if_colliding()``/``_finish_companion_move()``).
        fields: dict[str, Any] = {
            "device_type": self.device_type_id,
            "host": self.host_id,
        }
        if self._persisted_host_id() is not None and not self._host_managed_move:
            fields["rack"] = self.rack_id
            fields["rack_slot"] = self.rack_slot
        return fields

    def _companion_pair_pks(self) -> set[int]:
        """``{self.pk, the other half of self's host/companion pair}`` —
        used by ``_check_rack_slot_not_occupied()`` to exclude a pair's own
        two rows from each other's occupancy pre-flight (ADR 0018 review
        note 1). ``self.pk`` is included whenever set; the partner is read
        from the *persisted* ``host_id`` when ``self`` is a companion, or
        from the existing ``companion`` relation when ``self`` is a host.
        """
        pks: set[int] = {self.pk} if self.pk is not None else set()
        persisted_host_id = self._persisted_host_id()
        if persisted_host_id is not None:
            pks.add(persisted_host_id)
        elif self.pk is not None:
            companion = _get_related(self, "companion")
            if companion is not None and companion.pk is not None:
                pks.add(companion.pk)
        return pks

    def _check_companion_type_compatibility(self) -> None:
        """Enforce the type graph against ``host`` (ADR 0018 review note
        3) — ``host`` being merely non-null is not enough:

        - a device whose type is some other type's ``companion_type`` must
          have a ``host``;
        - a device with a ``host`` must satisfy ``self.device_type_id ==
          host.device_type.companion_type_id``;
        - a device whose type declares a ``companion_type`` may not itself
          have a ``host`` — it's a host, not a companion.

        Called from both ``clean()`` and ``save()`` — the latter because
        ``objects.create()`` never calls ``clean()``.
        """
        device_type = _get_related(self, "device_type")
        if device_type is not None and device_type.pk is not None:
            if device_type.companion_type_id is not None and self.host_id is not None:
                raise ValidationError(
                    f"{device_type} declares its own companion_type — an instance of it cannot "
                    "also be someone else's companion (ADR 0018)."
                )
            if self.host_id is None and device_type.companion_of.exists():
                raise ValidationError(
                    f"{device_type} is another type's companion_type — an instance of it must "
                    "have a host (ADR 0018); it cannot be created standalone."
                )
        if self.host_id is not None:
            host = _get_related(self, "host")
            if (
                host is not None
                and host.pk is not None
                and device_type is not None
                and device_type.pk is not None
            ):
                host_device_type = _get_related(host, "device_type")
                if (
                    host_device_type is not None
                    and host_device_type.pk is not None
                    and device_type.pk != host_device_type.companion_type_id
                ):
                    raise ValidationError(
                        f"{self} (type {device_type}) cannot be {host}'s companion — {host}'s "
                        f"type declares {host_device_type.companion_type} as its companion_type "
                        "(ADR 0018)."
                    )

    def _check_companion_creation_possible(self, device_type: "NetworkDeviceType") -> None:
        """Pre-flight for whether ``_materialize_companion()`` would
        succeed — pure, so it can run from both ``clean()`` (admin form
        errors) and the top of ``_materialize_companion()`` itself (the
        ``objects.create()`` path, which never calls ``clean()``). Same
        shape as ``_check_static_materialization_possible()``, one level
        up. A no-op for an ordinary type with no ``companion_type``.
        """
        if device_type.companion_type_id is None:
            return
        if self.rack is None:
            return  # unracked host materializes an unracked companion — nothing to place
        if self._companion_rack_slot is None:
            raise ValidationError(
                f"companion_rack_slot is required when creating a racked {device_type} — it "
                "materializes a companion device that needs its own rack slot (ADR 0018)."
            )
        companion_type = device_type.companion_type
        assert companion_type is not None  # companion_type_id checked non-null above
        companion_span = companion_type.slot_span
        companion_start = self._companion_rack_slot
        companion_end = companion_start + companion_span - 1
        if companion_end > self.rack.slot_count:
            raise ValidationError(
                f"companion_rack_slot {companion_start} plus {companion_type}'s span "
                f"({companion_span}, ending at ordinal {companion_end}) exceeds {self.rack}'s "
                f"slot_count ({self.rack.slot_count})."
            )
        # The pair's own two target ranges, checked against each other
        # explicitly (ADR 0018 review note 1) — neither row exists yet, so
        # the occupancy queries below can't see this overlap on their own.
        host_span = device_type.slot_span
        if self.rack_slot is not None:
            host_start, host_end = self.rack_slot, self.rack_slot + host_span - 1
            if companion_start <= host_end and host_start <= companion_end:
                raise ValidationError(
                    f"companion_rack_slot {companion_start}-{companion_end} would overlap this "
                    f"device's own rack_slot range ({host_start}-{host_end}) — a host and its "
                    "companion can't occupy the same ordinal (ADR 0018)."
                )
        if NetworkSwitch.objects.filter(
            rack=self.rack, rack_slot__gte=companion_start, rack_slot__lte=companion_end
        ).exists():
            raise ValidationError(
                f"companion_rack_slot(s) {companion_start}-{companion_end} in {self.rack} are "
                "already occupied by a switch."
            )
        conflict = (
            NetworkDevice.objects.filter(rack=self.rack, rack_slot__lte=companion_end)
            .annotate(_span=Coalesce(models.Max("device_type__type_ports__slot_offset"), 0) + 1)
            .annotate(_end=models.F("rack_slot") + models.F("_span") - 1)
            .filter(_end__gte=companion_start)
            .first()
        )
        if conflict is not None:
            assert conflict.rack_slot is not None
            conflict_end = conflict.rack_slot + conflict.slot_span - 1
            raise ValidationError(
                f"companion_rack_slot(s) {companion_start}-{companion_end} in {self.rack} "
                f"overlap {conflict}'s existing occupied range ({conflict.rack_slot}-{conflict_end})."
            )
        if self.port_addressing != PortAddressing.STATIC:
            return
        addressable = [tp for tp in companion_type.type_ports.select_related("vlan") if tp.vlan.subnet]
        for type_port in addressable:
            address = _suggest_rack_slot_address(
                self.rack, companion_start, type_port.vlan_id, type_port.slot_offset
            )
            if address is None:
                raise ValidationError(
                    f"No usable address range for {type_port.vlan} in {self.rack} — assign a "
                    f"Rack VLAN Range for this VLAN before creating a static {companion_type} "
                    "companion here."
                )
            try:
                _validate_static_address(
                    address,
                    type_port.vlan,
                    self.rack,
                    companion_start,
                    exclude_switch_address_pk=None,
                    exclude_device_port_pk=None,
                )
            except ValidationError as exc:
                # Same trap _check_static_materialization_possible() guards
                # against: _validate_static_address() raises keyed on
                # "address", the right shape for NetworkDevicePort.clean()
                # but not this call site (checking the *companion's*
                # prospective address from the host's own clean()) — a
                # dict-keyed ValidationError for a nonexistent field
                # crashes Django admin's add_error(), so re-raise flat.
                raise ValidationError(exc.messages) from exc

    def _check_companion_move_possible(self) -> None:
        """Pair-aware pre-flight for an existing host's own ``clean()``
        (ADR 0018 review note 1): explicitly checks the pair's own two
        *target* ranges against each other, which
        ``_check_rack_slot_not_occupied()``'s partner exclusion
        deliberately does not — excluding the partner without this would
        let a host and its companion land on top of each other. A no-op
        when this device has no companion, or when the move plan finds
        nothing actually changing.

        This is the ``clean()``-path pre-flight (a nice admin form error) —
        ``save()`` calls ``_check_pending_move_no_overlap()`` directly and
        unconditionally for the same check on the *save* path, since a bare
        ``save()`` never reaches this method at all (Codex review round 3,
        finding 1).
        """
        companion = _get_related(self, "companion")
        if companion is None or companion.pk is None:
            return
        pending_move = self._plan_companion_move(update_fields=None)
        if pending_move is None:
            return
        self._check_pending_move_no_overlap(pending_move)

    def _check_pending_move_no_overlap(self, pending_move: dict[str, Any]) -> None:
        """The pair's own two *target* ranges, checked against each other —
        shared by ``_check_companion_move_possible()`` (the ``clean()``-path
        pre-flight, for a nice admin form error) and ``save()`` itself
        (Codex review round 3, finding 1).

        Before this, the only path that ran this check was ``clean()``,
        which a bare ``save()`` never calls — and nothing on the save path
        caught it either: the companion's own ``full_clean()`` in
        ``_finish_companion_move()`` deliberately *excludes* its host from
        occupancy conflicts (via ``_companion_pair_pks()``, review note 1),
        precisely because pair-vs-pair overlap is this check's job, not
        ``_check_rack_slot_not_occupied()``'s. And ``unique_device_rack_slot``
        only compares *starting* slots, so a host spanning several ordinals
        (a non-zero ``slot_offset`` type, ADR 0017) could commit an
        overlapping companion at a *different* starting slot — 5–6 and 6,
        say — through a bare ``host.save()`` with nothing to stop it.

        Fetches the companion fresh rather than trusting any cached
        relation on ``self`` — ``slot_span`` depends only on
        ``device_type``, which never changes, so freshness doesn't matter
        for *that*, but doing it here keeps this method usable standalone
        without relying on a caller's possibly-stale ``_get_related()``
        result.
        """
        host_start = pending_move["host_new_rack_slot"]
        companion_start = pending_move["companion_new_rack_slot"]
        if host_start is None or companion_start is None:
            return  # unracking, or the companion ends up unracked — nothing can overlap
        companion = NetworkDevice._default_manager.get(pk=pending_move["companion_pk"])
        # ``self.slot_span`` reads through ``self.device_type`` — possibly
        # dirty in memory (Codex review round 4, finding 1): ``device_type``
        # is locked, but ``_check_locked_fields_unchanged()`` only checks
        # fields named in ``update_fields``, so ``save(update_fields=
        # ["rack_slot"])`` never even looks at it. A persisted span-2 host
        # temporarily holding an in-memory, never-to-be-persisted span-1
        # type would validate against the wrong span while the database
        # keeps the real one. Fetched fresh, the same pattern ``companion``
        # above already uses, since ``device_type`` never actually changes
        # — only what ``self`` happens to hold in memory does.
        persisted_self = NetworkDevice._default_manager.get(pk=self.pk)
        host_end = host_start + persisted_self.slot_span - 1
        companion_end = companion_start + companion.slot_span - 1
        if host_start <= companion_end and companion_start <= host_end:
            raise ValidationError(
                f"Moving {self} to rack_slot {host_start} would overlap its companion "
                f"{companion}'s target rack_slot {companion_start} (ADR 0018) — choose a "
                "companion_rack_slot that doesn't collide with the host's own range."
            )

    def _materialize_companion(self) -> None:
        """One-time creation of this (now-saved, ``self.pk`` set) device's
        companion, when its type declares one (ADR 0018) — the ADR's "one
        level up" of ADR 0010's seed-once port materialization. Runs
        inside the same transaction ``_materialize_ports()`` already
        opened via ``save()``'s ``is_new`` branch, so a failure anywhere
        (an unavailable companion slot, an address collision on the
        companion's own ports) rolls back the host, its ports, and any
        partial companion state together.

        The companion inherits the host's ``port_addressing`` (a
        creation-time choice made once, ADR 0013) and defaults its
        hostname to the host's own, verbatim, when none was given
        (decision 3).

        Resets ``self._companion_rack_slot``/``self._companion_hostname``
        to their class defaults once consumed here — unlike
        ``_port_addressing`` (which this property/setter pair otherwise
        mirrors), this input is read a *second* time, with a *different*
        meaning, if the same in-memory host is later moved
        (``_plan_companion_move()``'s "blank preserves the offset, a
        value given is an explicit absolute target" — decision 1). Without
        the reset, a host object reused after creation to perform an
        immediate move would misread its own leftover creation-time slot
        as an explicit move-time override.
        """
        device_type = _get_related(self, "device_type")
        if device_type is None or device_type.companion_type_id is None:
            return
        self._check_companion_creation_possible(device_type)
        companion = NetworkDevice(
            host=self,
            device_type_id=device_type.companion_type_id,
            rack=self.rack,
            rack_slot=self._companion_rack_slot if self.rack is not None else None,
            hostname=self._companion_hostname or self.hostname,
            created_by=self.created_by,
        )
        companion.port_addressing = self.port_addressing
        companion.full_clean()
        companion.save()
        self._companion_rack_slot = None
        self._companion_hostname = None

    def _plan_companion_move(
        self, update_fields: "list[str] | frozenset[str] | None"
    ) -> dict[str, Any] | None:
        """Compute the target ``rack``/``rack_slot`` for ``self``'s
        companion, from ``self``'s own pending rack/slot change, without
        writing anything — called from both ``save()``'s not-new branch
        (before ``super().save()`` writes ``self``'s own row) and
        ``clean()``'s pair pre-flight (``_check_companion_move_possible()``).

        Returns ``None`` when there's nothing to move: no companion,
        ``update_fields`` excludes both rack fields (review note 5), or
        **neither row's** effective new values differ from what's already
        persisted — checked for the host *and* the companion (Codex review
        round 4, finding 2), not the host alone. An explicit
        ``companion_rack_slot`` with the host otherwise stationary (the
        change form's "host slot unchanged, companion slot edited" case)
        is a real move that used to be discarded here before the
        companion's own target was ever computed.

        A blank (``None``) ``companion_rack_slot`` preserves the current
        relative offset (decision 1); an explicit one is an absolute
        target — this **implements** that decision rather than reopening
        it. Racking a previously-unracked assembly with no existing offset
        to preserve requires an explicit ``companion_rack_slot``
        (decision 6).
        """
        normalized = _normalize_update_fields(NetworkDevice, update_fields)
        if normalized is not None and not ({"rack", "rack_slot"} & normalized):
            return None
        if self.pk is None:
            return None
        companion = _get_related(self, "companion")
        if companion is None or companion.pk is None:
            return None
        persisted = NetworkDevice._default_manager.filter(pk=self.pk).values("rack_id", "rack_slot").first()
        if persisted is None:
            # Not "nothing to move" (Codex review round 4, finding 4) —
            # every caller of this method has already established
            # ``self.pk`` is set and this isn't a new row, so a missing
            # persisted row means something else deleted this host between
            # it being loaded and this ``save()`` reaching here (READ
            # COMMITTED — see ``django_mysql_read_committed`` in project
            # memory — so a concurrent transaction's commit is visible
            # mid-transaction). Returning ``None`` here would let
            # ``super().save()`` fall through to Django's own "``UPDATE``
            # affected 0 rows, so ``INSERT`` instead" fallback — and since
            # ``is_new`` was already captured as ``False`` at the top of
            # ``save()``, neither ``_materialize_ports()`` nor
            # ``_materialize_companion()`` would run, resurrecting an
            # invalid orphan host with no ports and no companion. Fail
            # loudly instead of proceeding.
            raise ValidationError(
                f"Cannot save {self}: this device no longer exists in the database (deleted by "
                "another operation) — reload it before making further changes."
            )
        old_rack_id, old_rack_slot = persisted["rack_id"], persisted["rack_slot"]
        # Effective post-save values (Codex review round 2, finding 2), not
        # blindly ``self.rack_id``/``self.rack_slot`` — when ``update_fields``
        # names only one of the pair, ``super().save()`` below leaves the
        # other field's DB value untouched no matter what ``self`` holds in
        # memory. A caller that mutates both and then calls
        # ``save(update_fields=["rack"])`` must plan the companion's move
        # from the *persisted* old slot, not an unsaved dirty one that's
        # never actually going to be written for this call.
        new_rack_id = self.rack_id if normalized is None or "rack" in normalized else old_rack_id
        new_rack_slot = self.rack_slot if normalized is None or "rack_slot" in normalized else old_rack_slot
        if new_rack_id is not None and new_rack_slot is None:
            # An inconsistent in-memory state ("rack and rack_slot must
            # both be set or both be empty" — RackSlotAssignmentMixin.clean(),
            # backed by the networkdevice_rack_and_slot_together
            # CheckConstraint) that a bare save() can still reach here,
            # *before* the DB constraint would ever get a chance to reject
            # it — this runs ahead of super().save(). Caught explicitly so
            # the arithmetic below never has to add None to an int.
            raise ValidationError(
                "rack_slot is required when rack is set — cannot plan this device's companion "
                "move from an inconsistent in-memory state (rack and rack_slot must both be set "
                "or both be empty)."
            )
        # Read from the DB, not ``companion.rack_id``/``companion.rack_slot``
        # (Codex review round 3, finding 2) — ``companion`` here is whatever
        # ``_get_related()``'s reverse-relation cache holds, which
        # ``_finish_companion_move()`` never updates: that method writes
        # through a *separately fetched* instance
        # (``NetworkDevice._default_manager.get(pk=...)``), so reusing the
        # same host object for a second move reads the companion's
        # pre-*first*-move slot, computing every offset from stale state —
        # silently, no error, just a wrong target.
        #
        # Fetched *before* the "nothing to move" decision (moved from where
        # round 3 left it, right after this comment, to the very end of the
        # function) — round 4 finding 2's fix needs the companion's own
        # persisted position to decide whether *it* is moving, not just the
        # host.
        companion_persisted = (
            NetworkDevice._default_manager.filter(pk=companion.pk).values("rack_id", "rack_slot").first()
        )
        if companion_persisted is None:
            # Not "nothing to move" (Codex review round 5, finding 3, the
            # companion-side twin of round 4 finding 4's fix on the host's
            # own lookup above) — ``host`` is ``CASCADE``, so this is the
            # *more* likely branch to fire under a concurrent host
            # deletion: whether the race lands here or on the host's own
            # lookup above is decided by which of two unlocked reads a few
            # lines apart loses to the other transaction's commit.
            # Returning ``None`` here would let ``super().save()`` fall
            # through to the same zero-row-``UPDATE``→``INSERT`` fallback
            # that resurrects an orphan host with no ports and no
            # companion. Raising here doesn't make these reads locked —
            # they still run before ``save()``'s own
            # ``_lock_type_rows(NetworkDevice, self.pk, ...)`` a few lines
            # up its call site, since that call needs the companion's pk
            # from this method's return value in the first place — it
            # closes the silent-resurrection outcome, not the underlying
            # race window itself.
            raise ValidationError(
                f"Cannot move {self}: its companion no longer exists in the database (deleted by "
                "another operation) — reload it before making further changes."
            )
        companion_old_rack_id = companion_persisted["rack_id"]
        companion_old_rack_slot = companion_persisted["rack_slot"]
        companion_new_rack_id: int | None
        companion_new_rack_slot: int | None
        if new_rack_id is None:
            companion_new_rack_id = None
            companion_new_rack_slot = None
        else:
            companion_new_rack_id = new_rack_id
            if self._companion_rack_slot is not None:
                companion_new_rack_slot = self._companion_rack_slot
            elif companion_old_rack_slot is not None and old_rack_slot is not None:
                assert new_rack_slot is not None  # guarded above: new_rack_id set implies new_rack_slot set
                companion_new_rack_slot = new_rack_slot + (companion_old_rack_slot - old_rack_slot)
            else:
                raise ValidationError(
                    "companion_rack_slot is required: this move has no existing relative offset "
                    "to preserve (ADR 0018 decision 6) — the assembly was unracked, or the "
                    "companion had no rack_slot recorded."
                )
        host_unchanged = new_rack_id == old_rack_id and new_rack_slot == old_rack_slot
        companion_unchanged = (
            companion_new_rack_id == companion_old_rack_id
            and companion_new_rack_slot == companion_old_rack_slot
        )
        if host_unchanged and companion_unchanged:
            return None
        return {
            "companion_pk": companion.pk,
            "host_old_rack_id": old_rack_id,
            "host_old_rack_slot": old_rack_slot,
            "host_new_rack_id": new_rack_id,
            "host_new_rack_slot": new_rack_slot,
            "companion_old_rack_id": companion_old_rack_id,
            "companion_old_rack_slot": companion_old_rack_slot,
            "companion_new_rack_id": companion_new_rack_id,
            "companion_new_rack_slot": companion_new_rack_slot,
        }

    def _park_companion_if_colliding(self, pending_move: dict[str, Any]) -> None:
        """Vacate the companion first, but only when it's actually
        necessary (ADR 0018 review note 6) — the host's new target range
        overlapping the companion's *currently occupied* range, the only
        case where writing the host's own row first would land it on the
        companion's still-occupied slot and trip
        ``unique_device_rack_slot``. Every ordinary move (the common case)
        skips this, and ``_finish_companion_move()`` alone does a single,
        truthful ``save()``.

        When a park *is* needed, it's a real
        ``save(update_fields=["rack", "rack_slot"])`` under
        ``_host_managed_move``, not a ``QuerySet.update()`` — auditlog
        tracks these fields (``config/settings.py:244``), and ``update()``
        would leave a false ``None → target`` entry on the following
        placement instead of two truthful ones (``old → None``, then
        ``None → target``).
        """
        host_new_rack_id = pending_move["host_new_rack_id"]
        host_new_rack_slot = pending_move["host_new_rack_slot"]
        companion_old_rack_id = pending_move["companion_old_rack_id"]
        companion_old_rack_slot = pending_move["companion_old_rack_slot"]
        if (
            host_new_rack_id is None
            or host_new_rack_slot is None
            or companion_old_rack_id is None
            or companion_old_rack_slot is None
            or host_new_rack_id != companion_old_rack_id
        ):
            return
        companion = NetworkDevice._default_manager.get(pk=pending_move["companion_pk"])
        # Persisted, not self.slot_span (audit finding while fixing Codex
        # review round 5 — a third instance of round 4 finding 1's class:
        # self.slot_span reads through self.device_type, possibly dirty
        # under a save(update_fields=[...]) that never touches
        # device_type). The exact-tuple case unique_device_rack_slot
        # actually enforces doesn't depend on either span — host_start and
        # companion_start alone decide it, and neither reads slot_span —
        # so a dirty span here can only ever make this *more* willing to
        # skip a park the DB constraint wouldn't have needed anyway, never
        # less. Fixed to match round 4 finding 1's site regardless, so
        # nothing in this method depends on that argument staying true.
        persisted_self = NetworkDevice._default_manager.get(pk=self.pk)
        host_start, host_end = host_new_rack_slot, host_new_rack_slot + persisted_self.slot_span - 1
        companion_start = companion_old_rack_slot
        companion_end = companion_start + companion.slot_span - 1
        if not (host_start <= companion_end and companion_start <= host_end):
            return  # ranges don't actually overlap — no park needed
        companion.rack = None
        companion.rack_slot = None
        companion._host_managed_move = True
        companion.save(update_fields=["rack", "rack_slot"])

    def _finish_companion_move(self, pending_move: dict[str, Any]) -> None:
        """Place the companion at its final target — the second half of
        the vacate-then-place sequence (review note 6); the park in
        ``_park_companion_if_colliding()`` is a no-op unless the target
        actually collided, so most moves reach here as the *only*
        companion write, with a truthful ``old → new`` audit entry.

        Also validates both rows' addresses against their new placement
        (review note 2) — ``_validate_existing_addresses_still_fit()`` is
        only ever reached from ``clean()``, and ``save()`` never calls
        ``clean()``, so a bare ``host.save()`` (no ``full_clean()``) would
        otherwise silently leave a stale, now-out-of-range address on
        either row.

        Validates the **host's** addresses off a freshly re-fetched
        instance, not ``self`` directly (Codex review round 3, finding 3)
        — ``self.rack``/``self.rack_slot`` can hold a dirty in-memory value
        that ``update_fields`` excluded from this very ``save()`` call
        (e.g. ``save(update_fields=["rack"])`` after also mutating
        ``rack_slot`` in memory), and ``_address_containment_error()``
        silently skips its rack-range check whenever ``rack_slot`` is
        ``None`` — not a failure, just nothing checked. By the time this
        runs, ``super().save()`` has already written ``self``'s own row,
        so re-fetching gets exactly what ``update_fields`` actually
        persisted, the same effective-value fix ``_plan_companion_move()``
        already applies to the move plan itself, applied here to the
        address check that plan doesn't cover.

        Runs ``companion.full_clean()`` before saving (Codex review round
        2, finding 1) — the pair-vs-pair overlap is already pre-flighted
        by ``_check_companion_move_possible()``/``_park_companion_if_
        colliding()``, but nothing before this checked the companion's
        *own* target against a third row: another device's or switch's
        occupied range, or ``rack.slot_count``, neither backed by a DB
        constraint. ``_host_managed_move`` is set **before** calling
        ``full_clean()``, not just before ``save()`` — ``clean()`` is what
        actually reads it (via ``_locked_fields()``), so setting it any
        later would trip the very placement lock the flag exists to
        bypass. ``full_clean()`` subsumes the explicit address-fit call
        above (``RackSlotAssignmentMixin.clean()`` already runs it), and
        its own ``validate_unique()``/``validate_constraints()`` overrides
        correctly exclude this pair's own host via ``_companion_pair_
        pks()`` — a genuine conflict here can only be against an unrelated
        row.
        """
        NetworkDevice._default_manager.get(pk=self.pk)._validate_existing_addresses_still_fit()
        companion = NetworkDevice._default_manager.get(pk=pending_move["companion_pk"])
        companion.rack_id = pending_move["companion_new_rack_id"]
        companion.rack_slot = pending_move["companion_new_rack_slot"]
        companion._host_managed_move = True
        companion.full_clean()
        companion.save(update_fields=["rack", "rack_slot"])
        # ``self._companion_rack_slot`` is reset by ``save()`` itself,
        # right after ``_plan_companion_move()`` returns — not here (Codex
        # review round 5, finding 4) — because this method never runs at
        # all when the host and the companion's own explicit target both
        # already match what's persisted (a true no-op), and the input
        # was still consulted for that call. See ``save()``'s comment at
        # that reset for the full reasoning; round 4 finding 3 originally
        # placed the reset here, one exit path short.


class NetworkDevicePortQuerySet(models.QuerySet):
    """Blocks bulk ``QuerySet.delete()`` from orphaning an offset port
    (ADR 0017) — the model's own ``delete()`` override below only guards a
    single ``instance.delete()``; a queryset delete bypasses
    ``Model.delete()`` for every row, the same reason
    ``NetworkDeviceTypePortQuerySet``/``NetworkSwitchTypePortQuerySet``
    already carry their own ``delete()`` override alongside the model's.

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
                        "Cannot delete an offset-0 Network Device Port that still has offset "
                        "ports (ADR 0017) deriving their address from it — delete those first, "
                        "or delete the whole device."
                    )
            return super().delete()


class NetworkDevicePort(AuditedModel):
    """A device port: one purpose (VLAN), one static address or DHCP.

    Materialized exactly once from the device's ``device_type`` when the
    device is first created (``NetworkDevice._materialize_ports``) —
    static by default when racked, computed rack-range-base + rack-slot
    (or, at a non-zero ``slot_offset``, rack-range-base + rack-slot +
    offset — ADR 0017) like every other static address here, or DHCP-
    configured (``is_dhcp=True``, ``address=None``) when unracked or
    explicitly chosen (ADR 0013, revising ADR 0010's always-DHCP rule).
    ``description`` (this port's purpose), ``vlan``, ``port_type``, and
    ``slot_offset`` are locked hardware/purpose facts copied from the type
    port (ADR 0010, ADR 0017); ``is_dhcp``/``address``/``switch_port`` are
    editable — **except** ``address`` on a ``slot_offset > 0`` port, which
    is derived from the offset-0 port on the same ``(device, vlan)`` and
    locked (see ``_locked_fields()``/``_derive_offset_siblings()``): an
    operator can flip *that* port's addressing either way, and every
    offset sibling follows it automatically. Identity is ``(device,
    description)`` — ``port_number``, when present at all, is neither
    required nor unique.

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
    whose address the *hardware itself* computes from another port's and
    refuses to let anyone change, not general multi-jack/multi-part
    hardware. See ADR 0017's scope-boundary section.
    """

    #: Set only by ``_derive_offset_siblings()`` while it writes a derived
    #: address onto an offset sibling, and consulted by ``_locked_fields()``
    #: — the single legitimate writer of an offset port's otherwise-locked
    #: ``address``. Never a field, never persisted; a plain class-level
    #: default like ``created_by``'s pattern elsewhere in this module isn't
    #: used here because this genuinely never needs to be constructor-
    #: settable the way ``port_addressing``/``address_materialization`` are
    #: — nothing outside this class should ever set it.
    _deriving_address: bool = False

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
            "Address offset from the device's slot, copied from the device's type — locked "
            "after creation. Above 0, this port's address is derived from the offset-0 port "
            "on the same VLAN and can't be edited directly."
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
            if self.pk is not None:
                _check_locked_fields_unchanged(
                    NetworkDevicePort, self.pk, self._locked_fields(), update_fields=update_fields
                )
            # Lock both the old and new NetworkSwitchPort (connecting,
            # disconnecting, or reassigning switch_port) — the other half of
            # the race NetworkSwitchPort.save() guards against when its
            # profile changes; see _lock_switch_port_rows().
            persisted_switch_port_id = self._persisted_switch_port_id()
            _lock_switch_port_rows(self.switch_port_id, persisted_switch_port_id)

            # Snapshot the *persisted* address/is_dhcp before super().save()
            # overwrites them (ADR 0017 plan review, note 2) — needed only
            # to decide, after the write, whether to cascade a derived
            # recompute to this port's offset siblings. Restricted to an
            # existing (self.pk is not None) offset-0 port: a brand new row
            # has no persisted state to compare against and no siblings yet
            # to cascade to (materialization derives each offset port's own
            # address directly, not via this cascade — see
            # NetworkDevice._materialize_ports()); an offset>0 port is
            # itself a cascade *target*, never a trigger, so it never needs
            # this snapshot either.
            #
            # Gated on the *persisted* slot_offset, not self.slot_offset —
            # same reasoning as ``_locked_fields()`` (finding 2): by the
            # time we reach this line the lock check above has already
            # raised for any tampered slot_offset that would otherwise
            # matter, but keying this gate off self.slot_offset too would
            # (harmlessly, since the write above never happens without
            # the lock check's own guard passing) still needlessly ask
            # "should a value that was never truly this row's offset-0
            # state trigger a cascade" — using the persisted value keeps
            # this gate meaningful/correct on its own, independent of the
            # lock check above.
            pre_save_pk = self.pk
            persisted = None
            if pre_save_pk is not None and self._persisted_slot_offset() == 0:
                persisted = (
                    NetworkDevicePort._default_manager.filter(pk=pre_save_pk)
                    .values("address", "is_dhcp")
                    .first()
                )

            super().save(
                force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
            )

            if persisted is not None:
                # *Effective* post-save values, not necessarily self.address/
                # self.is_dhcp directly. When update_fields restricts the
                # write, a field it excludes keeps its persisted value in
                # the database regardless of whatever's dirty in memory —
                # e.g. save(update_fields=["address"]) with a stale,
                # never-actually-saved self.is_dhcp left over from some
                # earlier unrelated edit. Deriving straight from
                # self.is_dhcp/self.address would let that stale value
                # dictate every sibling's address even though the
                # database's own is_dhcp never changed; falling back to the
                # pre-save snapshot for an excluded field guarantees we
                # only ever derive from what this save actually persisted.
                normalized = _normalize_update_fields(NetworkDevicePort, update_fields)
                effective_address = (
                    self.address if (normalized is None or "address" in normalized) else persisted["address"]
                )
                effective_is_dhcp = (
                    self.is_dhcp if (normalized is None or "is_dhcp" in normalized) else persisted["is_dhcp"]
                )
                if effective_address != persisted["address"] or effective_is_dhcp != persisted["is_dhcp"]:
                    self._derive_offset_siblings(effective_address, effective_is_dhcp)

    def delete(self, using=None, keep_parents=False) -> tuple[int, dict[str, int]]:
        # ADR 0017: block deleting an offset-0 port that still has offset
        # siblings on its VLAN — they derive their address from this row,
        # and losing it would leave them locked, persisted, and
        # permanently pointed at nothing. Deliberately does *not* run for
        # a whole-device delete (device.delete() cascades to every port,
        # including these, via on_delete=CASCADE) — Django's deletion
        # Collector issues that DELETE directly, bypassing both this
        # override and NetworkDevicePortQuerySet.delete() (see that
        # queryset's docstring), so removing a device still works in one
        # step.
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
                    "Cannot delete an offset-0 Network Device Port that still has offset ports "
                    "(ADR 0017) deriving their address from it — delete those first, or delete "
                    "the whole device."
                )
            return super().delete(using=using, keep_parents=keep_parents)

    def clean(self) -> None:
        super().clean()
        if self.pk is not None:
            _check_locked_fields_unchanged(
                NetworkDevicePort, self.pk, self._locked_fields(), update_fields=None
            )
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
            if self.pk is None and not self.address and device is not None and vlan is not None:
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

        Casing is **not** normalised on this half: the suffix is already
        lowercased (``NetworkDeviceTypePort.save()``/``clean()``), but
        ``device.hostname`` is whatever the operator typed — production
        stores ``DM7C-1``, so this yields ``DM7C-1-device-control`` where
        the addressing sheet spells it ``dm7c-1-device-control``. Phase 18
        owns hostname casing; no test here may compare this property
        case-sensitively.
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
        # ``is_dhcp``/``address``/``switch_port`` are meant to be editable,
        # so a plain save() must not be able to silently move, renumber, or
        # reorder a materialized port, or change its offset.
        fields: dict[str, Any] = {
            "device": self.device_id,
            "port_number": self.port_number,
            "description": self.description,
            "vlan": self.vlan_id,
            "port_type": self.port_type,
            "ordinal": self.ordinal,
            "source_type_port": self.source_type_port_id,
            "slot_offset": self.slot_offset,
        }
        # ADR 0017: a slot_offset > 0 port's address is derived from the
        # offset-0 port on its VLAN, not independently settable — lock it
        # the same way as the identity fields above, *unless* this write is
        # the one privileged writer of a derived address
        # (_derive_offset_siblings(), which sets _deriving_address before
        # writing). An offset-0 port's address is never added here — it
        # stays editable exactly as ADR 0003 requires.
        #
        # Whether to lock ``address`` is decided from the *persisted*
        # slot_offset (``_persisted_slot_offset()``), not
        # ``self.slot_offset`` — slot_offset is itself one of the fields
        # above this method exists to protect. Trusting the in-memory
        # value here would let a caller set ``slot_offset = 0`` on an already-
        # materialized offset port and pair it with an address edit: the
        # bogus in-memory offset would drop ``"address"`` from this dict
        # *before* the caller ever compares ``slot_offset`` against what's
        # actually persisted, so with ``update_fields=["address"]`` the
        # intersection check in ``_check_locked_fields_unchanged()`` would
        # see neither key and return early — skipping the comparison
        # (including of ``slot_offset`` itself) entirely, letting a locked
        # address change slip through unchecked in the same save().
        persisted_offset = self._persisted_slot_offset()
        locked_offset = self.slot_offset if persisted_offset is None else persisted_offset
        if locked_offset > 0 and not self._deriving_address:
            fields["address"] = self.address
        return fields

    def _derive_offset_siblings(self, control_address: str | None, control_is_dhcp: bool) -> None:
        """Recompute every offset sibling's ``address``/``is_dhcp`` from
        the offset-0 control port's own *effective* post-save values (ADR
        0017) — called only from ``save()``, only for an offset-0 port
        whose persisted address/DHCP state this save actually changed (see
        ``save()`` for the snapshot-before-write reasoning that decides
        that, and for why ``control_address``/``control_is_dhcp`` are
        passed in rather than read from ``self.address``/``self.is_dhcp``
        directly — those attributes can hold a dirty, never-actually-
        persisted value when ``update_fields`` excluded them from this
        save).

        This is the single legitimate writer of a locked offset port's
        ``address`` — each sibling's ``_deriving_address`` is set before
        ``full_clean()``/``save()`` so ``_locked_fields()`` lifts the lock
        for exactly this write. Runs inside the caller's
        ``transaction.atomic()`` block (``save()``'s), so a derived address
        that collides with an existing one, falls outside the rack's
        range, or overflows past the top of IPv4 address space
        (``ipaddress.AddressValueError`` is caught and re-raised as a
        ``ValidationError`` below, since the former isn't one and would
        otherwise reach the admin as a bare 500) raises and rolls back the
        control edit that triggered this cascade too — nothing is left
        half-updated.

        Going DHCP (``control_is_dhcp`` or ``control_address is None``)
        takes every sibling to DHCP with it (``address=None``); coming
        back to static re-derives ``control_address + sibling.slot_offset``
        for each. Same reasoning ADR 0017 gives for deriving the engine
        address at all: an engine address stored against a control address
        that no longer exists is worse than one that's simply gone.
        """
        siblings = NetworkDevicePort.objects.filter(
            device_id=self.device_id, vlan_id=self.vlan_id, slot_offset__gt=0
        )
        for sibling in siblings:
            sibling._deriving_address = True
            if control_is_dhcp or control_address is None:
                sibling.is_dhcp = True
                sibling.address = None
            else:
                sibling.is_dhcp = False
                try:
                    sibling.address = str(ipaddress.IPv4Address(control_address) + sibling.slot_offset)
                except ipaddress.AddressValueError as exc:
                    raise ValidationError(
                        f"{sibling}'s derived address ({control_address} + {sibling.slot_offset}) "
                        "is not a valid IPv4 address — the control address is too close to "
                        "255.255.255.255 for this offset to fit."
                    ) from exc
            sibling.full_clean()
            sibling.save()

    def _persisted_switch_port_id(self) -> int | None:
        if self.pk is None:
            return None
        return (
            NetworkDevicePort._default_manager.filter(pk=self.pk)
            .values_list("switch_port_id", flat=True)
            .first()
        )

    def _persisted_slot_offset(self) -> int | None:
        if self.pk is None:
            return None
        return (
            NetworkDevicePort._default_manager.filter(pk=self.pk)
            .values_list("slot_offset", flat=True)
            .first()
        )

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

    def refresh_locked_offset_address(self) -> None:
        """Re-sync ``address``/``is_dhcp`` from what's currently persisted
        — offset ports only (ADR 0017). A no-op for an offset-0 port
        (never called on unsaved rows either) — see
        ``AuditedModelAdminMixin.save_formset()``, the only caller.

        Exists for exactly one race: ``NetworkDevicePortForm`` disables
        ``address`` on an offset row, so Django keeps that field's
        *initial* value (whatever was persisted at form-bind/GET time) on
        the instance the formset constructs — never anything the user
        actually submitted, since a disabled field accepts no input. In
        one admin submission that edits both the offset-0 control row's
        address *and* some other editable field on an offset row (e.g.
        ``switch_port``), saving the control row cascades a fresh derived
        address onto every offset sibling in the database
        (``_derive_offset_siblings()``) — but the offset row's own
        formset-built instance was already constructed before any of this
        ran, so its ``address`` is now stale against what's actually
        persisted. Without this, that stale instance's own save would look
        like a locked-field violation to ``_check_locked_fields_unchanged()``
        even though nothing the user did asked to change the address —
        rejecting an otherwise entirely valid submission. Safe to simply
        overwrite: the disabled field guarantees the in-memory value was
        never user intent to begin with, so there is nothing to lose here.
        """
        if self.pk is None:
            return
        persisted_offset = self._persisted_slot_offset()
        if not persisted_offset:
            return
        current = NetworkDevicePort._default_manager.filter(pk=self.pk).values("address", "is_dhcp").first()
        if current is not None:
            self.address = current["address"]
            self.is_dhcp = current["is_dhcp"]

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
