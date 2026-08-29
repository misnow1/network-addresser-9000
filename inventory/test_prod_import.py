"""Tests for the production-data importer and its verifier.

These deliberately do **not** touch ``prod/`` — that directory is gitignored,
real site data, and the whole point of this suite is to exercise the same
*shapes* the real import hits (first-fit base reproduction, a manual-range
rack, an SD12-shaped ``slot_offset`` device with its DMI-DANTE card, a
secondary switch's derived profile, the duplicate-row collapse, and the
three deliberate address drops) against small synthetic CSVs built here, on
an invented VLAN numbering (130s) whose addresses appear nowhere in
``prod/``.

**Rack and product names are a different case, reused on purpose, not
invented.** ``AMPRACK1``/``W8LMTEST`` are made up, but ``XE300-1``,
``AVIO``, ``SHURE`` and ``CONSOLES`` are the real production rack names —
and have to be, because ``import_prod_data.py`` classifies several
behaviours (the manual-range racks, the no-Dante-card IK-42 slots, the
AVIO Control-address drop) by hardcoded, site-specific rack name, per
PLAN-prod-import.md's own design (§"The constraint that shapes
everything": "SHURE and CONSOLES must be created without a template").
Inventing fictional names for these racks would mean either not exercising
that hardcoded matching at all, or duplicating it under different literals
— testing a parallel implementation instead of the real one. Model/part
names (``IK42``, ``LM26``, ``Cisco SG300-10MP``, ``mps-avio-...``) are
reused the same way. None of this is site data the way row-level address
assignments are: every rack and product name here already appears verbatim,
in full, in `PLAN-prod-import.md`/`PROD-DATA-ANALYSIS.md` — committed,
non-gitignored files in this repo — so reusing them discloses nothing that
isn't already public. What ``prod/`` actually protects, and what this
suite genuinely invents, is the *address* assignments: every IP address
below is fabricated for this suite and appears in no CSV under ``prod/``.
"""

import csv
import importlib
import ipaddress
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from .management.commands._prod_import_csv import parse_device_models
from .management.commands.import_prod_data import (
    DEVICE_MODEL_SLUGS,
    DEVICE_TYPES,
    PRIMARY_SWITCH_TABLES,
    SECONDARY_DERIVED_TABLES,
)
from .management.commands.import_prod_data import HOSTNAME_SLUGS as IMPORTER_HOSTNAME_SLUGS
from .management.commands.verify_prod_import import HOSTNAME_SLUGS as VERIFIER_HOSTNAME_SLUGS
from .models import (
    NetworkDevice,
    NetworkDeviceModel,
    NetworkDevicePort,
    NetworkDeviceType,
    NetworkDeviceTypePort,
    NetworkSwitch,
    NetworkSwitchAddress,
    NetworkSwitchType,
    Owner,
    Rack,
    RackVlanRange,
)

# A migration module's name isn't a valid Python identifier ("0018_..."
# starts with a digit), so importlib rather than a plain `from ... import` —
# same reasoning tests.py's migration-reconstruction test classes already
# use. Importing it is safe and does nothing to the database; only actually
# *applying* a migration (via MigrationExecutor) runs its RunPython
# functions.
MIGRATION_HOSTNAME_SLUGS = importlib.import_module(
    "inventory.migrations.0018_seed_hostname_slugs"
).HOSTNAME_SLUGS

# -- Synthetic VLAN/rack scheme -----------------------------------------------------

FN_CONTROL = "Audio Control"
FN_DANTE_PRIMARY = "Dante Primary"
FN_DANTE_SECONDARY = "Dante Secondary"
FN_AES67 = "AES67"

#: (function, vlan_id, network_address, netmask)
VLAN_TABLE = [
    (FN_CONTROL, 130, "10.130.0.0", "255.255.248.0"),
    (FN_DANTE_PRIMARY, 131, "10.131.0.0", "255.255.248.0"),
    (FN_DANTE_SECONDARY, 132, "10.132.0.0", "255.255.248.0"),
    (FN_AES67, 137, "10.137.0.0", "255.255.248.0"),
]
_NETWORK_BY_FUNCTION = {row[0]: row[2] for row in VLAN_TABLE}
_VLAN_ID_BY_FUNCTION = {row[0]: row[1] for row in VLAN_TABLE}

#: (rack name, offset) in ascending order — AMPRACK1/XE300-1/AVIO/W8LMTEST are
#: template-allocated (first-fit); SHURE/CONSOLES are manual, mirroring
#: PLAN-prod-import.md's own two manual-range racks by name.
RACKS = [
    ("AMPRACK1", 256),
    ("XE300-1", 288),
    ("AVIO", 320),
    ("W8LMTEST", 352),
    ("SHURE", 800),
    ("CONSOLES", 864),
]
_OFFSET_BY_RACK = dict(RACKS)


def addr(function: str, rack: str, slot: int) -> str:
    """``base(function's VLAN) + rack's offset + slot`` — the same formula
    the app implements, used here only to build fixture input, not to
    check anything.
    """
    network = ipaddress.IPv4Network(f"{_NETWORK_BY_FUNCTION[function]}/21", strict=True)
    return str(network.network_address + _OFFSET_BY_RACK[rack] + slot)


# -- Switch Ports CSV tables ---------------------------------------------------------
# Column shape: (description, port, native VLAN label, mode, allowed, port type, note)

TABLE_SG300_3XAMP_PRIMARY = [
    ("Amp 1 Control", "1", "130 (Control)", "Access", "", "1GbE Copper", ""),
    ("Amp 2 Control", "2", "130 (Control)", "Access", "", "1GbE Copper", ""),
    ("Amp 3 Control", "3", "130 (Control)", "Access", "", "1GbE Copper", ""),
    ("Amp 1 Dante", "4", "131 (Dante Primary)", "Access", "", "1GbE Copper", ""),
    ("Amp 2 Dante", "5", "131 (Dante Primary)", "Access", "", "1GbE Copper", ""),
    ("Amp 3 Dante", "6", "131 (Dante Primary)", "Access", "", "1GbE Copper", ""),
    ("Patch Panel 1", "7", "131 (Dante Primary)", "Trunk", "130, 132-137", "1GbE Copper", ""),
    ("Patch Panel 2", "8", "131 (Dante Primary)", "Trunk", "130, 132-137", "1GbE Copper", ""),
    ("Patch Panel 3", "9", "131 (Dante Primary)", "Trunk", "130, 132-137", "Combo Port (1GbE + SFP)", ""),
    ("Patch Panel 4", "10", "131 (Dante Primary)", "Trunk", "130, 132-137", "Combo Port (1GbE + SFP)", ""),
]
TABLE_SG350_2XAMP_PRIMARY = [
    ("Amp 1 Control", "1", "130 (Control)", "Access", "", "1GbE Copper", ""),
    ("Amp 2 Control", "2", "130 (Control)", "Access", "", "1GbE Copper", ""),
    ("Amp 1 Dante", "3", "131 (Dante Primary)", "Access", "", "1GbE Copper", ""),
    ("Patch Panel 1", "4", "131 (Dante Primary)", "Trunk", "130, 132-137", "1GbE Copper", ""),
]
TABLE_SG300_DRIVE_PRIMARY = [
    ("WAP", "1", "131 (Dante Primary)", "Access", "", "1GbE Copper", ""),
    ("Patch Panel 1", "2", "131 (Dante Primary)", "Trunk", "130, 132-137", "1GbE Copper", ""),
]
TABLE_TLSG108E = [
    ("Audio Trunk", "1", "131 (Dante Primary)", "Trunk", "130, 132-137", "1GbE Copper", ""),
    ("Audio Trunk", "2", "131 (Dante Primary)", "Trunk", "130, 132-137", "1GbE Copper", ""),
]
TABLE_SG300_26P = [
    ("Audio Control", "1", "130 (Control)", "Access", "", "1GbE Copper", ""),
    ("Dante Primary", "2", "131 (Dante Primary)", "Access", "", "1GbE Copper", ""),
    ("Audio Trunk", "3", "131 (Dante Primary)", "Trunk", "130, 132-137", "1GbE Copper", ""),
]

