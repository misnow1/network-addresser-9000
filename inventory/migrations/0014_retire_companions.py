# ADR 0022 PR 2 — retire ADR 0018's device companions. See
# docs/adr/0022-add-in-cards-and-operator-set-ports.md decision 8 and "What
# this does to production data", and PLAN-adr-0022.md's PR 2 migration
# section (settled decisions 8, 9, 10).
#
# The one genuinely dangerous step in this plan. It moves an address between
# rows, creates a type port, mutates port_count, and deletes device and type
# rows — in one transaction, in a fixed order, with a guard that refuses
# shapes it does not understand.
#
# Historical models throughout — apps.get_model(), never a module-level
# import: the live models (post-PR-2) no longer have host/companion_type at
# all, and the live NetworkDevice.save()/NetworkDeviceType.save() would
# reject the ownership/ordinal/port_count changes below as locked-field
# violations. Every query uses schema_editor.connection.alias, and
# address_source is written as the literal "operator" rather than importing
# the enum — importing inventory.models here would pull in the live,
# post-PR-2 model shape.
#
# Irreversible by design. Reversing would mean recreating exactly the
# companion device/type rows this migration deletes, with their original
# primary keys and audit trail — not something a schema reversal can do.
# This project's local databases are rebuilt from the source CSVs, not
# migrated backward (see this migration's own precedent, 0011's
# link_production_companions()); the production database for this ADR is
# rebuilt the same way (ADR 0022, "What this does to production data").

from typing import Any

from django.db import migrations, models

#: Case-insensitive hostname suffix identifying a companion's row (ADR
#: 0018/0022) — the same convention ``import_prod_data.py``'s
#: ``DEVICE_CONTROL_SUFFIX`` uses, independently declared here since this
#: migration imports no application module. Settled decision 9: a
#: companion whose name doesn't follow it is a shape this migration was
#: not written for, and it refuses rather than guesses.
DEVICE_CONTROL_SUFFIX = "-device-control"


