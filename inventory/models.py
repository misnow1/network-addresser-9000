"""Domain models for Network Addresser 9000.

Canonical terminology lives in CONTEXT.md; design rationale and trade-offs
behind specific fields/relations are recorded as ADRs in docs/adr/. Address
suggestion and overlap validation (phase 3, see ROADMAP.md) live here too:
suggestion arithmetic itself is in suggestions.py, wired into each model's
``clean()`` so a blank suggested field is filled in on creation only —
matching ADR 0001's "suggests, but admin can override; once set, static."
"""

import ipaddress
from typing import Any

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from .suggestions import (
    ranges_overlap,
    required_block_size,
    suggest_default_gateway,
    suggest_dhcp_range,
    suggest_rack_vlan_range,
    suggest_slot_address,
)
from .validators import validate_ipv4_cidr


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
    return suggest_slot_address(rack_range.address_range, rack_slot)


def _address_containment_error(
    address: str, vlan: "VLAN", rack: "Rack | None", rack_slot: int | None
) -> str | None:
    """``None`` if ``address`` fits ``vlan``'s subnet (and, if racked with an
    assigned ``RackVlanRange``, that range too); otherwise an error message.

    Pure read — no exclusions/uniqueness here, so it doubles as the check
    for re-validating an *already-saved* address after its equipment moves,
    not just for a fresh/edited address row.
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
            return None
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
            used_ranges = list(vlan.rack_ranges.exclude(pk=self.pk).values_list("address_range", flat=True))
            if vlan.dhcp_range:
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
    """A switch make/model. port_type describes the physical port mix."""

    manufacturer = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    port_count = models.PositiveIntegerField()
    port_type = models.CharField(
        max_length=200,
        help_text='e.g. "8x 1GbE Copper + 2x 1GbE Combo".',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["manufacturer", "model"], name="unique_switch_type"),
        ]
        ordering = ["manufacturer", "model"]

    def __str__(self) -> str:
        return f"{self.manufacturer} {self.model}"


class NetworkSwitch(RackSlotAssignmentMixin, AuditedModel):
    """A physical switch instance. Unracked (rack is null) = spare pool."""

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
    """A single physical port on a switch — L2 config only, no address."""

    class PortMode(models.TextChoices):
        TRUNK = "trunk", "Trunk"
        ACCESS = "access", "Access"

    switch = models.ForeignKey(NetworkSwitch, on_delete=models.CASCADE, related_name="ports")
    port_number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    description = models.CharField(max_length=255, blank=True)
    port_type = models.CharField(max_length=50, blank=True, help_text="e.g. 1GbE, 10GbE SFP+.")
    port_mode = models.CharField(max_length=10, choices=PortMode.choices, default=PortMode.ACCESS)
    native_vlan = models.ForeignKey(
        VLAN,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="Primary (untagged) VLAN for this port.",
    )
    allowed_vlans = models.ManyToManyField(VLAN, blank=True, related_name="+")

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

    def clean(self) -> None:
        super().clean()
        if self.switch_id and self.port_number and self.port_number > self.switch.switch_type.port_count:
            raise ValidationError(
                f"port_number {self.port_number} exceeds {self.switch.switch_type}'s "
                f"port_count ({self.switch.switch_type.port_count})."
            )


class NetworkDeviceType(AuditedModel):
    """A device make/model — amp, processor, mixer, etc."""

    manufacturer = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    port_count = models.PositiveIntegerField()
    port_type = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["manufacturer", "model"], name="unique_device_type"),
        ]
        ordering = ["manufacturer", "model"]

    def __str__(self) -> str:
        return f"{self.manufacturer} {self.model}"


class NetworkDevice(RackSlotAssignmentMixin, AuditedModel):
    """An end-point device instance. Unracked (rack is null) = spare pool."""

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

    ``switch`` is not stored directly — it's redundant with (and could
    contradict) ``switch_port``, so it's derived from it via a property.
    ``switch_port`` is a one-to-one: a physical switch port can be claimed
    by at most one device port.
    """

    device = models.ForeignKey(NetworkDevice, on_delete=models.CASCADE, related_name="ports")
    port_number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    description = models.CharField(max_length=255, blank=True, help_text='e.g. "Dante Primary".')
    vlan = models.ForeignKey(VLAN, on_delete=models.PROTECT, related_name="device_ports")
    is_dhcp = models.BooleanField(default=False)
    address = models.GenericIPAddressField(protocol="IPv4", null=True, blank=True)
    default_gateway = models.GenericIPAddressField(protocol="IPv4", null=True, blank=True)
    switch_port = models.OneToOneField(
        NetworkSwitchPort,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="connected_device_port",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["device", "port_number"], name="unique_device_port_number"),
            models.UniqueConstraint(fields=["vlan", "address"], name="unique_device_port_vlan_address_value"),
            models.CheckConstraint(
                condition=(
                    models.Q(is_dhcp=True, address__isnull=True, default_gateway__isnull=True)
                    | models.Q(is_dhcp=False, address__isnull=False)
                ),
                name="device_port_dhcp_xor_static_address",
            ),
        ]
        ordering = ["device", "port_number"]

    def __str__(self) -> str:
        return f"{self.device} port {self.port_number}"

    @property
    def switch(self) -> "NetworkSwitch | None":
        switch_port = self.switch_port
        return switch_port.switch if switch_port is not None else None

    def clean(self) -> None:
        super().clean()
        if self.is_dhcp:
            if self.address or self.default_gateway:
                raise ValidationError("DHCP ports must not have a static address or gateway.")
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
        if self.device_id and self.port_number and self.port_number > self.device.device_type.port_count:
            raise ValidationError(
                f"port_number {self.port_number} exceeds {self.device.device_type}'s "
                f"port_count ({self.device.device_type.port_count})."
            )
