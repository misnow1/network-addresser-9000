"""One-off importer for the production addressing spreadsheet export.

Implements ``PLAN-prod-import.md``'s ``## Sequence`` (stages 1-9, in order)
inside one transaction: audit identity, VLANs, switch port VLAN profiles,
a rack template, racks (in ascending ``Address Offset`` order — first-fit
creation order is load-bearing, see the plan's "The constraint that shapes
everything"), switch types, device types, switches, then devices.

Every row this command writes goes through construct -> ``full_clean()`` ->
``save()`` and never ``bulk_create()`` or a bare ``objects.create()`` that
would skip ``clean()`` — see the plan's "The rule every stage below obeys".
Switch/device addresses are never computed here; creating a racked
``NetworkSwitch``/``NetworkDevice`` triggers its own materialization
(``NetworkSwitch._materialize_ports``/``_materialize_addresses``,
``NetworkDevice._materialize_ports``) exactly as it would from the admin,
which is the point — an importer that reimplemented the address arithmetic
would prove nothing about the app.

**The app must be quiesced for this run.** No other writer, admin session
included: this command reads "is any Rack occupying a candidate block"
without locking (the same unlocked-read caveat
``RackSlotAssignmentMixin``/``suggest_rack_vlan_range()`` already document),
and creation order determines every rack's base address, so a half-applied
import cannot be safely retried against a partially-populated database. See
this command's own ``help`` text and ``PLAN-prod-import.md``'s "The app
must be quiesced for the import."
"""

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from inventory.models import (
    VLAN,
    NetworkDevice,
    NetworkDeviceType,
    NetworkDeviceTypePort,
    NetworkSwitch,
    NetworkSwitchType,
    NetworkSwitchTypePort,
    PortAddressSource,
    PortMode,
    PortType,
    Rack,
    RackTemplate,
    RackTemplateVlan,
    RackVlanRange,
    SwitchPortVlanProfile,
    SwitchPortVlanProfileAllowedVlan,
)

from ._prod_import_csv import (
    AddressingRow,
    SwitchPortRow,
    SwitchPortTable,
    parse_addressing_rows,
    parse_rack_offsets,
    parse_switch_port_tables,
    parse_vlan_table,
    read_csv_rows,
)

IMPORT_USERNAME = "prod-import"

ADDRESSING_CSV_NAME = "MPS Audio Network Standards - IP Addressing mk2.csv"
CALC_LOOKUPS_CSV_NAME = "MPS Audio Network Standards - IP Calc Lookups.csv"
SWITCH_PORTS_CSV_NAME = "MPS Audio Network Standards - Switch Ports.csv"

#: VLAN table "Function" names this site's scheme keys off of. Everything
#: else (Lighting Control/Protocol, Video, NDI) is created as an ordinary
#: VLAN row (stage 2) but never referenced again — audio is the pilot, not
#: the scope (PROD-DATA-ANALYSIS.md §7.1).
FN_CONTROL = "Audio Control"
FN_DANTE_PRIMARY = "Dante Primary"
FN_DANTE_SECONDARY = "Dante Secondary"

#: VLAN function name -> the ``AddressingRow`` attribute holding that
#: function's address, for reading an ``OPERATOR``-sourced type port's
#: address off whichever CSV column corresponds to its VLAN (ADR 0022).
_ADDRESS_BY_FUNCTION: dict[str, Callable[[AddressingRow], str]] = {
    FN_CONTROL: lambda row: row.control,
    FN_DANTE_PRIMARY: lambda row: row.dante_primary,
    FN_DANTE_SECONDARY: lambda row: row.dante_secondary,
}

#: Racks whose bases sit behind deliberate gaps that first-fit will never
#: leave — entered with explicit ranges, not the "Audio Rack" template
#: (PLAN-prod-import.md "The constraint that shapes everything").
MANUAL_RANGE_RACKS = frozenset({"SHURE", "CONSOLES"})

#: By convention, the drive-rack primary switches serve DHCP
#: (PROD-DATA-ANALYSIS.md §7.7).
DHCP_SERVER_RACKS = frozenset({"FOH Drive #1", "FOH Drive #2"})

#: The Netgear switch's addressing-sheet description — deferred, not
#: created, since "Managed Switch" is not a model (PLAN-prod-import.md §6).
NETGEAR_DESCRIPTION = "Netgear Managed Switch (For W8LM Rack)"

#: Pinned, not derived. PLAN-prod-import.md §9 is explicit that the
#: DMI-DANTE card's rack and ordinal must be predictable rather than
#: computed, since the two resulting addresses get programmed into the
#: physical card. A `max(occupied slot) + 1` derivation would silently
#: follow a later or malformed row above slot 16 instead of catching it —
#: see ``_classify_addressing_rows()``'s collision guard below.
DMI_DANTE_RACK = "CONSOLES"
DMI_DANTE_SLOT = 17

#: Every switch-ports-CSV table name actually used to build a
#: ``NetworkSwitchType`` — excludes the Netgear and Unmanaged tables, which
#: name no real type (§6).
PRIMARY_SWITCH_TABLES: dict[str, tuple[str, str, str]] = {
    # CSV table name -> (manufacturer, model, type profile name)
    "Cisco SG300-10MP (For 3xAmp Rack Primary)": ("Cisco", "SG300-10MP", "For 3xAmp Rack Primary"),
    "Cisco SG350-10 (For 2xAmp Rack Primary)": ("Cisco", "SG350-10", "For 2xAmp Rack Primary"),
    "Cisco SG300-10MP (For Drive Rack Primary)": ("Cisco", "SG300-10MP", "For Drive Rack Primary"),
    "TP-Link TL-SG108E": ("TP-Link", "TL-SG108E", "Default"),
    "Cisco SG300-26P": ("Cisco", "SG300-26P", "Default"),
}