SWITCH_PORT_TABLES = [
    ("Cisco SG300-10MP (For 3xAmp Rack Primary)", TABLE_SG300_3XAMP_PRIMARY),
    ("Cisco SG350-10 (For 2xAmp Rack Primary)", TABLE_SG350_2XAMP_PRIMARY),
    ("Cisco SG300-10MP (For Drive Rack Primary)", TABLE_SG300_DRIVE_PRIMARY),
    ("TP-Link TL-SG108E", TABLE_TLSG108E),
    ("Cisco SG300-26P", TABLE_SG300_26P),
]

# -- Addressing CSV rows --------------------------------------------------------------
# (description, rack, slot, control, dante_primary, dante_secondary, notes)

ADDRESSING_ROWS = [
    # AMPRACK1: primary + secondary switch, one IK42 (duplicated row — dedup test).
    (
        "Cisco SG300-10MP (For 3xAmp Rack Primary)",
        "AMPRACK1",
        1,
        addr(FN_CONTROL, "AMPRACK1", 1),
        addr(FN_DANTE_PRIMARY, "AMPRACK1", 1),
        "",
        "",
    ),
    (
        "Cisco SG300-10MP (For 3xAmp Rack Secondary)",
        "AMPRACK1",
        2,
        addr(FN_CONTROL, "AMPRACK1", 2),
        "",
        addr(FN_DANTE_SECONDARY, "AMPRACK1", 2),
        "",
    ),
    (
        "IK42",
        "AMPRACK1",
        3,
        addr(FN_CONTROL, "AMPRACK1", 3),
        addr(FN_DANTE_PRIMARY, "AMPRACK1", 3),
        addr(FN_DANTE_SECONDARY, "AMPRACK1", 3),
        "",
    ),
    (  # sheet residue: exact duplicate of the row above (PROD-DATA-ANALYSIS.md §2.2)
        "IK42",
        "AMPRACK1",
        3,
        addr(FN_CONTROL, "AMPRACK1", 3),
        addr(FN_DANTE_PRIMARY, "AMPRACK1", 3),
        addr(FN_DANTE_SECONDARY, "AMPRACK1", 3),
        "",
    ),
    # XE300-1: IK-42s with no Dante card fitted (§5.2 — DP/DS dropped).
    (
        "IK42",
        "XE300-1",
        3,
        addr(FN_CONTROL, "XE300-1", 3),
        addr(FN_DANTE_PRIMARY, "XE300-1", 3),
        addr(FN_DANTE_SECONDARY, "XE300-1", 3),
        "",
    ),
    (
        "IK42",
        "XE300-1",
        4,
        addr(FN_CONTROL, "XE300-1", 4),
        addr(FN_DANTE_PRIMARY, "XE300-1", 4),
        addr(FN_DANTE_SECONDARY, "XE300-1", 4),
        "",
    ),
    # AVIO: single-port Dante adapters (§5.3 — Control dropped).
    (
        "mps-avio-radial-tx",
        "AVIO",
        1,
        addr(FN_CONTROL, "AVIO", 1),
        addr(FN_DANTE_PRIMARY, "AVIO", 1),
        "",
        "",
    ),
    (
        "mps-avio-na2-dline-1",
        "AVIO",
        2,
        addr(FN_CONTROL, "AVIO", 2),
        addr(FN_DANTE_PRIMARY, "AVIO", 2),
        "",
        "",
    ),
    # W8LMTEST: deferred Netgear switch (skipped entirely), plus an LM26
    # (§5.1 — Control dropped, no control interface exists).
    (
        "Netgear Managed Switch (For W8LM Rack)",
        "W8LMTEST",
        1,
        addr(FN_CONTROL, "W8LMTEST", 1),
        addr(FN_DANTE_PRIMARY, "W8LMTEST", 1),
        "",
        "",
    ),
    (
        "LM26",
        "W8LMTEST",
        2,
        addr(FN_CONTROL, "W8LMTEST", 2),
        addr(FN_DANTE_PRIMARY, "W8LMTEST", 2),
        addr(FN_DANTE_SECONDARY, "W8LMTEST", 2),
        "",
    ),
    # CONSOLES (manual range): an SD12-shaped Control/Engine pair whose
    # Control row also carries the DMI-DANTE marker (synthetic, deliberately
    # NOT base+slot — the card is re-addressed on its own ordinal, #41),
    # plus a Control-only console.
    (
        "SD12-TEST-1-Control",
        "CONSOLES",
        1,
        addr(FN_CONTROL, "CONSOLES", 1),
        "10.131.9.9",
        "10.132.9.9",
        "Used as DMI-DANTE2 Addresses?",
    ),
    (
        "SD12-TEST-1-Engine",
        "CONSOLES",
        2,
        addr(FN_CONTROL, "CONSOLES", 2),
        "",
        "",
        "No Dante interfaces",
    ),
    (
        "SD9",
        "CONSOLES",
        4,
        addr(FN_CONTROL, "CONSOLES", 4),
        "",
        "",
        "",
    ),
    # CONSOLES: a DM7C console + its Device Control row, sitting one
    # address above its host (ADR 0027 retires ADR 0022's per-instance
    # OPERATOR mechanism — a Yamaha console's Device Control interface is
    # now an ordinary slot_offset=1 type port, so every instance's
    # interface sits at the same fixed offset from its own console,
    # matching production after ADR 0027 plan Step 0's hand move).
    (
        "DM7C-1",
        "CONSOLES",
        6,
        addr(FN_CONTROL, "CONSOLES", 6),
        addr(FN_DANTE_PRIMARY, "CONSOLES", 6),
        addr(FN_DANTE_SECONDARY, "CONSOLES", 6),
        "",
    ),
    (
        "dm7c-1-device-control",
        "CONSOLES",
        7,
        "",
        addr(FN_DANTE_PRIMARY, "CONSOLES", 7),
        "",
        "Only on Dante Primary for controlling snakes",
    ),
    # CONSOLES: a DM3 console + its Device Control row, sitting one
    # address above its host too — the same fixed offset as DM7C now that
    # ADR 0027 makes it a type-level slot_offset rather than a
    # per-instance OPERATOR address.
    (
        "bej-dm3-1",
        "CONSOLES",
        8,
        addr(FN_CONTROL, "CONSOLES", 8),
        addr(FN_DANTE_PRIMARY, "CONSOLES", 8),
        addr(FN_DANTE_SECONDARY, "CONSOLES", 8),
        "",
    ),
    (
        "bej-dm3-1-device-control",
        "CONSOLES",
        9,
        "",
        addr(FN_DANTE_PRIMARY, "CONSOLES", 9),
        "",
        "",
    ),
]


def _write_csv(path: Path, rows: list) -> None:
    with path.open("w", newline="") as handle:
        csv.writer(handle).writerows(rows)


def _calc_lookups_rows() -> list:
    rows: list = [[""] * 10]
    rows.append(
        [
            "Function",
            "Address Range",
            "VLAN Base Address",
            "Netmask",
            "Rack",
            "Address Offset",
            "Control Base Address",
            "Dante Primary Base Address",
            "Notes",
            "Rack Increment",
        ]
    )
    for i in range(max(len(VLAN_TABLE), len(RACKS))):
        vlan_part = VLAN_TABLE[i] if i < len(VLAN_TABLE) else ("", "", "", "")
        rack_part = RACKS[i] if i < len(RACKS) else ("", "")
        rows.append(
            [
                vlan_part[0],
                str(vlan_part[1]) if vlan_part[1] != "" else "",
                vlan_part[2],
                vlan_part[3],
                rack_part[0],
                str(rack_part[1]) if rack_part[1] != "" else "",
                "",
                "",
                "",
                "",
            ]
        )
    return rows


def _switch_ports_rows() -> list:
    rows: list = []
    for index, (name, ports) in enumerate(SWITCH_PORT_TABLES):
        if index > 0:
            rows.append([""] * 9)
            rows.append(["", "", "", "", "", "", "", "", name])
        header_name = name if index == 0 else ""
        rows.append(
            [
                "Description",
                "Port",
                "Native VLAN",
                "Mode",
                "Allowed VLANS",
                "Port Type",
                "Note",
                "",
                header_name,
            ]
        )
        for port_row in ports:
            rows.append(list(port_row) + [""])
    return rows


