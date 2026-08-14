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
import ipaddress
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from .management.commands.verify_prod_import import _check_cross_vlan_alignment, _Findings
from .models import (
    VLAN,
    NetworkDevice,
    NetworkDevicePort,
    NetworkDeviceType,
    NetworkDeviceTypePort,
    NetworkSwitch,
    NetworkSwitchAddress,
    NetworkSwitchType,
    PortAddressSource,
    Rack,
    RackVlanRange,
)

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
    # CONSOLES: a DM7C console + its Device Control row, sitting BELOW its
    # host (ADR 0022 — production has the DM7C's interface one address
    # below its console, e.g. 10.201.6.4 vs .5).
    (
        "dm7c-1-device-control",
        "CONSOLES",
        5,
        "",
        addr(FN_DANTE_PRIMARY, "CONSOLES", 5),
        "",
        "Only on Dante Primary for controlling snakes",
    ),
    (
        "DM7C-1",
        "CONSOLES",
        6,
        addr(FN_CONTROL, "CONSOLES", 6),
        addr(FN_DANTE_PRIMARY, "CONSOLES", 6),
        addr(FN_DANTE_SECONDARY, "CONSOLES", 6),
        "",
    ),
    # CONSOLES: a DM3 console + its Device Control row, sitting ABOVE its
    # host (production has the DM3's interface one address above its
    # console) — the opposite direction from the DM7C pair above, proving
    # the importer's Device Control pre-pass isn't
    # slot-adjacency/direction-specific (ADR 0022).
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
        self.assertEqual(devices.get().device_type.model, "IK-42")
        self.assertEqual(devices.get().device_type.name, "with Dante Card")

    def test_sd12_slot_offset_and_dmi_dante_card(self) -> None:
        console = NetworkDevice.objects.get(rack__name="CONSOLES", rack_slot=1)
        self.assertEqual(console.hostname, "SD12-TEST-1")
        self.assertEqual(console.device_type.model, "SD12")
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
        self.assertEqual(card.device_type.model, "DMI-DANTE")
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
        self.assertEqual(
            NetworkSwitch.objects.get(rack__name="AMPRACK1", rack_slot=1).hostname,
            "Cisco SG300-10MP (For 3xAmp Rack Primary)",
        )
        self.assertEqual(NetworkDevice.objects.get(rack__name="W8LMTEST", rack_slot=2).hostname, "LM26")
        self.assertEqual(
            NetworkDevice.objects.get(rack__name="AVIO", rack_slot=1).hostname, "mps-avio-radial-tx"
        )

    def test_dm7c_and_dm3_device_control_ports(self) -> None:
        # ADR 0022: the importer's Device Control pre-pass folds each
        # "-device-control" row's address into its own console's fourth,
        # OPERATOR-sourced port rather than materializing it as a second
        # device — in both directions, DM7C's sits below its host, DM3's
        # sits above.
        dm7c_host = NetworkDevice.objects.get(rack__name="CONSOLES", rack_slot=6)
        self.assertEqual(dm7c_host.hostname, "DM7C-1")
        self.assertEqual(dm7c_host.device_type.name, "Default")
        self.assertEqual(dm7c_host.ports.count(), 4)
        dm7c_device_control = dm7c_host.ports.get(description="Device Control")
        assert dm7c_device_control.source_type_port is not None  # materialized ports always set this
        self.assertEqual(dm7c_device_control.source_type_port.address_source, PortAddressSource.OPERATOR)
        self.assertEqual(dm7c_device_control.address, addr(FN_DANTE_PRIMARY, "CONSOLES", 5))
        self.assertEqual(dm7c_device_control.hostname, "DM7C-1-device-control")
        # Slot 5 — the interface's own row in the sheet — releases entirely;
        # no device sits there at all (#42).
        self.assertFalse(NetworkDevice.objects.filter(rack__name="CONSOLES", rack_slot=5).exists())

        dm3_host = NetworkDevice.objects.get(rack__name="CONSOLES", rack_slot=8)
        self.assertEqual(dm3_host.hostname, "bej-dm3-1")
        dm3_device_control = dm3_host.ports.get(description="Device Control")
        assert dm3_device_control.source_type_port is not None  # materialized ports always set this
        self.assertEqual(dm3_device_control.source_type_port.address_source, PortAddressSource.OPERATOR)
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
        # ADR 0022 — the Device Control row's own slot (5, for the DM7C
        # pair in this fixture) is released; nothing should occupy it. A
        # stale device left there (e.g. a leftover row from before a
        # re-import) must be caught as an unexplained extra against the
        # complete expected device-slot set, not silently ignored because
        # no CSV row names that key to check "expected device" against.
        rack = Rack.objects.get(name="CONSOLES")
        device_type = NetworkDeviceType.objects.get(manufacturer="DiGiCo", model="SD9")
        NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=device_type, rack=rack, rack_slot=5, hostname="stale", port_addressing="dhcp"
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
            manufacturer="Lab.Gruppen", model="LM44", name="Redundant Mode"
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
        type_port = NetworkDeviceTypePort.objects.get(device_type__model="LM26", description="Dante Primary")
        NetworkDeviceTypePort.objects.filter(pk=type_port.pk).update(port_type="1gbe_sfp")
        with self.assertRaises(CommandError):
            call_command("verify_prod_import", data_dir=str(self.data_dir))

    def test_verify_catches_a_corrupted_stored_port_count(self) -> None:
        # The actual NetworkDeviceTypePort rows are left untouched — only
        # the stored port_count field is wrong. A check that only counts
        # actual rows (matching the expected count either way) sails past
        # this; the stored field has to be compared too.
        device_type = NetworkDeviceType.objects.get(
            manufacturer="Lab.Gruppen", model="LM26", name="Redundant Mode"
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


class CrossVlanAlignmentExemptionTests(TestCase):
    """ADR 0022, review note 14 — ``_check_cross_vlan_alignment()``'s
    ``OPERATOR`` exemption is bounded to the ``OPERATOR`` port itself, not
    to the whole device it sits on, and keys off ``address_source``, not
    off a missing ``source_type_port``. Exercised directly against
    ``_check_cross_vlan_alignment()`` rather than through a full CSV
    import — the scenario this guards against (an over-broad exemption)
    needs a ``source_type_port=None`` port, which no CSV row can express;
    ``_materialize_ports()`` always sets it.
    """

    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(username="alignment-test")
        # No dhcp_range — the /27 rack ranges below (.32-.63) need to be
        # clear of it, and the DHCP range is optional (unlike
        # import_prod_data.py's real VLANs, this fixture has no reason to
        # carry one).
        self.vlan_a = VLAN(name="Alignment A", vlan_id=901, subnet="10.90.0.0/24", created_by=self.user)
        self.vlan_a.full_clean()
        self.vlan_a.save()
        self.vlan_b = VLAN(name="Alignment B", vlan_id=902, subnet="10.91.0.0/24", created_by=self.user)
        self.vlan_b.full_clean()
        self.vlan_b.save()
        self.rack = Rack(name="Alignment Rack", slot_count=10, created_by=self.user)
        self.rack.full_clean()
        self.rack.save()
        for vlan, base in ((self.vlan_a, "10.90.0.32/27"), (self.vlan_b, "10.91.0.32/27")):
            rng = RackVlanRange(rack=self.rack, vlan=vlan, address_range=base, created_by=self.user)
            rng.full_clean()
            rng.save()
        self.device_type = NetworkDeviceType(
            manufacturer="Test", model="Alignment", name="Default", port_count=3, created_by=self.user
        )
        self.device_type.full_clean()
        self.device_type.save()
        for description, vlan, address_source in (
            ("Primary A", self.vlan_a, PortAddressSource.SLOT),
            ("Primary B", self.vlan_b, PortAddressSource.SLOT),
            ("Aux", self.vlan_a, PortAddressSource.OPERATOR),
        ):
            type_port = NetworkDeviceTypePort(
                device_type=self.device_type,
                description=description,
                port_type="1gbe_rj45",
                vlan=vlan,
                slot_offset=0,
                address_source=address_source,
                created_by=self.user,
            )
            type_port.full_clean()
            type_port.save()
        self.device = NetworkDevice(  # type: ignore[misc]
            device_type=self.device_type,
            rack=self.rack,
            rack_slot=1,
            hostname="alignment-1",
            created_by=self.user,
            operator_addresses={"Aux": "10.90.0.40"},
        )
        self.device.full_clean()
        self.device.save()

    def test_operator_port_exempt_aligned_slot_ports_pass(self) -> None:
        # The OPERATOR "Aux" port's address (10.90.0.99) shares Primary A's
        # VLAN but bears no derivable relationship to it — if it weren't
        # exempt, this would already fail on the happy path.
        findings = _Findings()
        _check_cross_vlan_alignment(findings)
        self.assertEqual(findings.mismatches, [])

    def test_corrupted_slot_port_still_fails_alignment(self) -> None:
        port = self.device.ports.get(description="Primary A")
        NetworkDevicePort.objects.filter(pk=port.pk).update(address="10.90.0.250")
        findings = _Findings()
        _check_cross_vlan_alignment(findings)
        self.assertTrue(any(m.check == "cross_vlan_alignment" for m in findings.mismatches))

    def test_corrupted_slot_port_with_no_source_type_port_still_fails_alignment(self) -> None:
        # The exemption keys off address_source == OPERATOR read through
        # source_type_port, not off source_type_port being unset — an
        # implementation that exempted every source_type_port=None port
        # (mistaking "no type port on record" for "operator-addressed")
        # would pass this the same as the happy path.
        port = self.device.ports.get(description="Primary A")
        NetworkDevicePort.objects.filter(pk=port.pk).update(address="10.90.0.250", source_type_port=None)
        findings = _Findings()
        _check_cross_vlan_alignment(findings)
        self.assertTrue(any(m.check == "cross_vlan_alignment" for m in findings.mismatches))


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
