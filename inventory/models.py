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
"""

import ipaddress
from typing import Any

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction

from .suggestions import (
    ranges_overlap,
    required_block_size,
    suggest_default_gateway,
    suggest_dhcp_range,
    suggest_rack_vlan_range,
    suggest_slot_address,
)
from .validators import validate_ipv4_cidr


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
    ``NetworkSwitchTypePort.allowed_vlans`` isn't itself a locked field
    checked here, but a locked type port's allowed VLANs can still be
    changed via ``.add()``/``.remove()``/``.set()``/``.clear()`` on the
    M2M manager — those write ``NetworkSwitchTypePortAllowedVlan`` (the
    through table) directly and never call ``NetworkSwitchTypePort.save()``
    or its ``_profile_locked()`` check. Unsupported for now; see ADR 0010.
    """
    if update_fields is not None:
        attname_to_name = {
            field.attname: field.name
            for field in model_cls._meta.concrete_fields
            if field.attname != field.name
        }
        normalized_update_fields = {attname_to_name.get(name, name) for name in update_fields}
        if not (set(current_values) & normalized_update_fields):
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
    incomplete. Device type ports have no numbering requirement (unlike
    switch type ports) since ``port_number`` is optional for these.
    """
    count = device_type.type_ports.count()
    if count != device_type.port_count:
        raise ValidationError(
            f"{device_type} declares port_count {device_type.port_count} but has "
            f"{count} Network Device Type Port(s) defined — define all of them before "
            "creating a device of this type."
        )


def _suggest_rack_slot_address(rack: "Rack | None", rack_slot: int | None, vlan_id: int) -> str | None:
    """Suggested static address for a rack-slot occupant on ``vlan_id``.

    ``None`` if unracked, or no ``RackVlanRange`` exists yet for that VLAN.
    Shared by ``NetworkSwitchAddress`` and ``NetworkDevicePort``.
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
    return suggest_slot_address(rack_range.address_range, rack_slot)


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
    try:
        validate_ipv4_cidr(vlan.subnet)
    except ValidationError:
        return None  # VLAN's own subnet is invalid; its own clean() will report that
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