def retire_companions(apps, schema_editor):
    """Fold every ADR 0018 companion pair into its host as a fourth,
    OPERATOR-sourced ``Device Control`` port (ADR 0022), then delete the
    now-portless companion devices and their types.

    Ordered steps, matching PLAN-adr-0022.md's PR 2 migration section
    exactly:

    1. Guard. Enumerate the ``NetworkDeviceType`` rows referenced by any
       ``companion_type`` (the companion types themselves) and check the
       *whole population* of each — not just instances that happen to have
       a host. An orphaned companion cannot exist (``host`` is
       ``OneToOneField(..., on_delete=CASCADE)``, ``0011_device_
       companions.py``); the corruption that *can* exist is an unlinked
       instance of a companion type, and that is what this checks for.
       Also checked, per instance: the companion's hostname follows the
       ``<host>-device-control`` convention (settled decision 9 — ADR 0018
       permitted other names, and this migration was not written to guess
       at them); and, over the *host* population separately (not reachable
       by iterating companions alone), that every host instance has
       exactly one companion.
    2. Create the ``Device Control`` type port on each affected host type,
       copying ``port_type``/``port_number`` from the companion's sole type
       port, and bump that type's ``port_count`` — once per *type*, not
       once per device instance, so a host type shared by several devices
       gets exactly one new type port.
    3. Guard the destination, then move each companion instance's one port
       row onto its host — never create-then-delete:
       ``unique_device_port_vlan_address_value`` forbids both rows existing
       at once, and moving preserves the port's audit history. Also checked
       here: the moved row's own ``port_type``/``port_number`` agree with
       the type port step 2 created, so a materialized port never ends up
       pointing at a ``source_type_port`` describing different hardware.
    4. Clear ``companion_type`` on every host type — the FK is ``PROTECT``,
       so deletion is refused while it is set.
    5. Delete the now-portless companion devices and their types.

    ``RemoveField`` for ``companion_type``/``host`` follows as separate
    operations after this one, once no row references either any more.
    """
    NetworkDeviceType = apps.get_model("inventory", "NetworkDeviceType")
    NetworkDeviceTypePort = apps.get_model("inventory", "NetworkDeviceTypePort")
    NetworkDevice = apps.get_model("inventory", "NetworkDevice")
    NetworkDevicePort = apps.get_model("inventory", "NetworkDevicePort")
    alias = schema_editor.connection.alias

    host_types = list(
        NetworkDeviceType.objects.using(alias)
        .filter(companion_type_id__isnull=False)
        .select_related("companion_type")
    )
    if not host_types:
        return  # nothing declares a companion — an empty, or already-clean, database

    # -- Step 1: guard --------------------------------------------------
    for host_type in host_types:
        companion_type = host_type.companion_type
        companion_type_ports = list(
            NetworkDeviceTypePort.objects.using(alias).filter(device_type_id=companion_type.pk)
        )
        if len(companion_type_ports) != 1:
            raise RuntimeError(
                f"Cannot retire companions: {companion_type} (companion_type of {host_type}) has "
                f"{len(companion_type_ports)} type ports, not exactly one — this migration only "
                "handles a single-port companion (ADR 0022)."
            )
        companion_type_port = companion_type_ports[0]
        host_has_matching_vlan = (
            NetworkDeviceTypePort.objects.using(alias)
            .filter(device_type_id=host_type.pk, vlan_id=companion_type_port.vlan_id)
            .exists()
        )
        if not host_has_matching_vlan:
            raise RuntimeError(
                f"Cannot retire companions: {host_type} has no type port on companion "
                f"{companion_type}'s VLAN (id={companion_type_port.vlan_id}) — nothing to anchor "
                "the new Device Control port's shared VLAN to."
            )
        instances = NetworkDevice.objects.using(alias).filter(device_type_id=companion_type.pk)
        for companion in instances:
            if companion.host_id is None:
                raise RuntimeError(
                    f"Cannot retire companions: {companion_type} instance pk={companion.pk} "
                    f"({companion.hostname!r}) has no host — an unlinked instance of a companion "
                    "type is the one corruption this migration refuses to guess at (ADR 0022)."
                )
            host = NetworkDevice.objects.using(alias).filter(pk=companion.host_id).first()
            if host is None or host.device_type_id != host_type.pk:
                raise RuntimeError(
                    f"Cannot retire companions: {companion_type} instance pk={companion.pk} "
                    f"({companion.hostname!r})'s host (pk={companion.host_id}) is not an instance "
                    f"of {host_type} — incompatible host type."
                )
            ports = list(NetworkDevicePort.objects.using(alias).filter(device_id=companion.pk))
            if len(ports) != 1:
                raise RuntimeError(
                    f"Cannot retire companions: {companion_type} instance pk={companion.pk} "
                    f"({companion.hostname!r}) has {len(ports)} ports, not exactly one."
                )
            port = ports[0]
            if port.is_dhcp or port.address is None:
                raise RuntimeError(
                    f"Cannot retire companions: {companion_type} instance pk={companion.pk} "
                    f"({companion.hostname!r})'s port is not statically addressed — this "
                    "migration only handles a statically-addressed companion (ADR 0022)."
                )
            stem = companion.hostname[: -len(DEVICE_CONTROL_SUFFIX)]
            if not companion.hostname.lower().endswith(DEVICE_CONTROL_SUFFIX) or (
                stem.lower() != host.hostname.lower()
            ):
                raise RuntimeError(
                    f"Cannot retire companions: {companion_type} instance pk={companion.pk} "
                    f"({companion.hostname!r}) does not follow the '<host>{DEVICE_CONTROL_SUFFIX}' "
                    f"hostname convention against its host {host.hostname!r} — settled decision 9 "
                    "refuses to guess at a companion shape ADR 0018 permitted but this migration "
                    "was not written for (a name other than the convention the importer used)."
                )

        # Every affected *host* instance, not just companions with a host —
        # a host instance the companion pass above never reaches (its
        # companion is simply missing) is invisible to a companion-only
        # loop (Codex review, first pass). Every host of a companion-
        # declaring type must have been given a companion at creation
        # (ADR 0018's own materialize-together guarantee); this is the
        # guard that actually checks that promise held, rather than
        # trusting it.
        host_instances = NetworkDevice.objects.using(alias).filter(device_type_id=host_type.pk)
        for host in host_instances:
            companion_count = NetworkDevice.objects.using(alias).filter(
                device_type_id=companion_type.pk, host_id=host.pk
            ).count()
            if companion_count != 1:
                raise RuntimeError(
                    f"Cannot retire companions: host {host_type} instance pk={host.pk} "
                    f"({host.hostname!r}) has {companion_count} companions, not exactly one — "
                    "every host of a companion-declaring type must have exactly one (ADR 0022)."
                )

    # -- Step 2: one Device Control type port per host type -------------
    new_type_port_by_host_type_id: dict[int, Any] = {}
    for host_type in host_types:
        companion_type = host_type.companion_type
        companion_type_port = NetworkDeviceTypePort.objects.using(alias).get(device_type_id=companion_type.pk)
        existing_max_ordinal = (
            NetworkDeviceTypePort.objects.using(alias)
            .filter(device_type_id=host_type.pk)
            .aggregate(models.Max("ordinal"))["ordinal__max"]
            or 0
        )
        new_type_port = NetworkDeviceTypePort.objects.using(alias).create(
            device_type_id=host_type.pk,
            port_number=companion_type_port.port_number,
            description="Device Control",
            port_type=companion_type_port.port_type,
            vlan_id=companion_type_port.vlan_id,
            slot_offset=0,
            address_source="operator",
            hostname_suffix="device-control",
            ordinal=existing_max_ordinal + 1,
            created_by_id=companion_type_port.created_by_id,
        )
        new_type_port_by_host_type_id[host_type.pk] = new_type_port
        NetworkDeviceType.objects.using(alias).filter(pk=host_type.pk).update(
            port_count=models.F("port_count") + 1
        )

    # -- Step 3: guard the destination, then move each instance port ----
    for host_type in host_types:
        companion_type = host_type.companion_type
        new_type_port = new_type_port_by_host_type_id[host_type.pk]
        instances = NetworkDevice.objects.using(alias).filter(device_type_id=companion_type.pk)
        for companion in instances:
            host_id = companion.host_id
            if (
                NetworkDevicePort.objects.using(alias)
                .filter(device_id=host_id, description="Device Control")
                .exists()
            ):
                raise RuntimeError(
                    f"Cannot retire companions: host pk={host_id} already has a 'Device Control' "
                    "port — refusing to move a second one onto it."
                )
            if (
                NetworkDevicePort.objects.using(alias)
                .filter(device_id=host_id, ordinal=new_type_port.ordinal)
                .exists()
            ):
                raise RuntimeError(
                    f"Cannot retire companions: host pk={host_id} already has a port at ordinal "
                    f"{new_type_port.ordinal} — refusing to move the companion's port onto it."
                )
            port = NetworkDevicePort.objects.using(alias).get(device_id=companion.pk)
            if port.port_type != new_type_port.port_type or port.port_number != new_type_port.port_number:
                raise RuntimeError(
                    f"Cannot retire companions: {companion_type} instance pk={companion.pk}'s port "
                    f"(port_type={port.port_type!r}, port_number={port.port_number!r}) does not "
                    f"match the type port just created for {host_type} "
                    f"(port_type={new_type_port.port_type!r}, port_number={new_type_port.port_number!r}) "
                    "— the instance port has drifted from its own type port; refusing to move it "
                    "onto a source_type_port describing different hardware."
                )
            NetworkDevicePort.objects.using(alias).filter(pk=port.pk).update(
                device_id=host_id,
                description="Device Control",
                ordinal=new_type_port.ordinal,
                source_type_port_id=new_type_port.pk,
            )

    # -- Step 4: clear companion_type before the PROTECT FK blocks deletion --
    NetworkDeviceType.objects.using(alias).filter(pk__in=[ht.pk for ht in host_types]).update(
        companion_type=None
    )

    # -- Step 5: delete the now-portless companion devices, then their types --
    companion_type_ids = {ht.companion_type_id for ht in host_types}
    NetworkDevice.objects.using(alias).filter(device_type_id__in=companion_type_ids).delete()
    NetworkDeviceType.objects.using(alias).filter(pk__in=companion_type_ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0013_operator_set_ports"),
    ]

    operations = [
        migrations.RunPython(retire_companions, reverse_code=migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="networkdevicetype",
            name="companion_type",
        ),
        migrations.RemoveField(
            model_name="networkdevice",
            name="host",
        ),
    ]
