"""Domain models for Network Addresser 9000.

Canonical terminology lives in CONTEXT.md; design rationale and trade-offs
behind specific fields/relations are recorded as ADRs in docs/adr/. Address
suggestion and overlap validation (phase 3, see ROADMAP.md) live here too:
suggestion arithmetic itself is in suggestions.py, wired into each model's
``clean()`` so a blank suggested field is filled in on creation only —
matching ADR 0001's "suggests, but admin can override; once set, static."
"""

import ipaddress

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from .suggestions import (
    ranges_overlap,
    suggest_default_gateway,
    suggest_dhcp_range,
    suggest_rack_vlan_range,
    suggest_slot_address,
)
from .validators import validate_ipv4_cidr


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
        if self.pk is None and self.subnet:
            try:
                validate_ipv4_cidr(self.subnet)
            except ValidationError:
                return  # subnet itself is invalid; clean_fields() already reports it
            if not self.default_gateway:
                self.default_gateway = suggest_default_gateway(self.subnet)
            if not self.dhcp_range:
                suggestion = suggest_dhcp_range(self.subnet)
                if suggestion:
                    self.dhcp_range = suggestion


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
        if self.pk is None and not self.address_range and self.rack_id and self.vlan_id:
            used_ranges = list(
                self.vlan.rack_ranges.exclude(pk=self.pk).values_list("address_range", flat=True)
            )
            if self.vlan.dhcp_range:
                used_ranges.append(self.vlan.dhcp_range)
            try:
                validate_ipv4_cidr(self.vlan.subnet)
            except ValidationError:
                pass  # VLAN's own subnet is invalid; nothing sensible to suggest
            else:
                suggestion = suggest_rack_vlan_range(self.vlan.subnet, self.rack.slot_count, used_ranges)
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
        try:
            validate_ipv4_cidr(self.vlan.subnet)
        except ValidationError:
            return  # VLAN's own subnet is invalid; its own clean() will report that
        vlan_network = ipaddress.IPv4Network(self.vlan.subnet, strict=True)
        range_network = ipaddress.IPv4Network(self.address_range, strict=True)
        if not range_network.subnet_of(vlan_network):
            raise ValidationError(
                {
                    "address_range": (
                        f"{self.address_range} is not within {self.vlan}'s subnet ({self.vlan.subnet})."
                    )
                }
            )
        for other in self.vlan.rack_ranges.exclude(pk=self.pk):
            if ranges_overlap(self.address_range, other.address_range):
                raise ValidationError(
                    {
                        "address_range": (
                            f"{self.address_range} overlaps {other.rack}'s range "
                            f"{other.address_range} on {self.vlan}."
                        )
                    }
                )
        if self.vlan.dhcp_range and ranges_overlap(self.address_range, self.vlan.dhcp_range):
            raise ValidationError(
                {
                    "address_range": (
                        f"{self.address_range} overlaps {self.vlan}'s DHCP range ({self.vlan.dhcp_range})."
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

    def _check_rack_slot_not_occupied(self) -> None:
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
        if self.pk is None and not self.address and self.switch_id and self.vlan_id:
            suggestion = _suggest_rack_slot_address(self.switch.rack, self.switch.rack_slot, self.vlan_id)
            if suggestion:
                self.address = suggestion
        if not self.address:
            raise ValidationError(
                {
                    "address": (
                        "This field is required — no suggestion could be computed "
                        "automatically (switch must be racked with a RackVlanRange "
                        "already assigned for this VLAN), so it must be entered manually."
                    )
                }
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
            if self.pk is None and not self.address and self.device_id and self.vlan_id:
                suggestion = _suggest_rack_slot_address(self.device.rack, self.device.rack_slot, self.vlan_id)
                if suggestion:
                    self.address = suggestion
            if not self.address:
                raise ValidationError("Static ports must have an address.")
        if self.device_id and self.port_number and self.port_number > self.device.device_type.port_count:
            raise ValidationError(
                f"port_number {self.port_number} exceeds {self.device.device_type}'s "
                f"port_count ({self.device.device_type.port_count})."
            )
