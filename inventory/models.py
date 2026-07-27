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
from typing import Any

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction

from .suggestions import (
    dhcp_range_overlaps_cidr,
    ranges_overlap,
    required_block_size,
    suggest_default_gateway,
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


class PortAddressing(models.TextChoices):
    """Creation-time choice of how a device's ports materialize (ADR 0013).

    Transient — never stored (see ``NetworkDevice.port_addressing``); the
    materialized ``NetworkDevicePort`` rows themselves are the record of
    what was chosen.
    """

    STATIC = "static", "Static"
    DHCP = "dhcp", "DHCP"


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
    ``SwitchPortVlanProfile.allowed_vlans`` isn't itself a field this helper
    checks (M2M managers don't go through ``Model.save()`` at all — see ADR
    0012), so its own locking/validation lives elsewhere: an ``m2m_changed``
    receiver for ``.add()``/``.set()``/``.clear()``, and
    ``SwitchPortVlanProfileAllowedVlan.save()``/``.clean()`` for direct
    through-row writes. A raw ``bulk_create()`` against that through table
    still bypasses both and is unsupported, consistent with the
    ``QuerySet.update()``/``bulk_create()`` gap above.
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
    try:
        return suggest_slot_address(rack_range.address_range, rack_slot)
    except ValueError:
        # rack_slot bypassed clean()'s rack_slot <= rack.slot_count guard
        # (save() alone never enforces it) and overflows this range's block —
        # its own clean() would report that.
        return None


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

    def clean(self) -> None:
        super().clean()
        if self.pk is None or self._state.adding:
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
            persisted_device_type_id = self._persisted_device_type_id()
            _lock_type_rows(NetworkDeviceType, self.device_type_id, persisted_device_type_id)
            if self._profile_locked() or self._persisted_profile_locked(persisted_device_type_id):
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
    hostname = models.CharField(max_length=255, blank=True)
    serial_number = models.CharField(max_length=100, blank=True)
    rack = models.ForeignKey(Rack, on_delete=models.PROTECT, null=True, blank=True, related_name="devices")
    rack_slot = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])

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
        ]
        ordering = ["hostname"]

    def __str__(self) -> str:
        return self.hostname or f"Device #{self.pk}"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None) -> None:
        # ``self.pk is None or self._state.adding`` — see NetworkSwitch.save().
        is_new = self.pk is None or self._state.adding
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
        if self.pk is None or self._state.adding:
            device_type = _get_related(self, "device_type")
            if device_type is not None and device_type.pk is not None:
                _validate_device_type_port_profile(device_type)
                if self._materializes_static():
                    self._check_static_materialization_possible()
        else:
            _check_locked_fields_unchanged(
                NetworkDevice, self.pk, {"device_type": self.device_type_id}, update_fields=None
            )

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
        materialize DHCP and that's not a failure). For the rest: any VLAN
        shared by more than one port can't be addressed by
        ``suggest_slot_address()``'s one-address-per-(slot, VLAN) model
        (decision 5, Switched Mode devices), and each remaining port's
        suggested address must actually be usable
        (``_validate_static_address``).
        """
        addressable = [tp for tp in self.device_type.type_ports.select_related("vlan") if tp.vlan.subnet]
        by_vlan: dict[int, list[NetworkDeviceTypePort]] = {}
        for type_port in addressable:
            by_vlan.setdefault(type_port.vlan_id, []).append(type_port)
        for type_port_group in by_vlan.values():
            if len(type_port_group) > 1:
                names = ", ".join(tp.description for tp in type_port_group)
                raise ValidationError(
                    f"{self.device_type} has more than one port on {type_port_group[0].vlan} "
                    f"({names}) — static materialization needs one address per VLAN, and this "
                    "device has no way to give one address to all of them. Use DHCP for this device."
                )
        for type_port in addressable:
            address = _suggest_rack_slot_address(self.rack, self.rack_slot, type_port.vlan_id)
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

    def _materialize_ports(self) -> None:
        """One-time copy of ``device_type``'s Network Device Type Ports into
        real ``NetworkDevicePort`` rows — static by default when racked, or
        DHCP when unracked or explicitly chosen (ADR 0013, revising ADR
        0010's always-DHCP rule). Runs inside the same transaction as this
        device's insert (see ``save()``), so any failure here rolls back
        the device and every port materialized before it.
        """
        _validate_device_type_port_profile(self.device_type)
        static = self._materializes_static()
        if static:
            self._check_static_materialization_possible()
        for type_port in self.device_type.type_ports.select_related("vlan").order_by("ordinal"):
            if static and type_port.vlan.subnet:
                port = NetworkDevicePort(
                    device=self,
                    port_number=type_port.port_number,
                    description=type_port.description,
                    vlan=type_port.vlan,
                    port_type=type_port.port_type,
                    ordinal=type_port.ordinal,
                    source_type_port=type_port,
                    is_dhcp=False,
                    address=None,
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
    device is first created (``NetworkDevice._materialize_ports``) —
    static by default when racked, computed rack-range-base + rack-slot
    like every other static address here, or DHCP-configured
    (``is_dhcp=True``, ``address=None``) when unracked or explicitly chosen
    (ADR 0013, revising ADR 0010's always-DHCP rule); an operator can flip
    a port's addressing afterward either way. ``description`` (this
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

    def _persisted_switch_port_id(self) -> int | None:
        if self.pk is None:
            return None
        return (
            NetworkDevicePort._default_manager.filter(pk=self.pk)
            .values_list("switch_port_id", flat=True)
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
