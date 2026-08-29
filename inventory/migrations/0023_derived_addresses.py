"""ADR 0027 PR 1, decision 5 — retires ``NetworkDeviceTypePort.
address_source`` and ``PortAddressSource`` (ADR 0022), the mechanism for a
Yamaha console's "Device Control" interface, by rewriting it to an
ordinary ``slot_offset=1`` port before dropping the field.

Order, and why each step is where it is:

1. **Assert the pre-conditions the rewrite in step 2 relies on, against
   data nothing has touched yet.** Every ``address_source="operator"``
   type port's statically-addressed instances must currently imply the
   same single, non-negative offset (``address - range_base -
   rack_slot``) — step 2 is about to declare one ``slot_offset`` for the
   whole type port, so this is the thing that has to already be true for
   that to be correct. It also checks that writing that offset onto every
   affected device doesn't newly collide an ordinal with another
   switch's or device's already-claimed one in the same rack (ADR 0027
   decision 5's "no divergence report" — a collision the rewrite itself
   would create is exactly what this project refuses to write, not
   something to tolerate and report).

   This is the **load-bearing** gate. It has to run first: the
   corresponding check after the rewrite (step 4 below) can only ever
   compare a row against the value the rewrite itself just wrote from the
   same formula, so it can't fail on anything the rewrite touched by
   construction — it only ever covers the untouched rows. Refusing here,
   before step 2 runs, means nothing has been written yet.

2. Rewrite every ``address_source="operator"`` type port (in the live
   estate, exactly the DM3 and DM7C "Device Control" ports — measured at
   2/132, see ``docs/plans/measure-adr-0027.py``) to ``slot_offset=1``,
   and copy that onto every ``NetworkDevicePort`` materialized from one of
   them. Every remaining ``OPERATOR``-sourced port in the estate implies
   exactly this offset after ADR 0027's plan "Step 0" (``mps-dm7c-1``
   hand-moved from slot 5 to 4 so both consoles' Device Control
   interfaces now sit one address above their own console) — this
   migration writes that as the type's *declared* shape rather than an
   operator-supplied one.

   This bypasses ADR 0010's "a profile's type ports lock once the profile
   has any instance" rule (``NetworkDeviceTypePort.save()`` in
   ``models.py``) — deliberately, and structurally, not via a flag: a
   historical model built by ``apps.get_model()`` carries only fields and
   relations, none of this app's custom ``save()``/``clean()`` overrides,
   so there is no lock to bypass in the first place. A reader who knows
   ADR 0010 should not read this as a bug — it is the only way a
   type-port rewrite like this one can happen at all (ADR 0027's "Known
   gaps" calls this out by name as "the fiddliest part of the
   implementation").

3. Re-derive the address of every affected, statically-addressed
   ``NetworkDevicePort``: ``range_base + rack_slot + 1``, the same formula
   ``inventory.models._suggest_rack_slot_address`` computes at the
   application layer, reimplemented here against historical models via
   ``inventory.suggestions.suggest_slot_address`` — pure arithmetic with
   no model dependency, safe to import into a migration (unlike
   ``inventory.models`` itself, which migrations must never import: it
   reflects the *current* code, not the schema this migration runs
   against).

4. Assert every static, racked ``NetworkDevicePort`` in the whole
   database now satisfies ``address == range_base + rack_slot +
   slot_offset`` — the same check ``docs/plans/measure-adr-0027.py``
   reports, reimplemented here as a second gate. Raises ``RuntimeError``
   and aborts the migration if any port diverges, **before** the column
   is dropped. On its own this is *not* the load-bearing check — see step
   1 above for why — but it stays because it's cheap and it still catches
   a row the rewrite never touched (pre-existing drift unrelated to
   ``address_source`` at all). On an empty/fresh database (every test
   run's own ``migrate``) both gates hold vacuously — nothing to check,
   nothing to fail.

5. Drop ``address_source`` (and, with it, every reference to
   ``PortAddressSource`` — a plain ``TextChoices``, not a DB object, so
   there is nothing else to drop for it).

**Irreversible, deliberately.** Reversing step 4 (Django's default
``RemoveField`` reverse re-adds the column, with ``PortAddressSource.
SLOT`` as the default for every existing row) recreates a column, not the
lost *meaning* — steps 1-2's rewrite is not undone, so a reversed database
has ``address_source`` back but no port actually reading ``"operator"``
any more. This is accepted: ADR 0027 supersedes the mechanism this field
existed for, and losslessly reversing a migration that also re-addresses
two rows around the field it drops was never the goal.
"""

import ipaddress

from django.db import migrations

from inventory.suggestions import suggest_slot_address


