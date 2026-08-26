"""ADR 0027 PR 1, decision 5 — retires ``NetworkDeviceTypePort.
address_source`` and ``PortAddressSource`` (ADR 0022), the mechanism for a
Yamaha console's "Device Control" interface, by rewriting it to an
ordinary ``slot_offset=1`` port before dropping the field.

Order, and why each step is where it is:

1. Rewrite every ``address_source="operator"`` type port (in the live
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

2. Re-derive the address of every affected, statically-addressed
   ``NetworkDevicePort``: ``range_base + rack_slot + 1``, the same formula
   ``inventory.models._suggest_rack_slot_address`` computes at the
   application layer, reimplemented here against historical models via
   ``inventory.suggestions.suggest_slot_address`` — pure arithmetic with
   no model dependency, safe to import into a migration (unlike
   ``inventory.models`` itself, which migrations must never import: it
   reflects the *current* code, not the schema this migration runs
   against).

3. Assert every static, racked ``NetworkDevicePort`` in the whole
   database now satisfies ``address == range_base + rack_slot +
   slot_offset`` — the same check ``docs/plans/measure-adr-0027.py``
   reports, reimplemented here as a hard gate. Raises ``RuntimeError`` and
   aborts the migration if any port still diverges, **before** the column
   is dropped: the drop is irreversible, and this assertion is the only
   thing standing between a drifted estate and silent data loss (plan
   Risks section). On an empty/fresh database (every test run's own
   ``migrate``) this holds vacuously — nothing to check, nothing to fail.

4. Drop ``address_source`` (and, with it, every reference to
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
        migrations.RunPython(_rewrite_operator_ports_and_readdress, reverse_code=migrations.RunPython.noop),
        migrations.RunPython(_assert_full_conformance, reverse_code=migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="networkdevicetypeport",
            name="address_source",
        ),
    ]