#: Tables that also get a derived Secondary profile (§3/§6) — the drive
#: rack switch and the two single-profile switches have no secondary.
SECONDARY_DERIVED_TABLES: dict[str, tuple[str, str, str]] = {
    "Cisco SG300-10MP (For 3xAmp Rack Primary)": ("Cisco", "SG300-10MP", "For 3xAmp Rack Secondary"),
    "Cisco SG350-10 (For 2xAmp Rack Primary)": ("Cisco", "SG350-10", "For 2xAmp Rack Secondary"),
}

#: Addressing-sheet Device Description -> the switch type's *table name*
#: (a key into ``PRIMARY_SWITCH_TABLES``/``SECONDARY_DERIVED_TABLES``).
SWITCH_DESCRIPTION_TO_TABLE: dict[str, str] = {
    "Cisco SG300-10MP (For 3xAmp Rack Primary)": "Cisco SG300-10MP (For 3xAmp Rack Primary)",
    "Cisco SG300-10MP (For 3xAmp Rack Secondary)": "Cisco SG300-10MP (For 3xAmp Rack Primary)",
    "Cisco SG350-10 (For 2xAmp Rack Primary)": "Cisco SG350-10 (For 2xAmp Rack Primary)",
    "Cisco SG350-10 (For 2xAmp Rack Secondary)": "Cisco SG350-10 (For 2xAmp Rack Primary)",
    "Cisco SG300-10MP (For Drive Rack Primary)": "Cisco SG300-10MP (For Drive Rack Primary)",
    "Spare SG300-26P": "Cisco SG300-26P",
}
_TLSG108E_RE = re.compile(r"^mps-tlsg108e-\d+$")


@dataclass(frozen=True)
class DeviceTypeSpec:
    """One ``NetworkDeviceType`` to create: identity plus its ports.

    Each port is ``(description, vlan_function, slot_offset, address_source,
    hostname_suffix)`` (ADR 0022) — the VLAN is resolved by function name
    (``FN_CONTROL`` etc.), not a hardcoded VLAN id, so this catalog is
    independent of which numeric VLAN ids a given Calc Lookups export
    happens to use. Every device port in this dataset is a plain 1GbE
    copper jack (PROD-DATA-ANALYSIS.md §7.3's AVIO devices state it
    explicitly; nothing elsewhere contradicts it), so ``port_type`` isn't
    part of the spec — the importer applies it uniformly when
    materializing type ports below.
    """

    key: str
    manufacturer: str
    model: str
    name: str
    ports: tuple[tuple[str, str, int, str, str], ...]


#: Default ``(address_source, hostname_suffix)`` for an ordinary
#: SLOT-addressed port with no derived hostname of its own — most ports in
#: this catalog. Spread with ``*_SLOT`` into a port tuple.
_SLOT = (PortAddressSource.SLOT, "")