def _assert_preconditions(apps, schema_editor):
    """The load-bearing gate (see the module docstring's step 1) — runs
    *before* anything is written, against data nothing has touched yet.

    Checks the two things ``_rewrite_operator_ports_and_readdress``'s
    single-formula rewrite actually relies on:

    1. Every ``address_source="operator"`` type port's statically
       addressed instances currently imply the same single, non-negative
       offset (``address - range_base - rack_slot``). The rewrite is
       about to declare one ``slot_offset`` for the whole type port; if
       two instances disagree today, no single value is correct for both.

    2. Writing that offset onto every device carrying one of these type
       ports doesn't newly collide an ordinal with another switch's or
       device's already-claimed one in the same rack — the same
       occupied-ordinal notion ``inventory.models.
       occupied_rack_slot_ordinals()`` computes at the application layer,
       reimplemented here against historical models. ADR 0027 decision 5
       refuses a write that creates a violation outright; a migration
       that *creates* one is exactly that, not inherited reality to
       report and tolerate.

    Deliberately covers every instance of an affected type port, DHCP
    included — occupancy doesn't care whether a port is statically
    addressed, only whether the type declares the offset.
    """
    NetworkDeviceTypePort = apps.get_model("inventory", "NetworkDeviceTypePort")
    NetworkDevicePort = apps.get_model("inventory", "NetworkDevicePort")
    NetworkDevice = apps.get_model("inventory", "NetworkDevice")
    NetworkSwitch = apps.get_model("inventory", "NetworkSwitch")
    RackVlanRange = apps.get_model("inventory", "RackVlanRange")

    operator_type_port_ids = list(
        NetworkDeviceTypePort.objects.filter(address_source="operator").values_list("pk", flat=True)
    )
    if not operator_type_port_ids:
        return  # fresh/test database — nothing to rewrite, nothing to check

    ranges_by_rack_vlan = {
        (rack_vlan_range.rack_id, rack_vlan_range.vlan_id): ipaddress.IPv4Network(
            rack_vlan_range.address_range, strict=True
        ).network_address
        for rack_vlan_range in RackVlanRange.objects.all()
    }

    # Invariant 1: every statically-addressed instance implies the same
    # single, non-negative offset.
    implied_offsets: set[int] = set()
    static_ports = NetworkDevicePort.objects.filter(
        source_type_port_id__in=operator_type_port_ids, is_dhcp=False
    ).select_related("device")
    for port in static_ports:
        device = port.device
        if device.rack_id is None or device.rack_slot is None:
            raise RuntimeError(
                f"ADR 0027 migration 0023 precondition: NetworkDevicePort {port.pk} "
                f"({port.description!r}) is statically addressed from an operator-sourced "
                "type port but its device is not racked."
            )
        base = ranges_by_rack_vlan.get((device.rack_id, port.vlan_id))
        if base is None:
            raise RuntimeError(
                f"ADR 0027 migration 0023 precondition: no Rack VLAN Range for rack "
                f"{device.rack_id} / vlan {port.vlan_id} — cannot verify NetworkDevicePort "
                f"{port.pk}'s implied offset."
            )
        implied_offsets.add(int(ipaddress.IPv4Address(port.address)) - int(base) - device.rack_slot)

    if not implied_offsets:
        return  # no statically-addressed operator port -- nothing implies an offset to check
    if len(implied_offsets) > 1 or min(implied_offsets) < 0:
        raise RuntimeError(
            "ADR 0027 migration 0023 precondition: operator-sourced type ports' instances "
            f"imply inconsistent or negative offsets {sorted(implied_offsets)} — refusing to "
            "rewrite to a single slot_offset. Reconcile the estate by hand before migrating "
            "(ADR 0027 plan Step 0's shape)."
        )
    new_offset = next(iter(implied_offsets))

    # Invariant 2: writing new_offset onto every device carrying one of
    # these type ports doesn't collide with another occupant's ordinal in
    # the same rack.
    affected_type_ids = list(
        NetworkDeviceTypePort.objects.filter(pk__in=operator_type_port_ids).values_list(
            "device_type_id", flat=True
        )
    )
    affected_devices = NetworkDevice.objects.filter(
        device_type_id__in=affected_type_ids, rack__isnull=False, rack_slot__isnull=False
    )
    for device in affected_devices:
        new_ordinal = device.rack_slot + new_offset
        other_ordinals: set[int] = set(
            NetworkSwitch.objects.filter(rack_id=device.rack_id, rack_slot__isnull=False).values_list(
                "rack_slot", flat=True
            )
        )
        for other_id, other_slot, other_offset in (
            NetworkDevice.objects.filter(rack_id=device.rack_id, rack_slot__isnull=False)
            .exclude(pk=device.pk)
            .values_list("id", "rack_slot", "device_type__type_ports__slot_offset")
        ):
            other_ordinals.add(other_slot)
            other_ordinals.add(other_slot + (other_offset or 0))
        if new_ordinal in other_ordinals:
            raise RuntimeError(
                f"ADR 0027 migration 0023 precondition: rewriting NetworkDevice {device.pk} "
                f"({device.hostname!r})'s operator-sourced type port to slot_offset={new_offset} "
                f"would claim ordinal {new_ordinal} in rack {device.rack_id}, already claimed by "
                "another device or switch."
            )