def _addressing_rows(source_rows: list | None = None) -> list:
    rows: list = [
        [
            "Device Description",
            "Location/Rack",
            "Slot",
            "Audio Control",
            "Dante Primary",
            "Dante Secondary",
            "Notes",
        ]
    ]
    for row in ADDRESSING_ROWS if source_rows is None else source_rows:
        rows.append([row[0], row[1], str(row[2]), row[3], row[4], row[5], row[6]])
    return rows


def write_fixture_csvs(data_dir: Path, addressing_source_rows: list | None = None) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(data_dir / "MPS Audio Network Standards - IP Calc Lookups.csv", _calc_lookups_rows())
    _write_csv(data_dir / "MPS Audio Network Standards - Switch Ports.csv", _switch_ports_rows())
    _write_csv(
        data_dir / "MPS Audio Network Standards - IP Addressing mk2.csv",
        _addressing_rows(addressing_source_rows),
    )


class ImportProdDataTests(TestCase):
    _tmpdir: tempfile.TemporaryDirectory
    data_dir: Path

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.data_dir = Path(cls._tmpdir.name)
        write_fixture_csvs(cls.data_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()
        super().tearDownClass()

    def setUp(self) -> None:
        call_command("import_prod_data", data_dir=str(self.data_dir))

    def test_refuses_when_a_rack_already_exists(self) -> None:
        # setUp() has already run the importer once, so at least one Rack
        # exists — re-running must refuse rather than layer a second import
        # on top (PLAN-prod-import.md: creation order is load-bearing).
        self.assertTrue(Rack.objects.exists())
        with self.assertRaises(CommandError):
            call_command("import_prod_data", data_dir=str(self.data_dir))

    def test_first_fit_bases_and_manual_ranges(self) -> None:
        for rack_name, offset in RACKS:
            rack = Rack.objects.get(name=rack_name)
            for function, vlan_id, network_addr, _netmask in VLAN_TABLE[:3]:
                expected = f"{ipaddress.IPv4Address(network_addr) + offset}/27"
                actual = RackVlanRange.objects.get(rack=rack, vlan__vlan_id=vlan_id).address_range
                self.assertEqual(actual, expected, f"{rack_name}/{function}")

    def test_duplicate_row_collapses_to_one_device(self) -> None:
        devices = NetworkDevice.objects.filter(rack__name="AMPRACK1", rack_slot=3)
        self.assertEqual(devices.count(), 1)
        self.assertEqual(devices.get().device_type.device_model.model, "IK-42")
        self.assertEqual(devices.get().device_type.name, "with Dante Card")

    def test_sd12_slot_offset_and_dmi_dante_card(self) -> None:
        console = NetworkDevice.objects.get(rack__name="CONSOLES", rack_slot=1)
        # Lowercase — ADR 0023 decision 8 (amended): hostname is normalised
        # on write.
        self.assertEqual(console.hostname, "sd12-test-1")
        self.assertEqual(console.device_type.device_model.model, "SD12")
        control_port = console.ports.get(slot_offset=0)
        engine_port = console.ports.get(slot_offset=1)
        self.assertEqual(control_port.address, addr(FN_CONTROL, "CONSOLES", 1))
        self.assertEqual(engine_port.address, addr(FN_CONTROL, "CONSOLES", 2))
        # No device sits at slot 2 — the "-Engine" row was absorbed into the
        # slot-1 device's span, not created as its own occupant.
        self.assertFalse(NetworkDevice.objects.filter(rack__name="CONSOLES", rack_slot=2).exists())

        # The DMI-DANTE card lands at CONSOLES slot 17, pinned rather than
        # derived (PLAN-prod-import.md §9) — the fixture's other CONSOLES
        # occupants (slots 1, 2, 4) are nowhere near it, so this also proves
        # the pin isn't just an accident of "highest slot + 1" here.
        card = NetworkDevice.objects.get(rack__name="CONSOLES", rack_slot=17)
        self.assertEqual(card.device_type.device_model.model, "DMI-DANTE")
        # ADR 0022 PR 3 — linked to the console whose row carried the
        # marker, read from the data rather than guessed (settled decision
        # 7). #41 stays open: no address changes from this link.
        self.assertTrue(card.device_type.is_add_in_card)
        self.assertEqual(card.host_id, console.pk)
        dp_port = card.ports.get(vlan__vlan_id=_VLAN_ID_BY_FUNCTION[FN_DANTE_PRIMARY])
        ds_port = card.ports.get(vlan__vlan_id=_VLAN_ID_BY_FUNCTION[FN_DANTE_SECONDARY])
        self.assertEqual(dp_port.address, addr(FN_DANTE_PRIMARY, "CONSOLES", 17))
        self.assertEqual(ds_port.address, addr(FN_DANTE_SECONDARY, "CONSOLES", 17))
        self.assertNotEqual(dp_port.address, "10.131.9.9")

    def test_secondary_switch_type_is_derived_correctly(self) -> None:
        primary = NetworkSwitchType.objects.get(
            manufacturer="Cisco", model="SG300-10MP", name="For 3xAmp Rack Primary"
        )
        secondary = NetworkSwitchType.objects.get(
            manufacturer="Cisco", model="SG300-10MP", name="For 3xAmp Rack Secondary"
        )
        self.assertEqual(secondary.port_count, primary.port_count)

        control_port = secondary.type_ports.get(port_number=1)
        self.assertEqual(control_port.profile.name, "Control Access")
        self.assertEqual(control_port.profile.native_vlan.vlan_id, 130)

        dante_port = secondary.type_ports.get(port_number=4)
        self.assertEqual(dante_port.profile.name, "Dante Secondary Access")
        self.assertEqual(dante_port.profile.native_vlan.vlan_id, 132)

        trunk_port = secondary.type_ports.get(port_number=7)
        self.assertEqual(trunk_port.profile.name, "Audio Trunk Secondary")
        self.assertEqual(trunk_port.profile.native_vlan.vlan_id, 132)
        allowed = {v.vlan_id for v in trunk_port.profile.allowed_vlans.all()}
        self.assertEqual(allowed, {130, 131, 137})

        switch = NetworkSwitch.objects.get(rack__name="AMPRACK1", rack_slot=2)
        self.assertEqual(switch.switch_type, secondary)

    def test_netgear_switch_deferred_not_created(self) -> None:
        self.assertFalse(NetworkSwitch.objects.filter(rack__name="W8LMTEST", rack_slot=1).exists())
        # The rack itself still imports, ranges and all.
        self.assertTrue(Rack.objects.filter(name="W8LMTEST").exists())

    def test_deliberate_address_drops(self) -> None:
        # §5.1: Lab.Gruppen has no Control interface at all.
        lm26 = NetworkDevice.objects.get(rack__name="W8LMTEST", rack_slot=2)
        control_id = _VLAN_ID_BY_FUNCTION[FN_CONTROL]
        dp_id = _VLAN_ID_BY_FUNCTION[FN_DANTE_PRIMARY]
        ds_id = _VLAN_ID_BY_FUNCTION[FN_DANTE_SECONDARY]
        self.assertFalse(lm26.ports.filter(vlan__vlan_id=control_id).exists())
        self.assertTrue(lm26.ports.filter(vlan__vlan_id=dp_id).exists())
        self.assertTrue(lm26.ports.filter(vlan__vlan_id=ds_id).exists())

        # §5.2: no Dante card fitted on these two.
        for slot in (3, 4):
            ik42 = NetworkDevice.objects.get(rack__name="XE300-1", rack_slot=slot)
            self.assertEqual(ik42.device_type.name, "without Dante Card")
            self.assertEqual(ik42.ports.count(), 1)
            self.assertTrue(ik42.ports.filter(vlan__vlan_id=control_id).exists())

        # §5.3: AVIO adapters are single-port, Dante Primary only.
        for slot in (1, 2):
            device = NetworkDevice.objects.get(rack__name="AVIO", rack_slot=slot)
            self.assertEqual(device.ports.count(), 1)
            self.assertEqual(device.ports.get().vlan.vlan_id, dp_id)

    def test_hostnames_from_device_description(self) -> None:
        # Lowercase — ADR 0023 decision 8 (amended).
        self.assertEqual(
            NetworkSwitch.objects.get(rack__name="AMPRACK1", rack_slot=1).hostname,
            "cisco sg300-10mp (for 3xamp rack primary)",
        )
        self.assertEqual(NetworkDevice.objects.get(rack__name="W8LMTEST", rack_slot=2).hostname, "lm26")
        self.assertEqual(
            NetworkDevice.objects.get(rack__name="AVIO", rack_slot=1).hostname, "mps-avio-radial-tx"
        )

    def test_import_applies_hostname_slugs_to_created_types(self) -> None:
        """ADR 0023 decision 10, amended (phase 18 PR 4) — every switch
        Type this import creates gets its ``hostname_slug`` from
        ``HOSTNAME_SLUGS`` (0 of which carried one on the live database).

        ADR 0026 PR 2 — the device side moved to ``NetworkDeviceModel``,
        seeded from ``DEVICE_MODEL_SLUGS`` by ``_create_device_models()``:
        one two-profile device model (``IK-42``) proves both profiles
        share the model's single value, not two independently-set ones
        that happen to agree.
        """
        primary = NetworkSwitchType.objects.get(
            manufacturer="Cisco", model="SG300-10MP", name="For 3xAmp Rack Primary"
        )
        secondary = NetworkSwitchType.objects.get(
            manufacturer="Cisco", model="SG300-10MP", name="For 3xAmp Rack Secondary"
        )
        self.assertEqual(primary.hostname_slug, "sg300-10mp")
        self.assertEqual(secondary.hostname_slug, "sg300-10mp")
        with_card = NetworkDeviceType.objects.get(
            device_model__manufacturer="Martin Audio", device_model__model="IK-42", name="with Dante Card"
        )
        without_card = NetworkDeviceType.objects.get(
            device_model__manufacturer="Martin Audio",
            device_model__model="IK-42",
            name="without Dante Card",
        )
        self.assertEqual(with_card.device_model_id, without_card.device_model_id)
        self.assertEqual(with_card.device_model.hostname_slug, "ik42")
        self.assertEqual(without_card.device_model.hostname_slug, "ik42")

    def test_dm7c_and_dm3_device_control_ports(self) -> None:
        # ADR 0027 retires ADR 0022's OPERATOR mechanism: the importer's
        # Device Control pre-pass folds each "-device-control" row into
        # its own console's fourth port, now an ordinary slot_offset=1
        # type port sitting one address above the host, for both consoles
        # alike.
        dm7c_host = NetworkDevice.objects.get(rack__name="CONSOLES", rack_slot=6)
        # Lowercase — ADR 0023 decision 8 (amended).
        self.assertEqual(dm7c_host.hostname, "dm7c-1")
        self.assertEqual(dm7c_host.device_type.name, "Default")
        self.assertEqual(dm7c_host.ports.count(), 4)
        dm7c_device_control = dm7c_host.ports.get(description="Device Control")
        assert dm7c_device_control.source_type_port is not None  # materialized ports always set this
        self.assertEqual(dm7c_device_control.slot_offset, 1)
        self.assertEqual(dm7c_device_control.address, addr(FN_DANTE_PRIMARY, "CONSOLES", 7))
        # Lowercase — the very property ADR 0022 believed it had protected
        # from case-sensitive assertions; ADR 0023 decision 8 (amended)
        # settles the casing this depends on.
        self.assertEqual(dm7c_device_control.hostname, "dm7c-1-device-control")
        # Slot 7 — the interface's own row in the sheet — releases entirely;
        # no device sits there at all (#42).
        self.assertFalse(NetworkDevice.objects.filter(rack__name="CONSOLES", rack_slot=7).exists())

        dm3_host = NetworkDevice.objects.get(rack__name="CONSOLES", rack_slot=8)
        self.assertEqual(dm3_host.hostname, "bej-dm3-1")
        dm3_device_control = dm3_host.ports.get(description="Device Control")
        assert dm3_device_control.source_type_port is not None  # materialized ports always set this
        self.assertEqual(dm3_device_control.slot_offset, 1)
        self.assertEqual(dm3_device_control.address, addr(FN_DANTE_PRIMARY, "CONSOLES", 9))
        self.assertFalse(NetworkDevice.objects.filter(rack__name="CONSOLES", rack_slot=9).exists())

    def test_verify_passes_against_a_correct_import(self) -> None:
        call_command("verify_prod_import", data_dir=str(self.data_dir))

    def test_verify_catches_a_wrong_address(self) -> None:
        port = NetworkDevicePort.objects.filter(
            device__rack__name="AVIO", device__rack_slot=1, address__isnull=False
        ).get()
        NetworkDevicePort.objects.filter(pk=port.pk).update(address="10.131.250.250")
        with self.assertRaises(CommandError):
            call_command("verify_prod_import", data_dir=str(self.data_dir))

    def test_verify_catches_a_corrupted_device_control_address(self) -> None:
        # ADR 0022 — corrupting *only* the Device Control port, leaving its
        # console's own Dante Primary port untouched, proves the two
        # VLAN-201 addresses are asserted independently (_device_address()
        # selects by description, not just (vlan, offset), which now
        # collides between them).
        dm7c_host = NetworkDevice.objects.get(rack__name="CONSOLES", rack_slot=6)
        device_control = dm7c_host.ports.get(description="Device Control")
        NetworkDevicePort.objects.filter(pk=device_control.pk).update(address="10.131.250.250")
        with self.assertRaises(CommandError):
            call_command("verify_prod_import", data_dir=str(self.data_dir))

    def test_verify_catches_a_stale_device_at_a_released_device_control_slot(self) -> None:
        # ADR 0027 (retiring ADR 0022's OPERATOR mechanism) — the Device
        # Control row's own slot (7, for the DM7C pair in this fixture) is
        # released; nothing should occupy it. A stale device left there
        # (e.g. a leftover row from before a re-import) must be caught as
        # an unexplained extra against the complete expected device-slot
        # set, not silently ignored because no CSV row names that key to
        # check "expected device" against.
        rack = Rack.objects.get(name="CONSOLES")
        device_type = NetworkDeviceType.objects.get(
            device_model__manufacturer="DiGiCo", device_model__model="SD9"
        )
        NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=device_type, rack=rack, rack_slot=7, hostname="stale", port_addressing="dhcp"
        )
        with self.assertRaises(CommandError):
            call_command("verify_prod_import", data_dir=str(self.data_dir))

    def test_verify_catches_a_wrong_extra_switch_address(self) -> None:
        # AMPRACK1's primary switch's Dante Secondary column is blank in the
        # sheet — the "extra" address §8 documents. Corrupting it must be
        # caught by deriving base + slot independently, not merely by
        # confirming *something* is there.
        address = NetworkSwitchAddress.objects.get(
            switch__rack__name="AMPRACK1",
            switch__rack_slot=1,
            vlan__vlan_id=_VLAN_ID_BY_FUNCTION[FN_DANTE_SECONDARY],
        )
        NetworkSwitchAddress.objects.filter(pk=address.pk).update(address="10.132.250.250")
        with self.assertRaises(CommandError):
            call_command("verify_prod_import", data_dir=str(self.data_dir))

    def test_verify_catches_a_device_reassigned_to_a_same_shaped_type(self) -> None:
        # An LM26 relabelled as an LM44 in the database is the same port
        # shape (Dante Primary + Dante Secondary, no Control) and would
        # materialize identical addresses — only the type identity differs.
        lm26 = NetworkDevice.objects.get(rack__name="W8LMTEST", rack_slot=2)
        lm44_type = NetworkDeviceType.objects.get(
            device_model__manufacturer="Lab.Gruppen", device_model__model="LM44", name="Redundant Mode"
        )
        NetworkDevice.objects.filter(pk=lm26.pk).update(device_type=lm44_type)
        with self.assertRaises(CommandError):
            call_command("verify_prod_import", data_dir=str(self.data_dir))

    def test_verify_catches_a_switch_reassigned_to_a_same_shaped_type(self) -> None:
        # The AMPRACK1 primary switch relabelled as the secondary type:
        # same manufacturer/model/port_count, wrong profile assignment.
        primary_switch = NetworkSwitch.objects.get(rack__name="AMPRACK1", rack_slot=1)
        secondary_type = NetworkSwitchType.objects.get(
            manufacturer="Cisco", model="SG300-10MP", name="For 3xAmp Rack Secondary"
        )
        NetworkSwitch.objects.filter(pk=primary_switch.pk).update(switch_type=secondary_type)
        with self.assertRaises(CommandError):
            call_command("verify_prod_import", data_dir=str(self.data_dir))

    def test_verify_catches_a_wrong_device_type_port(self) -> None:
        # A wrong port_type in the catalog is exactly what a slot_offset-only
        # check would miss.
        type_port = NetworkDeviceTypePort.objects.get(
            device_type__device_model__model="LM26", description="Dante Primary"
        )
        NetworkDeviceTypePort.objects.filter(pk=type_port.pk).update(port_type="1gbe_sfp")
        with self.assertRaises(CommandError):
            call_command("verify_prod_import", data_dir=str(self.data_dir))

    def test_verify_catches_a_corrupted_stored_port_count(self) -> None:
        # The actual NetworkDeviceTypePort rows are left untouched — only
        # the stored port_count field is wrong. A check that only counts
        # actual rows (matching the expected count either way) sails past
        # this; the stored field has to be compared too.
        device_type = NetworkDeviceType.objects.get(
            device_model__manufacturer="Lab.Gruppen", device_model__model="LM26", name="Redundant Mode"
        )
        NetworkDeviceType.objects.filter(pk=device_type.pk).update(port_count=device_type.port_count + 1)
        with self.assertRaises(CommandError):
            call_command("verify_prod_import", data_dir=str(self.data_dir))

    def test_verify_catches_a_dmi_dante_card_linked_to_the_wrong_console(self) -> None:
        # ADR 0022 PR 3 — the card's host link is a real expectation, not
        # merely "some device exists": re-pointing it at a different,
        # unrelated CONSOLES device must fail verification even though
        # every address is untouched.
        card = NetworkDevice.objects.get(rack__name="CONSOLES", rack_slot=17)
        wrong_host = NetworkDevice.objects.get(rack__name="CONSOLES", rack_slot=4)
        NetworkDevice.objects.filter(pk=card.pk).update(host=wrong_host.pk)
        with self.assertRaises(CommandError):
            call_command("verify_prod_import", data_dir=str(self.data_dir))

    # -- ADR 0023 decision 10: owner/location_slug seeding -----------------------------

    def test_owner_rows_seeded(self) -> None:
        mps = Owner.objects.get(slug="mps")
        self.assertEqual(mps.name, "MPS")
        bej = Owner.objects.get(slug="bej")
        self.assertEqual(bej.name, "BEJ")

    def test_every_rack_owner_is_mps(self) -> None:
        mps = Owner.objects.get(slug="mps")
        for rack_name, _offset in RACKS:
            self.assertEqual(Rack.objects.get(name=rack_name).owner_id, mps.pk, rack_name)

    def test_rack_location_slugs_match_the_slugify_plus_exceptions_rule(self) -> None:
        # AMPRACK1/W8LMTEST are the fabricated names settled decision 8
        # exists to keep working; XE300-1/CONSOLES are real production
        # names exercising the exceptions constant itself.
        expected = {
            "AMPRACK1": "amprack1",
            "XE300-1": "xe300-1",
            "AVIO": "avio",
            "W8LMTEST": "w8lmtest",
            "SHURE": "shure",
            "CONSOLES": None,
        }
        for rack_name, expected_slug in expected.items():
            self.assertEqual(Rack.objects.get(name=rack_name).location_slug, expected_slug, rack_name)

    def test_virtual_pools_get_no_location_slug(self) -> None:
        """CDD and CONTROL are pools rather than places, like CONSOLES, so they
        contribute no location component to a computed hostname (ADR 0023
        decision 2). They are in the source CSVs, so without explicit None
        entries in RACK_LOCATION_SLUG_EXCEPTIONS they slugify to "cdd" and
        "control" and gain a location they should not have. That is what
        happened in production: the operator cleared both by hand after the
        import, and verify_prod_import then failed on them.
        """
        for rack_name in ("CONSOLES", "CDD", "CONTROL"):
            rack = Rack.objects.filter(name=rack_name).first()
            if rack is None:
                continue  # not present in the synthetic fixture
            self.assertIsNone(rack.location_slug, rack_name)

    def test_no_device_or_switch_carries_owner_or_hostname_purpose_or_sequence(self) -> None:
        # ADR 0023 decision 10's negative half: the importer seeds only
        # Owner rows and Rack.owner/location_slug — never per-equipment.
        for device in NetworkDevice.objects.all():
            self.assertIsNone(device.owner_id, device)
            self.assertEqual(device.hostname_purpose, "", device)
            self.assertIsNone(device.hostname_sequence, device)
        for switch in NetworkSwitch.objects.all():
            self.assertIsNone(switch.owner_id, switch)
            self.assertEqual(switch.hostname_purpose, "", switch)
            self.assertIsNone(switch.hostname_sequence, switch)

    def test_verify_catches_a_rack_with_the_wrong_owner(self) -> None:
        bej = Owner.objects.get(slug="bej")
        Rack.objects.filter(name="AMPRACK1").update(owner=bej)
        with self.assertRaises(CommandError):
            call_command("verify_prod_import", data_dir=str(self.data_dir))

    def test_verify_catches_a_rack_with_the_wrong_location_slug(self) -> None:
        Rack.objects.filter(name="AMPRACK1").update(location_slug="wrong-slug")
        with self.assertRaises(CommandError):
            call_command("verify_prod_import", data_dir=str(self.data_dir))

    def test_verify_catches_equipment_that_unexpectedly_carries_an_owner(self) -> None:
        mps = Owner.objects.get(slug="mps")
        device = NetworkDevice.objects.get(rack__name="AVIO", rack_slot=1)
        NetworkDevice.objects.filter(pk=device.pk).update(owner=mps)
        with self.assertRaises(CommandError):
            call_command("verify_prod_import", data_dir=str(self.data_dir))