#: Tier 1 (six unambiguous types) and tier 3 (AVIO), per PLAN-prod-import.md
#: §7. The Yamaha consoles' Device Control interface (ADR 0022, closing
#: #42) is a fourth, ``OPERATOR``-sourced port on the console's own type
#: rather than a separate device type.
DEVICE_TYPES: tuple[DeviceTypeSpec, ...] = (
    DeviceTypeSpec(
        "ik42_with_card",
        "Martin Audio",
        "IK-42",
        "with Dante Card",
        (
            ("Control", FN_CONTROL, 0, *_SLOT),
            ("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT),
            ("Dante Secondary", FN_DANTE_SECONDARY, 0, *_SLOT),
        ),
    ),
    DeviceTypeSpec(
        "ik42_without_card",
        "Martin Audio",
        "IK-42",
        "without Dante Card",
        (("Control", FN_CONTROL, 0, *_SLOT),),
    ),
    DeviceTypeSpec(
        "ik81_with_card",
        "Martin Audio",
        "IK-81",
        "with Dante Card",
        (
            ("Control", FN_CONTROL, 0, *_SLOT),
            ("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT),
            ("Dante Secondary", FN_DANTE_SECONDARY, 0, *_SLOT),
        ),
    ),
    DeviceTypeSpec(
        "lm26",
        "Lab.Gruppen",
        "LM26",
        "Redundant Mode",
        (("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT), ("Dante Secondary", FN_DANTE_SECONDARY, 0, *_SLOT)),
    ),
    DeviceTypeSpec(
        "lm44",
        "Lab.Gruppen",
        "LM44",
        "Redundant Mode",
        (("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT), ("Dante Secondary", FN_DANTE_SECONDARY, 0, *_SLOT)),
    ),
    DeviceTypeSpec(
        "plm20000q",
        "Lab.Gruppen",
        "PLM20000Q",
        "Redundant Mode",
        (("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT), ("Dante Secondary", FN_DANTE_SECONDARY, 0, *_SLOT)),
    ),
    DeviceTypeSpec(
        "sd12",
        "DiGiCo",
        "SD12",
        "Default",
        (
            ("Control", FN_CONTROL, 0, *_SLOT),
            ("Engine", FN_CONTROL, 1, PortAddressSource.SLOT, "engine"),
        ),
    ),
    DeviceTypeSpec("sd9", "DiGiCo", "SD9", "Default", (("Control", FN_CONTROL, 0, *_SLOT),)),
    DeviceTypeSpec("sd11", "DiGiCo", "SD11", "Default", (("Control", FN_CONTROL, 0, *_SLOT),)),
    DeviceTypeSpec(
        "dmi_dante",
        "DiGiCo",
        "DMI-DANTE",
        "Default",
        (("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT), ("Dante Secondary", FN_DANTE_SECONDARY, 0, *_SLOT)),
    ),
    DeviceTypeSpec(
        "dm7c",
        "Yamaha",
        "DM7C",
        "Default",
        (
            ("Control", FN_CONTROL, 0, *_SLOT),
            ("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT),
            ("Dante Secondary", FN_DANTE_SECONDARY, 0, *_SLOT),
            ("Device Control", FN_DANTE_PRIMARY, 0, PortAddressSource.OPERATOR, "device-control"),
        ),
    ),
    DeviceTypeSpec("dm7ex", "Yamaha", "DM7-EX", "Default", (("Control", FN_CONTROL, 0, *_SLOT),)),
    DeviceTypeSpec(
        "dm3",
        "Yamaha",
        "DM3",
        "Default",
        (
            ("Control", FN_CONTROL, 0, *_SLOT),
            ("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT),
            ("Dante Secondary", FN_DANTE_SECONDARY, 0, *_SLOT),
            ("Device Control", FN_DANTE_PRIMARY, 0, PortAddressSource.OPERATOR, "device-control"),
        ),
    ),
    DeviceTypeSpec(
        "tio1608d2",
        "Yamaha",
        "Tio1608-D2",
        "Default",
        (
            ("Control", FN_CONTROL, 0, *_SLOT),
            ("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT),
            ("Dante Secondary", FN_DANTE_SECONDARY, 0, *_SLOT),
        ),
    ),
    DeviceTypeSpec(
        "sq5",
        "Allen & Heath",
        "SQ-5",
        "Default",
        (
            ("Control", FN_CONTROL, 0, *_SLOT),
            ("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT),
            ("Dante Secondary", FN_DANTE_SECONDARY, 0, *_SLOT),
        ),
    ),
    DeviceTypeSpec(
        "avio_amph_rjd1212",
        "Amphenol",
        "RJD1212-0050",
        "Default",
        (("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT),),
    ),
    DeviceTypeSpec(
        "avio_amph_rjd2203",
        "Amphenol",
        "RJD2203-0050",
        "Default",
        (("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT),),
    ),
    DeviceTypeSpec(
        "avio_amph_rjd32a3",
        "Amphenol",
        "RJD32A3-0050",
        "Default",
        (("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT),),
    ),
    DeviceTypeSpec(
        "avio_amph_rjd32u1",
        "Amphenol",
        "RJD32U1-0050",
        "Default",
        (("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT),),
    ),
    DeviceTypeSpec(
        "avio_audinate_ao2",
        "Audinate",
        "AVIO-AO2",
        "Default",
        (("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT),),
    ),
    DeviceTypeSpec(
        "avio_neutrik_na2dline",
        "Neutrik",
        "NA2-DLINE",
        "Default",
        (("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT),),
    ),
    DeviceTypeSpec(
        "avio_radial_tx",
        "Radial",
        "DiNET DAN-TX",
        "Default",
        (("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT),),
    ),
    DeviceTypeSpec(
        "avio_radial_rx",
        "Radial",
        "DiNET DAN-RX",
        "Default",
        (("Dante Primary", FN_DANTE_PRIMARY, 0, *_SLOT),),
    ),
)


def _build_spec_by_key(specs: tuple[DeviceTypeSpec, ...]) -> dict[str, DeviceTypeSpec]:
    """``DEVICE_TYPES`` is a tuple, not a mapping — indexing it by key
    (``DEVICE_TYPES[some_key]``) raises ``TypeError``, not ``KeyError``.
    Builds the real lookup once, raising loudly on a duplicate key rather
    than letting a later entry silently shadow an earlier one.
    """
    by_key: dict[str, DeviceTypeSpec] = {}
    for spec in specs:
        if spec.key in by_key:
            raise ValueError(f"Duplicate DeviceTypeSpec key: {spec.key!r}")
        by_key[spec.key] = spec
    return by_key


#: The real lookup for ``DEVICE_TYPES`` — see ``_build_spec_by_key()``.
SPEC_BY_KEY: dict[str, DeviceTypeSpec] = _build_spec_by_key(DEVICE_TYPES)

#: Case-insensitive hostname suffix identifying a Yamaha console's Device
#: Control row in the addressing sheet (ADR 0022) — the same convention
#: the pre-ADR-0022 importer used, stated once here since this importer is
#: the forward-going path.
DEVICE_CONTROL_SUFFIX = "-device-control"

#: Addressing-sheet Device Description -> device type key, for the rows
#: whose description is a plain model/hostname string (tiers 1 and 2 minus
#: the SD12 pairs, which are handled by the collapse logic below, and the
#: Device Control rows, which are handled by ``_classify_device_control_pairs()``
#: below).
DESCRIPTION_TO_DEVICE_KEY: dict[str, str] = {
    "IK42": "ik42_with_card",
    "IK81": "ik81_with_card",
    "LM26": "lm26",
    "LM44": "lm44",
    "PLM20000Q": "plm20000q",
    "SD9": "sd9",
    "SD11": "sd11",
    "DM7C-1": "dm7c",
    "DM7-EX-1": "dm7ex",
    "bej-dm3-1": "dm3",
    "bej-tio1608-d2-1": "tio1608d2",
    "SQ5-1": "sq5",
}

#: ``(rack, slot)`` pairs where an IK-42 has no Dante card fitted
#: (PROD-DATA-ANALYSIS.md §5.2) — every other "IK42" row gets the
#: with-Dante-Card type.
IK42_WITHOUT_DANTE_CARD: frozenset[tuple[str, int]] = frozenset({("XE300-1", 3), ("XE300-1", 4)})

#: AVIO hostname prefix -> device type key (PLAN-prod-import.md §7 tier 3).
#: Order matters: more specific patterns (``-aes-io-``, ``-usb-io-``) must
#: be tried before the shorter ``-input-``/``-output-`` ones they'd
#: otherwise be mistaken for.
AVIO_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^mps-avio-amph-output-\d+$"), "avio_amph_rjd1212"),
    (re.compile(r"^mps-avio-avio-aes-io-\d+$"), "avio_amph_rjd32a3"),
    (re.compile(r"^mps-avio-avio-usb-io-\d+$"), "avio_amph_rjd32u1"),
    (re.compile(r"^mps-avio-avio-input-\d+$"), "avio_amph_rjd2203"),
    (re.compile(r"^mps-avio-avio-output-\d+$"), "avio_audinate_ao2"),
    (re.compile(r"^mps-avio-na2-dline-\d+$"), "avio_neutrik_na2dline"),
    (re.compile(r"^mps-avio-radial-tx$"), "avio_radial_tx"),
    (re.compile(r"^mps-avio-radial-rx-\d+$"), "avio_radial_rx"),
)

#: The five profile names PLAN-prod-import.md §3 settles on.
PROFILE_CONTROL_ACCESS = "Control Access"
PROFILE_DANTE_PRIMARY_ACCESS = "Dante Primary Access"
PROFILE_DANTE_SECONDARY_ACCESS = "Dante Secondary Access"
PROFILE_AUDIO_TRUNK = "Audio Trunk"
PROFILE_AUDIO_TRUNK_SECONDARY = "Audio Trunk Secondary"


class Command(BaseCommand):
    help = (
        "One-off import of the production addressing spreadsheet export (PLAN-prod-import.md). "
        "Refuses to start if any Rack already exists — creation order determines every rack's "
        "base address (first-fit), so this cannot be safely re-run against a partially-"
        "populated database. "
        "PRECONDITION: the app must be quiesced before running this — no other writer, admin "
        "session included. Range-suggestion reads are unlocked, so a concurrent rack/range "
        "creation can interleave with this import and leave either side wrong. "
        "Run `manage.py verify_prod_import` afterward to check the result against the CSVs."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--data-dir",
            default="prod",
            help="Directory containing the three source CSVs (default: prod/).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if Rack.objects.exists():
            raise CommandError(
                "Refusing to import: at least one Rack already exists. This importer's rack "
                "creation order determines every rack's base address and cannot be safely "
                "layered on top of existing racks — see PLAN-prod-import.md's "
                "'The constraint that shapes everything'."
            )

        data_dir = Path(options["data_dir"])
        addressing_path = data_dir / ADDRESSING_CSV_NAME
        calc_lookups_path = data_dir / CALC_LOOKUPS_CSV_NAME
        switch_ports_path = data_dir / SWITCH_PORTS_CSV_NAME
        for path in (addressing_path, calc_lookups_path, switch_ports_path):
            if not path.is_file():
                raise CommandError(f"Missing source CSV: {path}")

        vlan_rows = parse_vlan_table(read_csv_rows(calc_lookups_path))
        rack_offset_rows = parse_rack_offsets(read_csv_rows(calc_lookups_path))
        addressing_rows = parse_addressing_rows(read_csv_rows(addressing_path))
        switch_port_tables = {t.name: t for t in parse_switch_port_tables(read_csv_rows(switch_ports_path))}

        with transaction.atomic():
            importer = _Importer(self, vlan_rows, rack_offset_rows, addressing_rows, switch_port_tables)
            importer.run()

        self.stdout.write(self.style.SUCCESS("Production data import complete."))


@dataclass
class _SwitchEntry:
    rack: str
    slot: int
    description: str
    table_name: str
    secondary: bool


@dataclass
class _DeviceEntry:
    rack: str
    slot: int
    hostname: str
    type_key: str
    #: ADR 0022 — a Yamaha console's Device Control address, keyed by the
    #: type port ``description`` ("Device Control") that will materialize
    #: it, found by ``_classify_device_control_pairs()`` below and carried
    #: alongside the host entry so ``_stage9_devices()`` can pass it
    #: straight to ``NetworkDevice.operator_addresses``. Empty for every
    #: type with no ``OPERATOR``-sourced port.
    operator_addresses: dict[str, str] = field(default_factory=dict)


class _Importer:
    """Holds the mutable state one import run threads through its stages.

    A plain object rather than free functions in ``Command.handle()`` so
    each stage method can read what an earlier stage built (VLANs, the
    template, racks-by-name, profiles-by-name, types-by-key) without a long
    parameter list — this command runs exactly once per process, so there's
    no reuse concern that would push back toward statelessness.
    """

    def __init__(
        self,
        command: Command,
        vlan_rows: list[Any],
        rack_offset_rows: list[Any],
        addressing_rows: list[AddressingRow],
        switch_port_tables: dict[str, SwitchPortTable],
    ) -> None:
        self.command = command
        self.vlan_rows = vlan_rows
        self.rack_offset_rows = rack_offset_rows
        self.addressing_rows = addressing_rows
        self.switch_port_tables = switch_port_tables

        self.user: Any = None
        self.vlans_by_function: dict[str, VLAN] = {}
        self.profiles_by_name: dict[str, SwitchPortVlanProfile] = {}
        self.racks_by_name: dict[str, Rack] = {}
        #: Keyed by (switch-ports-CSV table name, is-secondary) — the same
        #: primary table backs both a switch's own type and its derived
        #: Secondary type (§3/§6), so the table name alone isn't unique.
        self.switch_types_by_table: dict[tuple[str, bool], NetworkSwitchType] = {}
        self.device_types_by_key: dict[str, NetworkDeviceType] = {}

    def run(self) -> None:
        self._stage1_import_user()
        self._stage2_vlans()
        self._stage3_switch_port_profiles()
        template = self._stage4_rack_template()
        self._stage5_racks(template)
        self._stage6_switch_types()
        self._stage7_device_types()
        switch_entries, device_entries = self._classify_addressing_rows()
        self._stage8_switches(switch_entries)
        self._stage9_devices(device_entries)

    # -- Stage 1: audit identity -------------------------------------------------

    def _stage1_import_user(self) -> None:
        """Resolve the dedicated, disabled import identity every row's
        ``created_by`` points at (ADR 0004).

        Not ``get_or_create()``: that skips ``full_clean()`` like every
        other direct-write shortcut this command avoids, and — the more
        pointed problem — ``defaults`` are only applied on *insert*, so a
        pre-existing row under this username would be adopted as-is
        regardless of its ``is_staff``/``is_active``/password state. A
        real, usable account that happens to be named ``prod-import``
        would then silently own every row's provenance instead of the
        intended disabled audit identity. This refuses that case loudly
        instead of either adopting a real account or silently disabling
        one out from under whoever it belongs to.
        """
        User = get_user_model()
        try:
            user = User.objects.get(username=IMPORT_USERNAME)
        except User.DoesNotExist:
            user = User(username=IMPORT_USERNAME, is_staff=False, is_active=False)
            user.set_unusable_password()
            user.full_clean()
            user.save()
        else:
            if user.is_staff or user.is_active or user.has_usable_password():
                raise CommandError(
                    f"A user named {IMPORT_USERNAME!r} already exists and is not the dedicated, "
                    "disabled import identity this command expects (is_staff=False, "
                    "is_active=False, no usable password) — rename or remove that account "
                    "before importing, so this run doesn't attribute every row to it."
                )
        self.user = user

    # -- Stage 2: VLANs -----------------------------------------------------------

    def _stage2_vlans(self) -> None:
        for row in self.vlan_rows:
            # DHCP range .2-.254, declared not observed — deliberately wider
            # than the real .2-.100 pool so first-fit skips the bottom /24
            # and reproduces production's rack bases (PLAN-prod-import.md
            # §2, filed as #43). Must be set before any rack range is
            # allocated (stage 5), so it's set at construction, not after.
            network = ipaddress.IPv4Network(row.subnet, strict=True)
            base = str(network.network_address).rsplit(".", 1)[0]
            vlan = VLAN(
                name=row.function,
                vlan_id=row.vlan_id,
                subnet=row.subnet,
                dhcp_range_start=f"{base}.2",
                dhcp_range_end=f"{base}.254",
                created_by=self.user,
            )
            vlan.full_clean()  # suggests default_gateway
            vlan.save()
            self.vlans_by_function[row.function] = vlan

    # -- Stage 3: switch port VLAN profiles ----------------------------------------

    def _stage3_switch_port_profiles(self) -> None:
        control_vlan = self.vlans_by_function[FN_CONTROL]
        dante_p_vlan = self.vlans_by_function[FN_DANTE_PRIMARY]
        dante_s_vlan = self.vlans_by_function[FN_DANTE_SECONDARY]
        real_vlan_ids = {vlan.vlan_id for vlan in self.vlans_by_function.values()}

        self._create_profile(PROFILE_CONTROL_ACCESS, PortMode.ACCESS, control_vlan, frozenset())
        self._create_profile(PROFILE_DANTE_PRIMARY_ACCESS, PortMode.ACCESS, dante_p_vlan, frozenset())
        self._create_profile(PROFILE_DANTE_SECONDARY_ACCESS, PortMode.ACCESS, dante_s_vlan, frozenset())

        primary_trunk_allowed = self._primary_trunk_allowed_vlan_ids()
        audio_trunk_allowed_ids = {
            vlan_id
            for vlan_id in primary_trunk_allowed
            if vlan_id in real_vlan_ids and vlan_id != dante_p_vlan.vlan_id
        }
        self._create_profile(
            PROFILE_AUDIO_TRUNK,
            PortMode.TRUNK,
            dante_p_vlan,
            frozenset(self._vlan_by_id(v) for v in audio_trunk_allowed_ids),
        )

        # Secondary switch control ports are unused; ports with Native VLAN
        # 201 become Native VLAN 202, and 201 replaces 202 in the allowed
        # list (Switch Ports.csv's own note; PLAN-prod-import.md §3).
        secondary_allowed_ids = (audio_trunk_allowed_ids - {dante_s_vlan.vlan_id}) | {dante_p_vlan.vlan_id}
        secondary_allowed_ids = {
            v for v in secondary_allowed_ids if v in real_vlan_ids and v != dante_s_vlan.vlan_id
        }
        self._create_profile(
            PROFILE_AUDIO_TRUNK_SECONDARY,
            PortMode.TRUNK,
            dante_s_vlan,
            frozenset(self._vlan_by_id(v) for v in secondary_allowed_ids),
        )

    def _primary_trunk_allowed_vlan_ids(self) -> frozenset[int]:
        table = self.switch_port_tables["Cisco SG300-10MP (For 3xAmp Rack Primary)"]
        dante_p_id = self.vlans_by_function[FN_DANTE_PRIMARY].vlan_id
        for port in table.ports:
            if port.mode == PortMode.TRUNK and port.native_vlan_id == dante_p_id:
                return port.allowed_vlan_ids
        raise CommandError(f"No trunk port found in {table.name!r} to derive the Audio Trunk profile from.")

    def _vlan_by_id(self, vlan_id: int) -> VLAN:
        for vlan in self.vlans_by_function.values():
            if vlan.vlan_id == vlan_id:
                return vlan
        raise CommandError(f"No VLAN {vlan_id} was created in stage 2.")

    def _create_profile(
        self, name: str, mode: str, native_vlan: VLAN, allowed_vlans: frozenset[VLAN]
    ) -> SwitchPortVlanProfile:
        profile = SwitchPortVlanProfile(
            name=name, port_mode=mode, native_vlan=native_vlan, all_vlans_allowed=False, created_by=self.user
        )
        profile.full_clean()
        profile.save()
        for vlan in allowed_vlans:
            link = SwitchPortVlanProfileAllowedVlan(profile=profile, vlan=vlan, created_by=self.user)
            link.full_clean()
            link.save()
        self.profiles_by_name[name] = profile
        return profile

    # -- Stage 4: rack template -----------------------------------------------------

    def _stage4_rack_template(self) -> RackTemplate:
        template = RackTemplate(name="Audio Rack", created_by=self.user)
        template.full_clean()
        template.save()
        for function in (FN_CONTROL, FN_DANTE_PRIMARY, FN_DANTE_SECONDARY):
            link = RackTemplateVlan(
                template=template, vlan=self.vlans_by_function[function], created_by=self.user
            )
            link.full_clean()
            link.save()
        return template

    # -- Stage 5: racks, in ascending Address Offset order ---------------------------

    def _stage5_racks(self, template: RackTemplate) -> None:
        for row in self.rack_offset_rows:
            rack = Rack(name=row.rack, slot_count=30, created_by=self.user)
            if row.rack in MANUAL_RANGE_RACKS:
                rack.full_clean()
                rack.save()
                for function in (FN_CONTROL, FN_DANTE_PRIMARY, FN_DANTE_SECONDARY):
                    vlan = self.vlans_by_function[function]
                    address_range = self._manual_range(vlan, row.offset)
                    rng = RackVlanRange(
                        rack=rack, vlan=vlan, address_range=address_range, created_by=self.user
                    )
                    rng.full_clean()
                    rng.save()
            else:
                rack.template = template
                rack.full_clean()
                rack.save()
            self.racks_by_name[row.rack] = rack

    @staticmethod
    def _manual_range(vlan: VLAN, offset: int) -> str:
        network = ipaddress.IPv4Network(vlan.subnet, strict=True)
        base = network.network_address + offset
        return f"{base}/27"

    # -- Stage 6: switch types --------------------------------------------------------

    def _stage6_switch_types(self) -> None:
        for table_name, (manufacturer, model, name) in PRIMARY_SWITCH_TABLES.items():
            table = self.switch_port_tables[table_name]
            self.switch_types_by_table[(table_name, False)] = self._create_primary_switch_type(
                manufacturer, model, name, table
            )
        for table_name, (manufacturer, model, name) in SECONDARY_DERIVED_TABLES.items():
            table = self.switch_port_tables[table_name]
            self.switch_types_by_table[(table_name, True)] = self._create_secondary_switch_type(
                manufacturer, model, name, table
            )

    def _create_primary_switch_type(
        self, manufacturer: str, model: str, name: str, table: SwitchPortTable
    ) -> NetworkSwitchType:
        switch_type = NetworkSwitchType(
            manufacturer=manufacturer,
            model=model,
            name=name,
            port_count=len(table.ports),
            created_by=self.user,
        )
        switch_type.full_clean()
        switch_type.save()
        for port in table.ports:
            profile = self._primary_profile_for(port)
            type_port = NetworkSwitchTypePort(
                switch_type=switch_type,
                port_number=port.port_number,
                description=port.description,
                port_type=port.port_type,
                profile=profile,
                created_by=self.user,
            )
            type_port.full_clean()
            type_port.save()
        return switch_type

    def _create_secondary_switch_type(
        self, manufacturer: str, model: str, name: str, primary_table: SwitchPortTable
    ) -> NetworkSwitchType:
        switch_type = NetworkSwitchType(
            manufacturer=manufacturer,
            model=model,
            name=name,
            port_count=len(primary_table.ports),
            created_by=self.user,
        )
        switch_type.full_clean()
        switch_type.save()
        for port in primary_table.ports:
            profile = self._secondary_profile_for(port)
            type_port = NetworkSwitchTypePort(
                switch_type=switch_type,
                port_number=port.port_number,
                description=port.description,
                port_type=port.port_type,
                profile=profile,
                created_by=self.user,
            )
            type_port.full_clean()
            type_port.save()
        return switch_type

    def _primary_profile_for(self, port: SwitchPortRow) -> SwitchPortVlanProfile:
        control_id = self.vlans_by_function[FN_CONTROL].vlan_id
        dante_p_id = self.vlans_by_function[FN_DANTE_PRIMARY].vlan_id
        if port.mode == PortMode.ACCESS and port.native_vlan_id == control_id:
            return self.profiles_by_name[PROFILE_CONTROL_ACCESS]
        if port.mode == PortMode.ACCESS and port.native_vlan_id == dante_p_id:
            return self.profiles_by_name[PROFILE_DANTE_PRIMARY_ACCESS]
        if port.mode == PortMode.TRUNK and port.native_vlan_id == dante_p_id:
            return self.profiles_by_name[PROFILE_AUDIO_TRUNK]
        raise CommandError(
            f"Cannot classify switch port {port.description!r} (mode={port.mode}, "
            f"native={port.native_vlan_id}) against a known profile."
        )

    def _secondary_profile_for(self, port: SwitchPortRow) -> SwitchPortVlanProfile:
        """Independent-in-spirit derivation from the primary port row —
        control ports are unpatched but keep their configuration; Dante
        Primary access/trunk ports move to their Secondary counterpart.
        See ``verify_prod_import.py`` for the *actually* independent oracle
        this same rule must separately satisfy at verification time.
        """
        control_id = self.vlans_by_function[FN_CONTROL].vlan_id
        dante_p_id = self.vlans_by_function[FN_DANTE_PRIMARY].vlan_id
        if port.mode == PortMode.ACCESS and port.native_vlan_id == control_id:
            return self.profiles_by_name[PROFILE_CONTROL_ACCESS]
        if port.mode == PortMode.ACCESS and port.native_vlan_id == dante_p_id:
            return self.profiles_by_name[PROFILE_DANTE_SECONDARY_ACCESS]
        if port.mode == PortMode.TRUNK and port.native_vlan_id == dante_p_id:
            return self.profiles_by_name[PROFILE_AUDIO_TRUNK_SECONDARY]
        raise CommandError(
            f"Cannot derive a secondary profile for switch port {port.description!r} "
            f"(mode={port.mode}, native={port.native_vlan_id})."
        )

    # -- Stage 7: device types ---------------------------------------------------------

    def _stage7_device_types(self) -> None:
        for spec in DEVICE_TYPES:
            device_type = NetworkDeviceType(
                manufacturer=spec.manufacturer,
                model=spec.model,
                name=spec.name,
                port_count=len(spec.ports),
                created_by=self.user,
            )
            device_type.full_clean()
            device_type.save()
            for description, function, slot_offset, address_source, hostname_suffix in spec.ports:
                type_port = NetworkDeviceTypePort(
                    device_type=device_type,
                    description=description,
                    port_type=PortType.GBE_RJ45,
                    vlan=self.vlans_by_function[function],
                    slot_offset=slot_offset,
                    address_source=address_source,
                    hostname_suffix=hostname_suffix,
                    created_by=self.user,
                )
                type_port.full_clean()
                type_port.save()
            self.device_types_by_key[spec.key] = device_type

    # -- Row classification (dedup, switch/device split, SD12 collapse, DMI-DANTE) ----

    def _classify_addressing_rows(self) -> tuple[list[_SwitchEntry], list[_DeviceEntry]]:
        deduped = self._dedupe_rows(self.addressing_rows)
        by_key = {(row.rack, row.slot): row for row in deduped}
        consumed: set[tuple[str, int]] = set()
        switch_entries: list[_SwitchEntry] = []
        device_entries: list[_DeviceEntry] = []
        dmi_dante_racks: set[str] = set()
        max_slot_by_rack: dict[str, int] = {}

        for row in deduped:
            max_slot_by_rack[row.rack] = max(max_slot_by_rack.get(row.rack, 0), row.slot)

        # Device Control pre-pass (ADR 0022) — runs to completion before
        # the main per-row loop below, so a Device Control row is always
        # consumed before that loop reaches it regardless of CSV order
        # (the DM7C's Device Control row precedes its host; the DM3's
        # follows it). See _classify_device_control_pairs()'s own docstring for
        # why this can't be expressed as the SD12 pass's slot-adjacency
        # instead.
        self._classify_device_control_pairs(deduped, consumed, device_entries)

        for row in deduped:
            key = (row.rack, row.slot)
            if key in consumed:
                continue

            if row.description == NETGEAR_DESCRIPTION:
                consumed.add(key)
                continue  # deferred (§6) — no switch created for this row

            table_name = SWITCH_DESCRIPTION_TO_TABLE.get(row.description)
            if table_name is not None:
                secondary = "Secondary" in row.description
                switch_entries.append(
                    _SwitchEntry(row.rack, row.slot, row.description, table_name, secondary)
                )
                consumed.add(key)
                continue
            if _TLSG108E_RE.match(row.description):
                switch_entries.append(
                    _SwitchEntry(row.rack, row.slot, row.description, "TP-Link TL-SG108E", False)
                )
                consumed.add(key)
                continue

            # SD12 Control/Engine pair collapse (ADR 0017; PLAN-prod-import.md §7 tier 2).
            if row.description.endswith("-Control"):
                stem = row.description[: -len("-Control")]
                engine_key = (row.rack, row.slot + 1)
                engine_row = by_key.get(engine_key)
                if engine_row is not None and engine_row.description == f"{stem}-Engine":
                    device_entries.append(_DeviceEntry(row.rack, row.slot, stem, "sd12"))
                    consumed.add(key)
                    consumed.add(engine_key)
                    if row.dante_primary or row.dante_secondary:
                        # The DMI-DANTE-card artifact (PROD-DATA-ANALYSIS.md
                        # §5): the console's own "-Control" row is carrying
                        # the card's addresses. Re-addressed on the
                        # hardware at its own ordinal (#41), not copied.
                        dmi_dante_racks.add(row.rack)
                    continue

            type_key = self._device_type_key_for(row)
            hostname = row.description
            device_entries.append(_DeviceEntry(row.rack, row.slot, hostname, type_key))
            consumed.add(key)

        if dmi_dante_racks:
            if dmi_dante_racks != {DMI_DANTE_RACK}:
                raise CommandError(
                    f"DMI-DANTE marker found on {sorted(dmi_dante_racks)}, but "
                    f"PLAN-prod-import.md §9 pins the card to {DMI_DANTE_RACK} slot "
                    f"{DMI_DANTE_SLOT} — refusing to guess a slot for a different rack."
                )
            if (DMI_DANTE_RACK, DMI_DANTE_SLOT) in by_key:
                raise CommandError(
                    f"{DMI_DANTE_RACK} slot {DMI_DANTE_SLOT} is pinned for the DMI-DANTE card "
                    "(PLAN-prod-import.md §9) but the addressing sheet already has a row there."
                )
            if max_slot_by_rack.get(DMI_DANTE_RACK, 0) >= DMI_DANTE_SLOT:
                raise CommandError(
                    f"{DMI_DANTE_RACK} already has an occupant at or past the pinned DMI-DANTE "
                    f"slot {DMI_DANTE_SLOT} (PLAN-prod-import.md §9) — the export has grown past "
                    "what the pin assumed; resolve this before importing."
                )
            device_entries.append(_DeviceEntry(DMI_DANTE_RACK, DMI_DANTE_SLOT, "", "dmi_dante"))

        return switch_entries, device_entries

    def _classify_device_control_pairs(
        self,
        deduped: list[AddressingRow],
        consumed: set[tuple[str, int]],
        device_entries: list[_DeviceEntry],
    ) -> None:
        """Pairs a ``<host>-device-control`` row with its host (ADR 0022),
        emitting one merged ``_DeviceEntry`` at the *host's* slot — the
        Device Control row's own address folded into the host's
        ``operator_addresses`` rather than a separate device — and marking
        both rows consumed before ``_classify_addressing_rows()``'s main
        per-row loop ever reaches either of them.

        Keyed on hostname, not the SD12 pass's slot-adjacency
        (``row.slot + 1``) — that can't express a Device Control row sitting
        *below* its host (the DM7C's interface, one address below its
        console) and *above* it (the DM3's, one address above) in the
        same catalog. Matches case-insensitively, within one rack — the
        same convention the pre-ADR-0022 importer used, stated once here
        since this importer is the forward-going path.

        Zero or several stem matches, or a host type with no
        ``OPERATOR``-sourced type port to receive the address (a rename,
        or a row misclassified as a Device Control row) is a loud
        ``CommandError`` — never a guess.
        """
        for row in deduped:
            key = (row.rack, row.slot)
            if key in consumed or not row.description.lower().endswith(DEVICE_CONTROL_SUFFIX):
                continue
            stem = row.description[: -len(DEVICE_CONTROL_SUFFIX)]
            matches = [
                other
                for other in deduped
                if other.rack == row.rack and other is not row and other.description.lower() == stem.lower()
            ]
            if len(matches) != 1:
                raise CommandError(
                    f"Device Control row {row.description!r} at {row.rack} slot {row.slot} (ADR "
                    f"0022): expected exactly one host row matching {stem!r} (case-insensitive) "
                    f"in the same rack, found {len(matches)}."
                )
            host_row = matches[0]
            host_key = self._device_type_key_for(host_row)
            host_spec = SPEC_BY_KEY[host_key]
            operator_ports = [
                (description, function)
                for description, function, _slot_offset, address_source, _hostname_suffix in host_spec.ports
                if address_source == PortAddressSource.OPERATOR
            ]
            if len(operator_ports) != 1:
                raise CommandError(
                    f"Device Control row {row.description!r} matches host {host_row.description!r} "
                    f"(type {host_key!r}), but that type declares {len(operator_ports)} "
                    "OPERATOR-sourced ports, not exactly one — ADR 0022 catalog mismatch."
                )
            port_description, function = operator_ports[0]
            address = _ADDRESS_BY_FUNCTION[function](row)
            if not address:
                raise CommandError(
                    f"Device Control row {row.description!r} at {row.rack} slot {row.slot} has no "
                    f"address for {function!r} — nothing to give {host_row.description!r}'s "
                    f"{port_description!r} port."
                )
            device_entries.append(
                _DeviceEntry(
                    host_row.rack,
                    host_row.slot,
                    host_row.description,
                    host_key,
                    operator_addresses={port_description: address},
                )
            )
            consumed.add((host_row.rack, host_row.slot))
            consumed.add(key)

    @staticmethod
    def _dedupe_rows(rows: list[AddressingRow]) -> list[AddressingRow]:
        """Collapse repeated ``(rack, slot)`` rows to their first occurrence
        (PROD-DATA-ANALYSIS.md §2.2) — sheet residue from reshaping cycles,
        confirmed identical in content, one device per slot.
        """
        seen: set[tuple[str, int]] = set()
        result = []
        for row in rows:
            key = (row.rack, row.slot)
            if key in seen:
                continue
            seen.add(key)
            result.append(row)
        return result

    def _device_type_key_for(self, row: AddressingRow) -> str:
        if row.description == "IK42" and (row.rack, row.slot) in IK42_WITHOUT_DANTE_CARD:
            return "ik42_without_card"
        mapped = DESCRIPTION_TO_DEVICE_KEY.get(row.description)
        if mapped is not None:
            return mapped
        for pattern, key in AVIO_PATTERNS:
            if pattern.match(row.description):
                return key
        raise CommandError(
            f"Could not classify addressing row {row.description!r} at {row.rack} slot {row.slot} "
            "as a known switch or device type."
        )

    # -- Stage 8: switches -----------------------------------------------------------

    def _stage8_switches(self, entries: list[_SwitchEntry]) -> None:
        for entry in entries:
            switch_type = self.switch_types_by_table[(entry.table_name, entry.secondary)]
            rack = self.racks_by_name[entry.rack]
            switch = NetworkSwitch(
                switch_type=switch_type,
                rack=rack,
                rack_slot=entry.slot,
                hostname=entry.description,
                dhcp_server_enabled=entry.rack in DHCP_SERVER_RACKS,
                created_by=self.user,
            )
            switch.full_clean()
            switch.save()  # materializes ports + one address per RackVlanRange (ADR 0016)

    # -- Stage 9: devices -------------------------------------------------------------

    def _stage9_devices(self, entries: list[_DeviceEntry]) -> None:
        for entry in entries:
            device_type = self.device_types_by_key[entry.type_key]
            rack = self.racks_by_name[entry.rack]
            device = NetworkDevice(  # type: ignore[misc]
                device_type=device_type,
                rack=rack,
                rack_slot=entry.slot,
                hostname=entry.hostname,
                # ADR 0022 — {} for every ordinary entry; populated only by
                # _classify_device_control_pairs() for a Device Control host
                # entry, and _materialize_ports() ignores it entirely
                # unless device_type actually has an OPERATOR-sourced port.
                operator_addresses=entry.operator_addresses,
                created_by=self.user,
            )
            device.full_clean()
            device.save()  # materializes static addresses (ADR 0013, 0017, 0022)