def _rewrite_operator_ports_and_readdress(apps, schema_editor):
    NetworkDeviceTypePort = apps.get_model("inventory", "NetworkDeviceTypePort")
    NetworkDevicePort = apps.get_model("inventory", "NetworkDevicePort")
    RackVlanRange = apps.get_model("inventory", "RackVlanRange")

    operator_type_port_ids = list(
        NetworkDeviceTypePort.objects.filter(address_source="operator").values_list("pk", flat=True)
    )
    if not operator_type_port_ids:
        return  # fresh/test database — nothing to rewrite, nothing to fail

    NetworkDeviceTypePort.objects.filter(pk__in=operator_type_port_ids).update(slot_offset=1)
    # Every instance port materialized from one of these type ports gets
    # the same offset, whether or not it currently holds a static address
    # (an unracked/DHCP device's port has none to re-derive, but its own
    # slot_offset column must still agree with its type's).
    NetworkDevicePort.objects.filter(source_type_port_id__in=operator_type_port_ids).update(slot_offset=1)

    affected_ports = NetworkDevicePort.objects.filter(
        source_type_port_id__in=operator_type_port_ids, is_dhcp=False
    ).select_related("device")
    for port in affected_ports:
        device = port.device
        if device.rack_id is None or device.rack_slot is None:
            raise RuntimeError(
                f"NetworkDevicePort {port.pk} ({port.description!r}) is statically addressed "
                "from an operator-sourced type port but its device is not racked — cannot "
                "re-derive its address (ADR 0027 migration 0023)."
            )
        try:
            rack_range = RackVlanRange.objects.get(rack_id=device.rack_id, vlan_id=port.vlan_id)
        except RackVlanRange.DoesNotExist:
            raise RuntimeError(
                f"No Rack VLAN Range for rack {device.rack_id} / vlan {port.vlan_id} — cannot "
                f"re-derive NetworkDevicePort {port.pk}'s address (ADR 0027 migration 0023)."
            ) from None
        new_address = suggest_slot_address(rack_range.address_range, device.rack_slot + 1)
        NetworkDevicePort.objects.filter(pk=port.pk).update(address=new_address)


def _assert_full_conformance(apps, schema_editor):
    """Runs *after* the rewrite — see the module docstring's step 4 for
    why this is a second, cheap check rather than the load-bearing one
    (``_assert_preconditions`` above is that): every row the rewrite just
    touched satisfies this by construction, since the rewrite wrote
    ``address`` from the exact same formula this compares against. This
    still earns its place by covering pre-existing drift on a row the
    rewrite never touches at all.
    """
    NetworkDevicePort = apps.get_model("inventory", "NetworkDevicePort")
    RackVlanRange = apps.get_model("inventory", "RackVlanRange")

    ranges_by_rack_vlan = {
        (rack_vlan_range.rack_id, rack_vlan_range.vlan_id): ipaddress.IPv4Network(
            rack_vlan_range.address_range, strict=True
        ).network_address
        for rack_vlan_range in RackVlanRange.objects.all()
    }
    divergent = []
    ports = NetworkDevicePort.objects.filter(is_dhcp=False, device__rack__isnull=False).select_related(
        "device"
    )
    for port in ports:
        if port.address is None:
            continue  # DB CheckConstraint already refuses this; defensive only
        device = port.device
        base = ranges_by_rack_vlan.get((device.rack_id, port.vlan_id))
        if base is None:
            continue  # no range for this (rack, vlan) — out of ADR 0027's scope, same as
            # measure-adr-0027.py's "unscoped" bucket, not a divergence to report here
        implied = int(ipaddress.IPv4Address(port.address)) - int(base) - device.rack_slot
        if implied != port.slot_offset:
            divergent.append((port.pk, port.description, implied, port.slot_offset))
    if divergent:
        details = "; ".join(
            f"port {pk} ({description!r}): implied offset {implied} != declared {declared}"
            for pk, description, implied, declared in divergent
        )
        raise RuntimeError(
            f"ADR 0027 migration 0023: {len(divergent)} static racked port(s) do not satisfy "
            "address == range_base + rack_slot + slot_offset — refusing to drop "
            f"address_source while the estate disagrees with its own ordinals: {details}"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0022_hostname_slug_to_device_model"),
    ]

    operations = [
        migrations.RunPython(_assert_preconditions, reverse_code=migrations.RunPython.noop),
        migrations.RunPython(_rewrite_operator_ports_and_readdress, reverse_code=migrations.RunPython.noop),
        migrations.RunPython(_assert_full_conformance, reverse_code=migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="networkdevicetypeport",
            name="address_source",
        ),
    ]