class ImportProdDataMalformedDmiDanteTests(TestCase):
    """The DMI-DANTE card's rack/slot is pinned (PLAN-prod-import.md §9),
    not derived — this is the "refuse rather than guess" half of that fix,
    exercised against its own one-off fixture rather than the shared one
    above, since it needs an extra, deliberately-malformed row.
    """

    def test_refuses_when_the_marker_appears_on_an_unexpected_rack(self) -> None:
        malformed_rows = [
            *ADDRESSING_ROWS,
            (
                "BOGUS-Control",
                "AMPRACK1",
                20,
                addr(FN_CONTROL, "AMPRACK1", 20),
                "10.131.9.9",
                "10.132.9.9",
                "",
            ),
            (
                "BOGUS-Engine",
                "AMPRACK1",
                21,
                addr(FN_CONTROL, "AMPRACK1", 21),
                "",
                "",
                "",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_fixture_csvs(data_dir, addressing_source_rows=malformed_rows)
            with self.assertRaises(CommandError):
                call_command("import_prod_data", data_dir=str(data_dir))

    def test_refuses_with_zero_markers(self) -> None:
        """ADR 0022 PR 3, settled decision 7/review note 10 — exactly one
        marker is *required*, not merely tolerated: the shared fixture's
        SD12-TEST-1-Control row is the only marker, so blanking its Dante
        Primary/Secondary columns (still a well-formed SD12 pair, just no
        longer carrying the DMI-DANTE artifact) leaves zero.
        """
        malformed_rows = [
            (row[0], row[1], row[2], row[3], "", "", row[6]) if row[0] == "SD12-TEST-1-Control" else row
            for row in ADDRESSING_ROWS
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_fixture_csvs(data_dir, addressing_source_rows=malformed_rows)
            with self.assertRaises(CommandError):
                call_command("import_prod_data", data_dir=str(data_dir))
            self.assertFalse(Rack.objects.exists())  # refused before any writes committed

    def test_refuses_with_two_markers_on_the_same_rack(self) -> None:
        """The bug settled decision 7 corrects: ``dmi_dante_racks`` used to
        be a ``set[str]`` of rack names, so two marker-bearing consoles in
        the same rack (``CONSOLES``, same as the shared fixture's existing
        SD12-TEST-1 marker) collapsed to one entry and passed the guard
        undetected. Collecting the marker *rows* instead catches this.
        """
        malformed_rows = [
            *ADDRESSING_ROWS,
            (
                "SD12-TEST-2-Control",
                "CONSOLES",
                20,
                addr(FN_CONTROL, "CONSOLES", 20),
                "10.131.9.19",
                "10.132.9.19",
                "",
            ),
            (
                "SD12-TEST-2-Engine",
                "CONSOLES",
                21,
                addr(FN_CONTROL, "CONSOLES", 21),
                "",
                "",
                "",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_fixture_csvs(data_dir, addressing_source_rows=malformed_rows)
            with self.assertRaises(CommandError):
                call_command("import_prod_data", data_dir=str(data_dir))
            self.assertFalse(Rack.objects.exists())  # refused before any writes committed


class ImportProdDataIllegalRackNameTests(TestCase):
    """ADR 0023 decision 10 / settled decision 8: a rack name that neither
    appears in ``RACK_LOCATION_SLUG_EXCEPTIONS`` nor slugifies to a legal
    DNS label is refused outright — not silently imported with a null
    ``location_slug``. Exercised against its own one-off fixture (an extra
    rack-offset row appended to the shared scheme) rather than the shared
    fixture above, which uses only names that are known to slugify legally
    (that's the whole point of settled decision 8 — see
    ``test_rack_location_slugs_match_the_slugify_plus_exceptions_rule``
    above for the proof that ordinary names, including the fabricated
    ``AMPRACK1``/``W8LMTEST``, are never refused).
    """

    def test_refuses_when_rack_name_neither_maps_nor_slugs_legally(self) -> None:
        bogus_rows = [*_calc_lookups_rows(), ["", "", "", "", "###", "999", "", "", "", ""]]
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            data_dir.mkdir(parents=True, exist_ok=True)
            _write_csv(data_dir / "MPS Audio Network Standards - IP Calc Lookups.csv", bogus_rows)
            _write_csv(data_dir / "MPS Audio Network Standards - Switch Ports.csv", _switch_ports_rows())
            _write_csv(data_dir / "MPS Audio Network Standards - IP Addressing mk2.csv", _addressing_rows())
            with self.assertRaisesRegex(CommandError, "RACK_LOCATION_SLUG_EXCEPTIONS"):
                call_command("import_prod_data", data_dir=str(data_dir))
            self.assertFalse(Rack.objects.exists())  # refused before any writes committed — even AMPRACK1


class ImportProdDataMalformedDeviceControlTests(TestCase):
    """ADR 0022's importer pre-pass refuses rather than guesses when a
    ``-device-control`` row's host can't be found, or is ambiguous —
    mirrors ``ImportProdDataMalformedDmiDanteTests``'s shape: its own
    one-off fixture, since each case needs a deliberately-malformed row
    the shared fixture doesn't have.
    """

    def test_refuses_an_unmatched_device_control_row(self) -> None:
        malformed_rows = [
            *ADDRESSING_ROWS,
            (
                "orphan-1-device-control",
                "AMPRACK1",
                20,
                "",
                addr(FN_DANTE_PRIMARY, "AMPRACK1", 20),
                "",
                "",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_fixture_csvs(data_dir, addressing_source_rows=malformed_rows)
            with self.assertRaises(CommandError):
                call_command("import_prod_data", data_dir=str(data_dir))

    def test_refuses_when_several_rows_match_the_device_control_stem(self) -> None:
        # Two rows sharing the stem "dup-host" in the same rack make the
        # host lookup ambiguous — the pre-pass must refuse rather than
        # guess which one the Device Control row belongs to.
        malformed_rows = [
            *ADDRESSING_ROWS,
            (
                "dup-host",
                "AMPRACK1",
                20,
                addr(FN_CONTROL, "AMPRACK1", 20),
                addr(FN_DANTE_PRIMARY, "AMPRACK1", 20),
                addr(FN_DANTE_SECONDARY, "AMPRACK1", 20),
                "",
            ),
            (
                "dup-host",
                "AMPRACK1",
                21,
                addr(FN_CONTROL, "AMPRACK1", 21),
                addr(FN_DANTE_PRIMARY, "AMPRACK1", 21),
                addr(FN_DANTE_SECONDARY, "AMPRACK1", 21),
                "",
            ),
            (
                "dup-host-device-control",
                "AMPRACK1",
                22,
                "",
                addr(FN_DANTE_PRIMARY, "AMPRACK1", 22),
                "",
                "",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_fixture_csvs(data_dir, addressing_source_rows=malformed_rows)
            with self.assertRaises(CommandError):
                call_command("import_prod_data", data_dir=str(data_dir))


class VerifyCatchesADeviceControlRowsExtraColumnsTests(TestCase):
    """ADR 0022, Codex review P2 — only ``dante_primary`` is ever read off
    a ``-device-control`` row (both by the importer's pre-pass and by the
    verifier's own independent check); a populated ``control``/``dante_
    secondary`` column would otherwise describe a real address that's
    silently discarded on both sides. Its own one-off fixture, since the
    shared fixture's rows are all well-formed and the importer's own
    pre-pass never reads the other two columns at all — nothing here
    corrupts the database after the fact, it corrupts the *sheet* the
    import is built from.
    """

    def test_verify_catches_a_populated_control_column_on_a_device_control_row(self) -> None:
        malformed_rows = [
            row if row[0] != "dm7c-1-device-control" else (row[0], row[1], row[2], "10.130.6.4", *row[4:])
            for row in ADDRESSING_ROWS
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_fixture_csvs(data_dir, addressing_source_rows=malformed_rows)
            call_command("import_prod_data", data_dir=str(data_dir))  # succeeds — the column is ignored
            with self.assertRaises(CommandError):
                call_command("verify_prod_import", data_dir=str(data_dir))


class ImportUserIdentityTests(TestCase):
    """Stage 1's dedicated audit identity (ADR 0004) — construct ->
    full_clean() -> save() like every other row this command writes, and
    refuses rather than silently adopting or disabling a pre-existing
    account under its username.
    """

    _tmpdir: tempfile.TemporaryDirectory
    data_dir: Path

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.data_dir = Path(cls._tmpdir.name)
        write_fixture_csvs(cls.data_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()
        super().tearDownClass()

    def test_creates_a_disabled_import_identity(self) -> None:
        call_command("import_prod_data", data_dir=str(self.data_dir))
        user = get_user_model().objects.get(username="prod-import")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_active)
        self.assertFalse(user.has_usable_password())

    def test_refuses_when_username_taken_by_a_real_account(self) -> None:
        get_user_model().objects.create_user(username="prod-import", password="a-real-password")
        with self.assertRaises(CommandError):
            call_command("import_prod_data", data_dir=str(self.data_dir))
        self.assertFalse(Rack.objects.exists())  # refused before any writes committed


class ParseDeviceModelsTests(TestCase):
    """Unit tests for ``_prod_import_csv.parse_device_models()`` directly —
    no management command, no database. Review council finding 5: a
    malformed/missing header used to be treated exactly like a well-formed
    one (silently discarding what was actually a data row), and a skipped
    body row (blank Manufacturer or Model) went uncounted and unwarned —
    invisible to both the importer and to ``verify_prod_import.py``'s
    independent check, since both read this same parser.
    """

    def test_valid_header_parses_normally(self) -> None:
        rows = parse_device_models(
            [
                ["Manufacturer", "Model", "Description", "Hostname Slug"],
                ["Martin Audio", "IK-42", "Amp Rack Processor", "ik42"],
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].manufacturer, "Martin Audio")

    def test_empty_file_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_device_models([])

    def test_malformed_header_raises_instead_of_silently_dropping_the_first_row(self) -> None:
        """The defect this guards against: an unconditional ``rows[1:]``
        would have treated this real data row as a header and discarded it.
        """
        with self.assertRaises(ValueError):
            parse_device_models([["Martin Audio", "IK-42", "Amp Rack Processor", "ik42"]])

    def test_header_matching_is_case_and_whitespace_insensitive(self) -> None:
        rows = parse_device_models(
            [
                [" manufacturer ", " MODEL ", "Description", "Hostname Slug"],
                ["Martin Audio", "IK-42", "", ""],
            ]
        )
        self.assertEqual(len(rows), 1)

    def test_blank_manufacturer_row_is_skipped_and_warned_with_its_line_number(self) -> None:
        with self.assertWarns(UserWarning) as ctx:
            rows = parse_device_models(
                [
                    ["Manufacturer", "Model", "Description", "Hostname Slug"],
                    ["", "IK-42", "", ""],
                    ["Martin Audio", "IK-42", "", ""],
                ]
            )
        self.assertEqual(len(rows), 1)
        # Line 2 — the header is line 1, the blank row is the first data row.
        self.assertIn("2", str(ctx.warning))

    def test_blank_model_row_is_skipped_and_warned(self) -> None:
        with self.assertWarns(UserWarning):
            rows = parse_device_models(
                [
                    ["Manufacturer", "Model", "Description", "Hostname Slug"],
                    ["Martin Audio", "", "", ""],
                ]
            )
        self.assertEqual(rows, [])

    def test_no_warning_when_nothing_is_skipped(self) -> None:
        import warnings as warnings_module

        with warnings_module.catch_warnings():
            warnings_module.simplefilter("error")
            # Must not raise — simplefilter("error") turns any warning
            # fired here into an exception.
            parse_device_models(
                [
                    ["Manufacturer", "Model", "Description", "Hostname Slug"],
                    ["Martin Audio", "IK-42", "", ""],
                ]
            )


class DeviceModelsCsvImportTests(TestCase):
    """ADR 0026 decision 5 — the Device Models CSV is optional. Every other
    test class in this module already exercises the "absent" branch (none
    of them write this file, and the importer must still succeed with
    every description blank — see ``write_fixture_csvs()``), so this class
    covers the "present" branch specifically.
    """

    def test_import_with_the_csv_lands_descriptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_fixture_csvs(data_dir)
            _write_csv(
                data_dir / "MPS Audio Network Standards - Device Models.csv",
                [
                    ["Manufacturer", "Model", "Description", "Hostname Slug"],
                    ["Martin Audio", "IK-42", "Amp Rack Processor", "ik42"],
                ],
            )
            call_command("import_prod_data", data_dir=str(data_dir))

        ik42 = NetworkDeviceModel.objects.get(manufacturer="Martin Audio", model="IK-42")
        self.assertEqual(ik42.description, "Amp Rack Processor")
        # A model the CSV doesn't mention still gets created, blank.
        other = NetworkDeviceModel.objects.exclude(pk=ik42.pk).first()
        assert other is not None
        self.assertEqual(other.description, "")

    def test_import_without_the_csv_succeeds_with_blank_descriptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_fixture_csvs(data_dir)
            call_command("import_prod_data", data_dir=str(data_dir))

        self.assertEqual(
            NetworkDeviceModel.objects.count(), NetworkDeviceModel.objects.filter(description="").count()
        )
        self.assertTrue(NetworkDeviceModel.objects.exists())

    def test_verify_catches_a_description_mismatch_against_the_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_fixture_csvs(data_dir)
            _write_csv(
                data_dir / "MPS Audio Network Standards - Device Models.csv",
                [
                    ["Manufacturer", "Model", "Description", "Hostname Slug"],
                    ["Martin Audio", "IK-42", "Amp Rack Processor", "ik42"],
                ],
            )
            call_command("import_prod_data", data_dir=str(data_dir))
            NetworkDeviceModel.objects.filter(manufacturer="Martin Audio", model="IK-42").update(
                description="Hand-edited after import"
            )
            with self.assertRaises(CommandError):
                call_command("verify_prod_import", data_dir=str(data_dir))


class DeviceModelsCsvHostnameSlugPrecedenceTests(TestCase):
    """ADR 0026 PR 2 settled decision B — the three CSV-vs-seed-catalog
    cases for ``hostname_slug``, each requiring its own fixture since they
    disagree on what the CSV even contains:

    | case | behaviour |
    |---|---|
    | CSV absent entirely | seed catalog (``DEVICE_MODEL_SLUGS``) supplies every slug |
    | CSV present, row missing for a model | seed catalog fills that model in |
    | CSV present, row present, cell blank | blank wins over the catalog |

    ``DEVICE_TYPES`` (the real production catalog, unlike the synthetic
    VLAN/addressing scheme the rest of this module builds) is what these
    fixtures import, so ``Martin Audio``/``IK-42`` and
    ``Lab.Gruppen``/``LM26`` are real catalog entries with known
    ``DEVICE_MODEL_SLUGS`` values (``"ik42"``/``"lm26"``) to assert against.
    """

    def test_csv_absent_falls_back_to_seed_catalog_for_every_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_fixture_csvs(data_dir)  # no Device Models CSV written
            call_command("import_prod_data", data_dir=str(data_dir))

        for (manufacturer, model), expected_slug in DEVICE_MODEL_SLUGS.items():
            device_model = NetworkDeviceModel.objects.get(manufacturer=manufacturer, model=model)
            self.assertEqual(device_model.hostname_slug, expected_slug, f"{manufacturer}/{model}")

    def test_csv_present_but_row_missing_falls_back_to_seed_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_fixture_csvs(data_dir)
            # Names a row for IK-42 only — every other model, including
            # Lab.Gruppen/LM26, has no row in this CSV at all.
            _write_csv(
                data_dir / "MPS Audio Network Standards - Device Models.csv",
                [
                    ["Manufacturer", "Model", "Description", "Hostname Slug"],
                    ["Martin Audio", "IK-42", "", "ik42"],
                ],
            )
            call_command("import_prod_data", data_dir=str(data_dir))

        lm26 = NetworkDeviceModel.objects.get(manufacturer="Lab.Gruppen", model="LM26")
        self.assertEqual(lm26.hostname_slug, DEVICE_MODEL_SLUGS[("Lab.Gruppen", "LM26")])

    def test_csv_row_present_with_blank_cell_wins_over_the_seed_catalog(self) -> None:
        """The decisive case (review note 5): an explicit blank cell means
        "no hostname for this model" and must not be silently overwritten
        by ``DEVICE_MODEL_SLUGS["Lab.Gruppen", "LM26"] == "lm26"``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_fixture_csvs(data_dir)
            _write_csv(
                data_dir / "MPS Audio Network Standards - Device Models.csv",
                [
                    ["Manufacturer", "Model", "Description", "Hostname Slug"],
                    ["Lab.Gruppen", "LM26", "", ""],
                ],
            )
            call_command("import_prod_data", data_dir=str(data_dir))

        lm26 = NetworkDeviceModel.objects.get(manufacturer="Lab.Gruppen", model="LM26")
        self.assertEqual(lm26.hostname_slug, "")

    def test_csv_row_present_with_a_non_blank_cell_overrides_the_seed_catalog(self) -> None:
        """The CSV wins even when it disagrees with the catalog, not just
        when it agrees or is blank.
        """
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_fixture_csvs(data_dir)
            _write_csv(
                data_dir / "MPS Audio Network Standards - Device Models.csv",
                [
                    ["Manufacturer", "Model", "Description", "Hostname Slug"],
                    ["Martin Audio", "IK-42", "", "operator-override"],
                ],
            )
            call_command("import_prod_data", data_dir=str(data_dir))

        ik42 = NetworkDeviceModel.objects.get(manufacturer="Martin Audio", model="IK-42")
        self.assertNotEqual(DEVICE_MODEL_SLUGS[("Martin Audio", "IK-42")], "operator-override")
        self.assertEqual(ik42.hostname_slug, "operator-override")

    def test_verify_catches_a_hostname_slug_mismatch_against_the_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_fixture_csvs(data_dir)
            _write_csv(
                data_dir / "MPS Audio Network Standards - Device Models.csv",
                [
                    ["Manufacturer", "Model", "Description", "Hostname Slug"],
                    ["Martin Audio", "IK-42", "", "ik42"],
                ],
            )
            call_command("import_prod_data", data_dir=str(data_dir))
            NetworkDeviceModel.objects.filter(manufacturer="Martin Audio", model="IK-42").update(
                hostname_slug="hand-edited"
            )
            with self.assertRaises(CommandError):
                call_command("verify_prod_import", data_dir=str(data_dir))

    def test_verify_passes_when_blank_cell_matches_blank_database_value(self) -> None:
        """The blank-wins case, verified independently — proves the
        verifier's own oracle agrees with the importer's precedence
        rather than expecting the catalog value.
        """
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_fixture_csvs(data_dir)
            _write_csv(
                data_dir / "MPS Audio Network Standards - Device Models.csv",
                [
                    ["Manufacturer", "Model", "Description", "Hostname Slug"],
                    ["Lab.Gruppen", "LM26", "", ""],
                ],
            )
            call_command("import_prod_data", data_dir=str(data_dir))
            call_command("verify_prod_import", data_dir=str(data_dir))  # must not raise

    def test_verify_does_not_trip_on_an_uppercase_csv_cell(self) -> None:
        """Review council finding 6 — ``NetworkDeviceModel.clean_fields()``
        lowercases ``hostname_slug`` on import while ``parse_device_models()``
        only strips the cell, so an uppercase CSV value imports correctly
        (lowercased) but would trip a spurious mismatch if the verifier
        compared it against its own raw, unlowercased self.
        """
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_fixture_csvs(data_dir)
            _write_csv(
                data_dir / "MPS Audio Network Standards - Device Models.csv",
                [
                    ["Manufacturer", "Model", "Description", "Hostname Slug"],
                    ["Martin Audio", "IK-42", "", "IK42"],
                ],
            )
            call_command("import_prod_data", data_dir=str(data_dir))
            ik42 = NetworkDeviceModel.objects.get(manufacturer="Martin Audio", model="IK-42")
            self.assertEqual(ik42.hostname_slug, "ik42")  # lowercased on import
            call_command("verify_prod_import", data_dir=str(data_dir))  # must not raise


class HostnameSlugsConstantTests(TestCase):
    """PLAN-hostname-computation.md PR 4 "Seeding" — ``HOSTNAME_SLUGS``
    covers every ``(manufacturer, model)`` the importer's own catalog
    actually creates (a subset check, code review round 2, finding 4b —
    the importer pairs must be **covered**, but the constant is allowed
    to carry more than that; the migration's own separate copy does, by
    design, for a live-only pair the current importer no longer creates
    at all), and the importer's and verifier's independently re-declared
    copies (neither imports the other, on purpose) agree with each other
    exactly (still equality — the two are meant to describe the same
    rebuild-time catalog).

    ADR 0026 PR 2, settled decision A — ``HOSTNAME_SLUGS`` (importer and
    verifier) is switch-only now; the device side moved to
    ``import_prod_data.DEVICE_MODEL_SLUGS``, which has no verifier-side
    counterpart at all (the verifier checks the CSV against
    ``NetworkDeviceModel.hostname_slug`` directly instead — see
    ``DeviceModelsCsvImportTests`` for that coverage). Every test below
    that used to check one combined constant now checks the two
    separately.
    """

    def test_every_importer_switch_pair_has_a_hostname_slugs_entry(self) -> None:
        """A subset check (code review finding 2), not equality — the
        importer's own HOSTNAME_SLUGS must cover every switch pair the
        importer actually creates, but is allowed to carry no more than
        that (the migration's *separate* copy carries one additional,
        live-only pair — ("Cisco", "SG350-10P") — that the current
        importer catalog no longer creates at all; equality here would
        force that pair into a constant where it would be dead weight).
        """
        switch_pairs = {
            (manufacturer, model) for manufacturer, model, _name in PRIMARY_SWITCH_TABLES.values()
        } | {(manufacturer, model) for manufacturer, model, _name in SECONDARY_DERIVED_TABLES.values()}
        self.assertTrue(
            switch_pairs <= set(IMPORTER_HOSTNAME_SLUGS),
            switch_pairs - set(IMPORTER_HOSTNAME_SLUGS),
        )

    def test_every_importer_device_pair_has_a_device_model_slugs_entry(self) -> None:
        """Same subset shape as the switch-side test above, but against
        ``DEVICE_MODEL_SLUGS`` — the seed catalog ``_create_device_models()``
        falls back to (ADR 0026 PR 2 settled decision B) when the Device
        Models CSV supplies no row for a model.
        """
        device_pairs = {(spec.manufacturer, spec.model) for spec in DEVICE_TYPES}
        self.assertTrue(
            device_pairs <= set(DEVICE_MODEL_SLUGS),
            device_pairs - set(DEVICE_MODEL_SLUGS),
        )

    def test_importer_and_verifier_copies_agree(self) -> None:
        self.assertEqual(IMPORTER_HOSTNAME_SLUGS, VERIFIER_HOSTNAME_SLUGS)

    def test_migration_copy_covers_every_importer_switch_pair(self) -> None:
        """Not asked for by either review round, but noted and left to
        judgement: relaxing the importer-catalog check (above) to a
        subset removed the last structural pressure keeping the
        migration's *own* separate copy (0018_seed_hostname_slugs.py,
        deliberately not shared code — see that module's own docstring)
        in step with the importer's. This restores it cheaply: every
        pair the importer creates must also appear, with the same slug,
        in the migration's copy — which is free to carry more (the
        live-only ("Cisco", "SG350-10P") pair, currently), just not less.
        """
        self.assertTrue(
            set(IMPORTER_HOSTNAME_SLUGS) <= set(MIGRATION_HOSTNAME_SLUGS),
            set(IMPORTER_HOSTNAME_SLUGS) - set(MIGRATION_HOSTNAME_SLUGS),
        )
        mismatched = {
            pair: (slug, MIGRATION_HOSTNAME_SLUGS[pair])
            for pair, slug in IMPORTER_HOSTNAME_SLUGS.items()
            if MIGRATION_HOSTNAME_SLUGS.get(pair) != slug
        }
        self.assertEqual(mismatched, {})

    def test_migration_copy_covers_every_device_model_slugs_pair(self) -> None:
        """Same restoration as the switch-side test above, for
        ``DEVICE_MODEL_SLUGS`` — migration 0018's frozen copy predates the
        PR 2 split and still carries both halves undifferentiated, so it
        must still cover (with matching values) everything the device
        side's seed catalog now carries on its own.
        """
        self.assertTrue(
            set(DEVICE_MODEL_SLUGS) <= set(MIGRATION_HOSTNAME_SLUGS),
            set(DEVICE_MODEL_SLUGS) - set(MIGRATION_HOSTNAME_SLUGS),
        )
        mismatched = {
            pair: (slug, MIGRATION_HOSTNAME_SLUGS[pair])
            for pair, slug in DEVICE_MODEL_SLUGS.items()
            if MIGRATION_HOSTNAME_SLUGS.get(pair) != slug
        }
        self.assertEqual(mismatched, {})

    def test_amphenol_entries_are_corrected_not_the_live_typo(self) -> None:
        """Settled with Mike: the live ``rdj…`` slugs against models
        spelled ``RJD…`` are a typo. The constant carries the correction,
        not the typo. Amphenol is a device manufacturer, so this checks
        ``DEVICE_MODEL_SLUGS`` (ADR 0026 PR 2), not ``HOSTNAME_SLUGS``.
        """
        for model in ("RJD1212-0050", "RJD2203-0050", "RJD32A3-0050", "RJD32U1-0050"):
            slug = DEVICE_MODEL_SLUGS[("Amphenol", model)]
            self.assertTrue(slug.startswith("rjd"), f"{model}: {slug!r} does not start with 'rjd'")

    def test_amphenol_absent_from_both_live_hostname_slugs_but_present_in_frozen_migration(self) -> None:
        """ADR 0026 PR 2 settled decision A, review note 14 — after the
        switch-only conversion, neither live ``HOSTNAME_SLUGS`` copy
        (importer or verifier) names an Amphenol pair at all; migration
        0018's frozen copy is history and keeps all four, corrected.
        """
        amphenol_models = ("RJD1212-0050", "RJD2203-0050", "RJD32A3-0050", "RJD32U1-0050")
        for model in amphenol_models:
            self.assertNotIn(("Amphenol", model), IMPORTER_HOSTNAME_SLUGS)
            self.assertNotIn(("Amphenol", model), VERIFIER_HOSTNAME_SLUGS)
            slug = MIGRATION_HOSTNAME_SLUGS[("Amphenol", model)]
            self.assertTrue(slug.startswith("rjd"), f"{model}: {slug!r} does not start with 'rjd'")