class VLAN(AuditedModel):
    """An 802.1Q VLAN and its IPv4 addressing — one row, per CONTEXT.md."""

    name = models.CharField(max_length=100)
    vlan_id = models.PositiveIntegerField(
        unique=True,
        validators=[MinValueValidator(1), MaxValueValidator(4094)],
        help_text="802.1Q VLAN ID (1-4094).",
    )
    subnet = models.CharField(
        max_length=18,
        validators=[validate_ipv4_cidr],
        help_text="IPv4 subnet in CIDR notation, e.g. 10.200.0.0/21.",
    )
    default_gateway = models.GenericIPAddressField(
        protocol="IPv4",
        blank=True,
        null=True,
        help_text="Suggested as the lowest host address in the subnet; stored and overridable.",
    )
    dhcp_range = models.CharField(
        max_length=18,
        blank=True,
        validators=[validate_ipv4_cidr],
        help_text="Suggested as the bottom /24 of the subnet; stored and overridable.",
    )

    class Meta:
        ordering = ["vlan_id"]

    def __str__(self) -> str:
        return f"{self.name} (VLAN {self.vlan_id})"

    def clean(self) -> None:
        super().clean()
        if not self.subnet:
            return
        try:
            validate_ipv4_cidr(self.subnet)
        except ValidationError:
            return  # subnet itself is invalid; clean_fields() already reports it
        vlan_network = ipaddress.IPv4Network(self.subnet, strict=True)

        if self.pk is None:
            if not self.default_gateway:
                suggestion = suggest_default_gateway(self.subnet)
                if suggestion:
                    self.default_gateway = suggestion
            if not self.dhcp_range:
                suggestion = suggest_dhcp_range(self.subnet)
                if suggestion:
                    self.dhcp_range = suggestion

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

        dhcp_network = None
        if self.dhcp_range:
            try:
                dhcp_network = ipaddress.IPv4Network(self.dhcp_range, strict=True)
            except ValueError:
                pass  # malformed value; the field's own validator already reports it
            else:
                if not dhcp_network.subnet_of(vlan_network):
                    raise ValidationError(
                        {"dhcp_range": f"{self.dhcp_range} is not within subnet {self.subnet}."}
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
                if dhcp_network is not None and dhcp_network.overlaps(range_network):
                    raise ValidationError(
                        {
                            "dhcp_range": (
                                f"{self.dhcp_range} overlaps {rack_range.rack}'s existing range "
                                f"({rack_range.address_range})."
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


class Rack(AuditedModel):
    """A physical container with a fixed slot count.

    Has no "purpose" field by design — a spare rack is an ordinary Rack
    whose slots happen to hold spare equipment (CONTEXT.md).
    """

    name = models.CharField(max_length=100)
    slot_count = models.PositiveIntegerField()

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        if self.pk is None:
            return  # nothing assigned yet on a not-yet-created rack
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
        help_text="Leave blank to suggest the next free block sized for the rack's slot_count.",
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
        if self.pk is None and not self.address_range and rack is not None and vlan is not None:
            used_ranges = []
            for value in vlan.rack_ranges.exclude(pk=self.pk).values_list("address_range", flat=True):
                try:
                    validate_ipv4_cidr(value)
                except ValidationError:
                    continue  # that sibling range's own malformed value; its own clean() reports it
                used_ranges.append(value)
            if vlan.dhcp_range:
                try:
                    validate_ipv4_cidr(vlan.dhcp_range)
                except ValidationError:
                    pass  # VLAN's own malformed dhcp_range; its own clean() reports it
                else:
                    used_ranges.append(vlan.dhcp_range)
            try:
                validate_ipv4_cidr(vlan.subnet)
            except ValidationError:
                pass  # VLAN's own subnet is invalid; nothing sensible to suggest
            else:
                suggestion = suggest_rack_vlan_range(vlan.subnet, rack.slot_count, used_ranges)
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
        if vlan.dhcp_range and ranges_overlap(self.address_range, vlan.dhcp_range):
            raise ValidationError(
                {"address_range": f"{self.address_range} overlaps {vlan}'s DHCP range ({vlan.dhcp_range})."}
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
    are set, ``rack_slot`` must fall within the rack's ``slot_count`` — this
    last check is cross-table so it can't be expressed as a DB constraint.

    Also cross-checks the *other* equipment table so a switch and a device
    can't both claim the same physical slot. This is an interim, form/
    full_clean-time guard, not a concurrency-safe one — a shared rack-
    occupancy table would be needed to close the direct-ORM/race-condition
    gap; that's a bigger schema change better suited to phase 3's "Overlap
    validation" work (see ROADMAP.md) than a scaffolding fix.
    """

    rack: Rack | None
    rack_slot: int | None

    pk: int | None

    def clean(self) -> None:
        super().clean()  # type: ignore[misc]
        if (self.rack is None) != (self.rack_slot is None):
            raise ValidationError(
                "rack and rack_slot must both be set (racked) or both be empty (spare pool)."
            )
        if self.rack is not None and self.rack_slot is not None:
            if self.rack_slot > self.rack.slot_count:
                raise ValidationError(
                    f"rack_slot {self.rack_slot} exceeds {self.rack}'s slot_count ({self.rack.slot_count})."
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

    Known gap (documented, not closed): ``allowed_vlans.add()``/
    ``.remove()``/``.set()``/``.clear()`` bypass this lock — see the
    "Known gap" note on ``_check_locked_fields_unchanged`` and ADR 0010.
    """

    switch_type = models.ForeignKey(NetworkSwitchType, on_delete=models.CASCADE, related_name="type_ports")
    port_number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    description = models.CharField(max_length=255, blank=True)
    port_type = models.CharField(max_length=20, choices=PortType.choices)
    port_mode = models.CharField(max_length=10, choices=PortMode.choices, default=PortMode.ACCESS)
    native_vlan = models.ForeignKey(
        VLAN,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="Primary (untagged) VLAN for this port.",
    )
    allowed_vlans: models.ManyToManyField = models.ManyToManyField(
        VLAN, through="NetworkSwitchTypePortAllowedVlan", related_name="+", blank=True
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
            _lock_type_rows(NetworkSwitchType, self.switch_type_id, self._persisted_switch_type_id())
            if self._profile_locked() or self._persisted_profile_locked():
                raise ValidationError(
                    "This profile's ports are locked because it already has switch instances; "
                    "create a new named profile to change the port layout."
                )
            super().save(
                force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
            )

    def delete(self, using=None, keep_parents=False) -> tuple[int, dict[str, int]]:
        with transaction.atomic():
            _lock_type_rows(NetworkSwitchType, self.switch_type_id)
            if self._profile_locked():
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

    def _persisted_profile_locked(self) -> bool:
        """Whether this row's *persisted* parent (before any in-memory
        reassignment) is locked. ``_profile_locked()`` alone only sees the
        in-memory ``switch_type`` — reassigning a locked type port to a
        different, unlocked profile would otherwise pass that check and
        silently move the row out from under the locked profile.
        """
        original_switch_type_id = self._persisted_switch_type_id()
        if original_switch_type_id is None:
            return False
        return NetworkSwitchType._default_manager.filter(
            pk=original_switch_type_id, switches__isnull=False
        ).exists()


class NetworkSwitchTypePortAllowedVlan(AuditedModel):
    """Explicit through model for ``NetworkSwitchTypePort.allowed_vlans``.

    A plain M2M's auto-generated join table has no ``on_delete`` to set, so
    it can't protect a VLAN from removal (ADR 0007) — this explicit model
    gives the ``vlan`` side a real ``PROTECT`` FK, which Django's deletion
    collector honors for both ``Model.delete()`` and bulk
    ``QuerySet.delete()``/admin bulk-delete alike.
    """

    type_port = models.ForeignKey(
        NetworkSwitchTypePort, on_delete=models.CASCADE, related_name="allowed_vlan_links"
    )
    vlan = models.ForeignKey(VLAN, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["type_port", "vlan"], name="unique_switch_type_port_allowed_vlan"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.type_port} allows {self.vlan}"


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
        # ``self._state.adding``, not ``self.pk is None`` — a pk can be
        # pre-assigned (fixtures, scripted inserts) before the row actually
        # exists, and self.pk is None would then wrongly skip materialization.
        is_new = self._state.adding
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

    def clean(self) -> None:
        super().clean()
        if self._state.adding:
            switch_type = _get_related(self, "switch_type")
            if switch_type is not None and switch_type.pk is not None:
                _validate_switch_type_port_profile(switch_type)
        else:
            _check_locked_fields_unchanged(
                NetworkSwitch, self.pk, {"switch_type": self.switch_type_id}, update_fields=None
            )

    def _materialize_ports(self) -> None:
        """One-time copy of ``switch_type``'s Network Switch Type Ports into
        real ``NetworkSwitchPort`` rows (ADR 0010). Runs inside the same
        transaction as this switch's insert (see ``save()``), so an
        incomplete profile or a failed child row leaves neither the switch
        nor any partial ports behind.
        """
        _validate_switch_type_port_profile(self.switch_type)
        for type_port in self.switch_type.type_ports.all():
            port = NetworkSwitchPort.objects.create(
                switch=self,
                port_number=type_port.port_number,
                description=type_port.description,
                port_type=type_port.port_type,
                port_mode=type_port.port_mode,
                native_vlan=type_port.native_vlan,
                source_type_port=type_port,
                created_by=self.created_by,
            )
            vlan_ids = list(type_port.allowed_vlans.values_list("pk", flat=True))
            if vlan_ids:
                port.allowed_vlans.set(vlan_ids)

    def _check_rack_slot_not_occupied(self) -> None:
        if NetworkDevice.objects.filter(rack=self.rack, rack_slot=self.rack_slot).exists():
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


class NetworkSwitchPort(AuditedModel):
    """A single physical port on a switch — L2 config only, no address.

    Materialized exactly once from the switch's ``switch_type`` when the
    switch is first created (``NetworkSwitch._materialize_ports``).
    ``port_type`` is a locked hardware fact copied from the type port;
    everything else here (VLAN purpose, description, mode) is editable per
    switch — in contrast to ``NetworkDevicePort``, where purpose/VLAN are
    locked too (ADR 0010).
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
    port_mode = models.CharField(max_length=10, choices=PortMode.choices, default=PortMode.ACCESS)
    native_vlan = models.ForeignKey(
        VLAN,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="Primary (untagged) VLAN for this port.",
    )
    allowed_vlans: models.ManyToManyField = models.ManyToManyField(
        VLAN, through="NetworkSwitchPortAllowedVlan", related_name="+", blank=True
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
        if self.pk is not None:
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

    def _locked_fields(self) -> dict[str, Any]:
        # ``switch``/``port_number``/``source_type_port`` identify which
        # physical port this row represents (materialized once from the
        # switch's type, ADR 0010) — only VLAN purpose/description/mode
        # are meant to be editable per switch, so a plain save() must not
        # be able to silently move or renumber a materialized port.
        return {
            "switch": self.switch_id,
            "port_number": self.port_number,
            "port_type": self.port_type,
            "source_type_port": self.source_type_port_id,
        }


class NetworkSwitchPortAllowedVlan(AuditedModel):
    """Explicit through model for ``NetworkSwitchPort.allowed_vlans`` — see
    ``NetworkSwitchTypePortAllowedVlan`` for why a plain M2M can't protect
    a VLAN from removal.
    """

    port = models.ForeignKey(NetworkSwitchPort, on_delete=models.CASCADE, related_name="allowed_vlan_links")
    vlan = models.ForeignKey(VLAN, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["port", "vlan"], name="unique_switch_port_allowed_vlan"),
        ]

    def __str__(self) -> str:
        return f"{self.port} allows {self.vlan}"


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
            if self.pk is not None:
                _lock_type_rows(NetworkDeviceType, self.pk)
                if self.devices.exists():
                    _check_locked_fields_unchanged(
                        NetworkDeviceType,
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
        if self.pk is not None and self.devices.exists():
            _check_locked_fields_unchanged(
                NetworkDeviceType,
                self.pk,
                {
                    "manufacturer": self.manufacturer,
                    "model": self.model,
                    "name": self.name,
                    "port_count": self.port_count,
                },
                update_fields=None,
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
    """

    device_type = models.ForeignKey(NetworkDeviceType, on_delete=models.CASCADE, related_name="type_ports")
    port_number = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])
    description = models.CharField(
        max_length=255, help_text='Required — this port\'s purpose/identity, e.g. "Dante Primary".'
    )
    port_type = models.CharField(max_length=20, choices=PortType.choices)
    vlan = models.ForeignKey(VLAN, on_delete=models.PROTECT, related_name="+")
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
            _lock_type_rows(NetworkDeviceType, self.device_type_id, self._persisted_device_type_id())
            if self._profile_locked() or self._persisted_profile_locked():
                raise ValidationError(
                    "This profile's ports are locked because it already has device instances; "
                    "create a new named profile to change the port layout."
                )
            self._assign_ordinal_if_unset()
            super().save(
                force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
            )

    def delete(self, using=None, keep_parents=False) -> tuple[int, dict[str, int]]:
        with transaction.atomic():
            _lock_type_rows(NetworkDeviceType, self.device_type_id)
            if self._profile_locked():
                raise ValidationError(
                    "This profile's ports are locked because it already has device instances; "
                    "create a new named profile to change the port layout."
                )
            return super().delete(using=using, keep_parents=keep_parents)

    def clean(self) -> None:
        super().clean()
        if self._profile_locked():
            raise ValidationError(
                "This profile's ports are locked because it already has device instances; "
                "create a new named profile to change the port layout."
            )
        self._assign_ordinal_if_unset()

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

    def _persisted_profile_locked(self) -> bool:
        """Whether this row's *persisted* parent (before any in-memory
        reassignment) is locked — see ``NetworkSwitchTypePort``'s version
        for why ``_profile_locked()`` alone isn't enough.
        """
        original_device_type_id = self._persisted_device_type_id()
        if original_device_type_id is None:
            return False
        return NetworkDeviceType._default_manager.filter(
            pk=original_device_type_id, devices__isnull=False
        ).exists()

    def _assign_ordinal_if_unset(self) -> None:
        if self.pk is not None or self.ordinal:
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
    hostname = models.CharField(max_length=255, blank=True)
    serial_number = models.CharField(max_length=100, blank=True)
    rack = models.ForeignKey(Rack, on_delete=models.PROTECT, null=True, blank=True, related_name="devices")
    rack_slot = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])

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
        # ``self._state.adding``, not ``self.pk is None`` — see NetworkSwitch.save().
        is_new = self._state.adding
        with transaction.atomic():
            if not is_new:
                _check_locked_fields_unchanged(
                    NetworkDevice, self.pk, {"device_type": self.device_type_id}, update_fields=update_fields
                )
            elif self.device_type_id is not None:
                # Locks the type row so a concurrent edit to its port
                # templates/count can't interleave with this materialization.
                _lock_type_rows(NetworkDeviceType, self.device_type_id)
            super().save(
                force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
            )
            if is_new:
                self._materialize_ports()

    def clean(self) -> None:
        super().clean()
        if self._state.adding:
            device_type = _get_related(self, "device_type")
            if device_type is not None and device_type.pk is not None:
                _validate_device_type_port_profile(device_type)
        else:
            _check_locked_fields_unchanged(
                NetworkDevice, self.pk, {"device_type": self.device_type_id}, update_fields=None
            )

    def _materialize_ports(self) -> None:
        """One-time copy of ``device_type``'s Network Device Type Ports into
        real ``NetworkDevicePort`` rows, materialized as DHCP (ADR 0010) —
        an operator gives a port a static address afterward. Runs inside
        the same transaction as this device's insert (see ``save()``).
        """
        _validate_device_type_port_profile(self.device_type)
        for type_port in self.device_type.type_ports.order_by("ordinal"):
            NetworkDevicePort.objects.create(
                device=self,
                port_number=type_port.port_number,
                description=type_port.description,
                vlan=type_port.vlan,
                port_type=type_port.port_type,
                ordinal=type_port.ordinal,
                source_type_port=type_port,
                is_dhcp=True,
                address=None,
                created_by=self.created_by,
            )

    def _check_rack_slot_not_occupied(self) -> None:
        if NetworkSwitch.objects.filter(rack=self.rack, rack_slot=self.rack_slot).exists():
            raise ValidationError(
                f"Rack slot {self.rack_slot} in {self.rack} is already occupied by a switch."
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


class NetworkDevicePort(AuditedModel):
    """A device port: one purpose (VLAN), one static address or DHCP.

    Materialized exactly once from the device's ``device_type`` when the
    device is first created (``NetworkDevice._materialize_ports``),
    starting out DHCP-configured (``is_dhcp=True``, ``address=None``) — an
    operator gives it a static address afterward. ``description`` (this
    port's purpose), ``vlan``, and ``port_type`` are locked hardware/
    purpose facts copied from the type port (ADR 0010); only
    ``is_dhcp``/``address``/``switch_port`` are editable. Identity is
    ``(device, description)`` — ``port_number``, when present at all, is
    neither required nor unique.

    ``switch`` is not stored directly — it's redundant with (and could
    contradict) ``switch_port``, so it's derived from it via a property.
    ``switch_port`` is a one-to-one: a physical switch port can be claimed
    by at most one device port.

    Known limitation (deferred, see DESIGN.md and ADR 0010): a device port
    is always single-VLAN/single-address. Hardware where two physical jacks
    are bridged into one logical interface (e.g. Shure ULXD4Q/D "Switched"
    mode) can be tracked as two ordinary ports here, but the system won't
    stop them from being given two different addresses, which wouldn't
    reflect the real, single-IP hardware.
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
    ordinal = models.PositiveIntegerField(editable=False, default=0)
    # Materialization always passes is_dhcp=True explicitly regardless of
    # this default (ADR 0010) — this stays False so directly-constructed
    # ports (tests, or any future non-materialization path) keep defaulting
    # to static, matching every other static-address model in this file.
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
        if self.pk is not None:
            _check_locked_fields_unchanged(
                NetworkDevicePort, self.pk, self._locked_fields(), update_fields=update_fields
            )
        super().save(
            force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields
        )

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
                suggestion = _suggest_rack_slot_address(device.rack, device.rack_slot, vlan.pk)
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

    def _locked_fields(self) -> dict[str, Any]:
        # ``device``/``port_number``/``ordinal``/``source_type_port``
        # identify which physical port this row represents (materialized
        # once from the device's type, ADR 0010) — only
        # ``is_dhcp``/``address``/``switch_port`` are meant to be editable,
        # so a plain save() must not be able to silently move, renumber, or
        # reorder a materialized port.
        return {
            "device": self.device_id,
            "port_number": self.port_number,
            "description": self.description,
            "vlan": self.vlan_id,
            "port_type": self.port_type,
            "ordinal": self.ordinal,
            "source_type_port": self.source_type_port_id,
        }

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
