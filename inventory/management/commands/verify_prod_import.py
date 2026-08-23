"""Independent verification of a completed ``import_prod_data`` run.

Implements ``PLAN-prod-import.md``'s ``## Verification`` section: the
export is treated as its own test oracle, so every check here re-derives
what *should* be in the database straight from the three source CSVs and a
handful of documented, source-independent rules (the three spurious-address
drops, the SD12 collapse, the derived secondary switch profile) — never by
calling into ``import_prod_data.py``. That module is imported nowhere in
this file, on purpose: "if the secondary-switch port matrix check and the
importer both call the same helper, the check proves nothing"
(PLAN-prod-import.md). Only the neutral CSV-parsing helpers in
``_prod_import_csv.py`` (pure I/O, no domain judgement) and the pure
arithmetic in ``inventory.suggestions`` (the documented, already-validated
``base + slot`` formula — PROD-DATA-ANALYSIS.md §2.1) are shared.

Exits non-zero (``CommandError``) if any check fails, printing every
mismatch found rather than stopping at the first — "close enough" is not
the point of this command.
"""

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Q

from inventory.models import (
    VLAN,
    NetworkDevice,
    NetworkDeviceModel,
    NetworkDevicePort,
    NetworkDeviceType,
    NetworkDeviceTypePort,
    NetworkSwitch,
    NetworkSwitchAddress,
    NetworkSwitchType,
    Owner,
    PortAddressSource,
    Rack,
)
from inventory.suggestions import suggest_slot_address

from ._prod_import_csv import (
    AddressingRow,
    DeviceModelRow,
    SwitchPortRow,
    SwitchPortTable,
    parse_addressing_rows,
    parse_device_models,
    parse_rack_offsets,
    parse_switch_port_tables,
    parse_vlan_table,
    read_csv_rows,
)

ADDRESSING_CSV_NAME = "MPS Audio Network Standards - IP Addressing mk2.csv"
CALC_LOOKUPS_CSV_NAME = "MPS Audio Network Standards - IP Calc Lookups.csv"
SWITCH_PORTS_CSV_NAME = "MPS Audio Network Standards - Switch Ports.csv"
#: Optional, like the importer's own copy — checked only when present.
DEVICE_MODELS_CSV_NAME = "MPS Audio Network Standards - Device Models.csv"

FN_CONTROL = "Audio Control"
FN_DANTE_PRIMARY = "Dante Primary"
FN_DANTE_SECONDARY = "Dante Secondary"

#: VLAN function name -> the device port ``description`` this dataset's
#: catalog always uses for it — every device type here names its ports
#: this way (SD12's "Engine" and the consoles' "Device Control" are the
#: only exceptions, both handled by their own callers, not this map).
FUNCTION_TO_PORT_DESCRIPTION: dict[str, str] = {
    FN_CONTROL: "Control",
    FN_DANTE_PRIMARY: "Dante Primary",
    FN_DANTE_SECONDARY: "Dante Secondary",
}

MANUAL_RANGE_RACKS = frozenset({"SHURE", "CONSOLES"})
DHCP_SERVER_RACKS = frozenset({"FOH Drive #1", "FOH Drive #2"})
NETGEAR_DESCRIPTION = "Netgear Managed Switch (For W8LM Rack)"

#: Re-declared independently of ``import_prod_data.OWNER_ROWS`` — same
#: source knowledge (ADR 0023 decision 10), separately typed in.
OWNER_ROWS: tuple[tuple[str, str], ...] = (("mps", "MPS"), ("bej", "BEJ"))

#: Re-declared independently of ``import_prod_data.RACK_LOCATION_SLUG_
#: EXCEPTIONS`` — this module's own docstring forbids importing that
#: module, on the grounds that a check sharing the importer's helper proves
#: nothing.
RACK_LOCATION_SLUG_EXCEPTIONS: dict[str, str | None] = {
    # XE300 is a Martin Audio speaker model, and this rack holds the amps that
    # drive them — so the model number is the meaningful part, not noise to be
    # abbreviated away. "xe1" also loses the distinction from a future XE500.
    # (Operator decision, 2026-08-18; "xe3001" reads as "X E three thousand
    # and one", which is why the digits are not simply run together.)
    "XE300-1": "xe300-1",
    "XE300-2": "xe300-2",
    "FOH Drive #1": "foh1",
    "FOH Drive #2": "foh2",
    # Not in the source CSVs yet, so the importer never reaches it today —
    # carried so that adding it to the sheet cannot silently produce
    # "foh-drive-3" instead of matching its two siblings.
    "FOH Drive #3": "foh3",
    # Virtual pools rather than places, so they contribute no location
    # component to a computed hostname (ADR 0023 decision 2). Without explicit
    # None entries these slugify to "cdd"/"control" and gain a location they
    # should not have.
    "CONSOLES": None,
    "CDD": None,
    "CONTROL": None,
}

#: Re-declared independently of ``import_prod_data.HOSTNAME_SLUGS`` — this
#: module's own docstring forbids importing that module, on the grounds
#: that a check sharing the importer's helper proves nothing. See that
#: module's own constant for the citation of where each value comes from.
HOSTNAME_SLUGS: dict[tuple[str, str], str] = {
    ("Allen & Heath", "SQ-5"): "sq5",
    ("Amphenol", "RJD1212-0050"): "rjd1212",
    ("Amphenol", "RJD2203-0050"): "rjd2203",
    ("Amphenol", "RJD32A3-0050"): "rjd32a3",
    ("Amphenol", "RJD32U1-0050"): "rjd32u1",
    ("Audinate", "AVIO-AO2"): "avioao2",
    ("DiGiCo", "DMI-DANTE"): "dmidante",
    ("DiGiCo", "SD11"): "sd11",
    ("DiGiCo", "SD12"): "sd12",
    ("DiGiCo", "SD9"): "sd9",
    ("Lab.Gruppen", "LM26"): "lm26",
    ("Lab.Gruppen", "LM44"): "lm44",
    ("Lab.Gruppen", "PLM20000Q"): "plm20q",
    ("Martin Audio", "IK-42"): "ik42",
    ("Martin Audio", "IK-81"): "ik81",
    ("Neutrik", "NA2-DLINE"): "na2dline",
    ("Radial", "DiNET DAN-RX"): "danrx",
    ("Radial", "DiNET DAN-TX"): "dantx",
    ("Yamaha", "DM3"): "dm3",
    ("Yamaha", "DM7-EX"): "dm7ex",
    ("Yamaha", "DM7C"): "dm7c",
    ("Yamaha", "Tio1608-D2"): "tio1608d2",
    ("Cisco", "SG300-10MP"): "sg300-10mp",
    ("Cisco", "SG300-26P"): "sg300-26p",
    ("Cisco", "SG350-10"): "sg350-10",
    ("TP-Link", "TL-SG108E"): "tl-sg108e",
}

_RACK_SLUG_COLLAPSE_RE = re.compile(r"[^a-z0-9]+")


def _expected_location_slug(rack_name: str) -> str | None:
    """Re-derives ADR 0023 decision 10's slugify-plus-exceptions rule
    independently of ``import_prod_data._slugify_rack_name``/
    ``_rack_location_slug`` — same mechanical rule, separately typed in.
    """
    if rack_name in RACK_LOCATION_SLUG_EXCEPTIONS:
        return RACK_LOCATION_SLUG_EXCEPTIONS[rack_name]
    return _RACK_SLUG_COLLAPSE_RE.sub("-", rack_name.lower()).strip("-")


#: Pinned, not derived — re-declared independently of
#: ``import_prod_data.DMI_DANTE_RACK``/``DMI_DANTE_SLOT``. PLAN-prod-import.md
#: §9 pins the card to CONSOLES slot 17 deliberately, specifically so the
#: resulting addresses are predictable enough to program into physical
#: hardware — a `max(slot) + 1` derivation on either side of the import
#: would silently follow a later or malformed row instead of catching it.
DMI_DANTE_RACK = "CONSOLES"
DMI_DANTE_SLOT = 17

#: Re-declared independently of ``import_prod_data.SWITCH_DESCRIPTION_TO_TABLE``
#: — same source knowledge, separately typed in, so a mistake in one isn't
#: silently validated by the other.
SWITCH_DESCRIPTIONS = frozenset(
    {
        "Cisco SG300-10MP (For 3xAmp Rack Primary)",
        "Cisco SG300-10MP (For 3xAmp Rack Secondary)",
        "Cisco SG350-10 (For 2xAmp Rack Primary)",
        "Cisco SG350-10 (For 2xAmp Rack Secondary)",
        "Cisco SG300-10MP (For Drive Rack Primary)",
        "Spare SG300-26P",
    }
)
_TLSG108E_RE = re.compile(r"^mps-tlsg108e-\d+$")

#: Descriptions whose Control-column address is spurious — Lab.Gruppen
#: products have no control interface (PROD-DATA-ANALYSIS.md §5.1).
NO_CONTROL_PORT_DESCRIPTIONS = frozenset({"LM26", "LM44", "PLM20000Q"})

#: ``(rack, slot)`` pairs whose Dante addresses are spurious — no Dante
#: card fitted (§5.2).
NO_DANTE_CARD_SLOTS = frozenset({("XE300-1", 3), ("XE300-1", 4)})

#: The five profile identities PLAN-prod-import.md §3 settles on, as
#: ``(port_mode, native_function)`` pairs sufficient to name one uniquely
#: among this dataset's ports.
PROFILE_CONTROL_ACCESS = "Control Access"
PROFILE_DANTE_PRIMARY_ACCESS = "Dante Primary Access"
PROFILE_DANTE_SECONDARY_ACCESS = "Dante Secondary Access"
PROFILE_AUDIO_TRUNK = "Audio Trunk"
PROFILE_AUDIO_TRUNK_SECONDARY = "Audio Trunk Secondary"

PRIMARY_TABLE_TO_TYPE_NAME = {
    "Cisco SG300-10MP (For 3xAmp Rack Primary)": "For 3xAmp Rack Primary",
    "Cisco SG350-10 (For 2xAmp Rack Primary)": "For 2xAmp Rack Primary",
    "Cisco SG300-10MP (For Drive Rack Primary)": "For Drive Rack Primary",
    "TP-Link TL-SG108E": "Default",
    "Cisco SG300-26P": "Default",
}
SECONDARY_TABLE_TO_TYPE_NAME = {
    "Cisco SG300-10MP (For 3xAmp Rack Primary)": "For 3xAmp Rack Secondary",
    "Cisco SG350-10 (For 2xAmp Rack Primary)": "For 2xAmp Rack Secondary",
}

#: Independently-declared expected ``(manufacturer, model, name)`` per
#: addressing-sheet Device Description, for ordinary (non-switch, non-SD12,
#: non-Device-Control) device rows — re-typed from PLAN-prod-import.md §7,
#: not imported from ``import_prod_data.DESCRIPTION_TO_DEVICE_KEY``. The two
#: ``-device-control`` rows aren't here (ADR 0022) — ``_check_device_
#: control_pairs()`` consumes and verifies those directly, against the
#: host's own identity.
DESCRIPTION_TO_DEVICE_IDENTITY: dict[str, tuple[str, str, str]] = {
    "IK42": ("Martin Audio", "IK-42", "with Dante Card"),
    "IK81": ("Martin Audio", "IK-81", "with Dante Card"),
    "LM26": ("Lab.Gruppen", "LM26", "Redundant Mode"),
    "LM44": ("Lab.Gruppen", "LM44", "Redundant Mode"),
    "PLM20000Q": ("Lab.Gruppen", "PLM20000Q", "Redundant Mode"),
    "SD9": ("DiGiCo", "SD9", "Default"),
    "SD11": ("DiGiCo", "SD11", "Default"),
    "DM7C-1": ("Yamaha", "DM7C", "Default"),
    "DM7-EX-1": ("Yamaha", "DM7-EX", "Default"),
    "bej-dm3-1": ("Yamaha", "DM3", "Default"),
    "bej-tio1608-d2-1": ("Yamaha", "Tio1608-D2", "Default"),
    "SQ5-1": ("Allen & Heath", "SQ-5", "Default"),
}

#: §5.2: these two IK-42 rows have no Dante card fitted, a different type
#: from every other "IK42" row.
IK42_WITHOUT_DANTE_CARD: frozenset[tuple[str, int]] = frozenset({("XE300-1", 3), ("XE300-1", 4)})

#: AVIO hostname prefix -> expected identity (§7 tier 3), most-specific
#: pattern first — see ``import_prod_data.AVIO_PATTERNS`` for why order
#: matters here (independently re-declared, not shared).
AVIO_IDENTITY_PATTERNS: tuple[tuple[re.Pattern[str], tuple[str, str, str]], ...] = (
    (re.compile(r"^mps-avio-amph-output-\d+$"), ("Amphenol", "RJD1212-0050", "Default")),
    (re.compile(r"^mps-avio-avio-aes-io-\d+$"), ("Amphenol", "RJD32A3-0050", "Default")),
    (re.compile(r"^mps-avio-avio-usb-io-\d+$"), ("Amphenol", "RJD32U1-0050", "Default")),
    (re.compile(r"^mps-avio-avio-input-\d+$"), ("Amphenol", "RJD2203-0050", "Default")),
    (re.compile(r"^mps-avio-avio-output-\d+$"), ("Audinate", "AVIO-AO2", "Default")),
    (re.compile(r"^mps-avio-na2-dline-\d+$"), ("Neutrik", "NA2-DLINE", "Default")),
    (re.compile(r"^mps-avio-radial-tx$"), ("Radial", "DiNET DAN-TX", "Default")),
    (re.compile(r"^mps-avio-radial-rx-\d+$"), ("Radial", "DiNET DAN-RX", "Default")),
)

SD12_IDENTITY = ("DiGiCo", "SD12", "Default")
DMI_DANTE_IDENTITY = ("DiGiCo", "DMI-DANTE", "Default")

#: Case-insensitive hostname suffix identifying a Yamaha console's Device
#: Control row in the addressing sheet (ADR 0022) — re-declared
#: independently of ``import_prod_data.DEVICE_CONTROL_SUFFIX``, same
#: convention.
DEVICE_CONTROL_SUFFIX = "-device-control"

#: A plain 1GbE copper jack is the only device port type in this dataset
#: (PROD-DATA-ANALYSIS.md §7.3) — re-declared as a literal here rather than
#: importing ``inventory.models.PortType``, so this check owes the app's
#: enum nothing either.
EXPECTED_DEVICE_PORT_TYPE = "1gbe_rj45"

#: Independently-declared expected port catalog per device type identity —
#: ``(manufacturer, model, name) -> ((description, VLAN function,
#: slot_offset), ...)``. Re-typed from PLAN-prod-import.md §7, not imported
#: from ``import_prod_data.DEVICE_TYPES``.
EXPECTED_DEVICE_TYPE_PORTS: dict[tuple[str, str, str], tuple[tuple[str, str, int], ...]] = {
    ("Martin Audio", "IK-42", "with Dante Card"): (
        ("Control", FN_CONTROL, 0),
        ("Dante Primary", FN_DANTE_PRIMARY, 0),
        ("Dante Secondary", FN_DANTE_SECONDARY, 0),
    ),
    ("Martin Audio", "IK-42", "without Dante Card"): (("Control", FN_CONTROL, 0),),
    ("Martin Audio", "IK-81", "with Dante Card"): (
        ("Control", FN_CONTROL, 0),
        ("Dante Primary", FN_DANTE_PRIMARY, 0),
        ("Dante Secondary", FN_DANTE_SECONDARY, 0),
    ),
    ("Lab.Gruppen", "LM26", "Redundant Mode"): (
        ("Dante Primary", FN_DANTE_PRIMARY, 0),
        ("Dante Secondary", FN_DANTE_SECONDARY, 0),
    ),
    ("Lab.Gruppen", "LM44", "Redundant Mode"): (
        ("Dante Primary", FN_DANTE_PRIMARY, 0),
        ("Dante Secondary", FN_DANTE_SECONDARY, 0),
    ),
    ("Lab.Gruppen", "PLM20000Q", "Redundant Mode"): (
        ("Dante Primary", FN_DANTE_PRIMARY, 0),
        ("Dante Secondary", FN_DANTE_SECONDARY, 0),
    ),
    SD12_IDENTITY: (("Control", FN_CONTROL, 0), ("Engine", FN_CONTROL, 1)),
    ("DiGiCo", "SD9", "Default"): (("Control", FN_CONTROL, 0),),
    ("DiGiCo", "SD11", "Default"): (("Control", FN_CONTROL, 0),),
    DMI_DANTE_IDENTITY: (
        ("Dante Primary", FN_DANTE_PRIMARY, 0),
        ("Dante Secondary", FN_DANTE_SECONDARY, 0),
    ),
    # The fourth, OPERATOR-sourced Device Control port (ADR 0022, closing
    # #42) shares its VLAN with the console's own Dante Primary port —
    # verified by ``_check_device_control_pairs()``, not by the
    # (vlan, offset)-keyed generic address check.
    ("Yamaha", "DM7C", "Default"): (
        ("Control", FN_CONTROL, 0),
        ("Dante Primary", FN_DANTE_PRIMARY, 0),
        ("Dante Secondary", FN_DANTE_SECONDARY, 0),
        ("Device Control", FN_DANTE_PRIMARY, 0),
    ),
    ("Yamaha", "DM7-EX", "Default"): (("Control", FN_CONTROL, 0),),
    ("Yamaha", "DM3", "Default"): (
        ("Control", FN_CONTROL, 0),
        ("Dante Primary", FN_DANTE_PRIMARY, 0),
        ("Dante Secondary", FN_DANTE_SECONDARY, 0),
        ("Device Control", FN_DANTE_PRIMARY, 0),
    ),
    ("Yamaha", "Tio1608-D2", "Default"): (
        ("Control", FN_CONTROL, 0),
        ("Dante Primary", FN_DANTE_PRIMARY, 0),
        ("Dante Secondary", FN_DANTE_SECONDARY, 0),
    ),
    ("Allen & Heath", "SQ-5", "Default"): (
        ("Control", FN_CONTROL, 0),
        ("Dante Primary", FN_DANTE_PRIMARY, 0),
        ("Dante Secondary", FN_DANTE_SECONDARY, 0),
    ),
    ("Amphenol", "RJD1212-0050", "Default"): (("Dante Primary", FN_DANTE_PRIMARY, 0),),
    ("Amphenol", "RJD2203-0050", "Default"): (("Dante Primary", FN_DANTE_PRIMARY, 0),),
    ("Amphenol", "RJD32A3-0050", "Default"): (("Dante Primary", FN_DANTE_PRIMARY, 0),),
    ("Amphenol", "RJD32U1-0050", "Default"): (("Dante Primary", FN_DANTE_PRIMARY, 0),),
    ("Audinate", "AVIO-AO2", "Default"): (("Dante Primary", FN_DANTE_PRIMARY, 0),),
    ("Neutrik", "NA2-DLINE", "Default"): (("Dante Primary", FN_DANTE_PRIMARY, 0),),
    ("Radial", "DiNET DAN-TX", "Default"): (("Dante Primary", FN_DANTE_PRIMARY, 0),),
    ("Radial", "DiNET DAN-RX", "Default"): (("Dante Primary", FN_DANTE_PRIMARY, 0),),
}


def _expected_device_identity(row: AddressingRow) -> tuple[str, str, str]:
    """Independently-derived expected ``(manufacturer, model, name)`` for an
    ordinary device row — a separate classification from
    ``import_prod_data.py``'s, not a call into it.
    """
    if row.description == "IK42" and (row.rack, row.slot) in IK42_WITHOUT_DANTE_CARD:
        return ("Martin Audio", "IK-42", "without Dante Card")
    mapped = DESCRIPTION_TO_DEVICE_IDENTITY.get(row.description)
    if mapped is not None:
        return mapped
    for pattern, identity in AVIO_IDENTITY_PATTERNS:
        if pattern.match(row.description):
            return identity
    raise CommandError(f"Cannot independently classify device {row.description!r} at {row.rack}/{row.slot}.")


def _expected_switch_identity(description: str) -> tuple[str, str, str]:
    """Independently-derived expected ``(manufacturer, model, name)`` for a
    switch row's Device Description — a separate parse from
    ``import_prod_data.SWITCH_DESCRIPTION_TO_TABLE``, not a call into it.
    """
    if description == "Spare SG300-26P":
        return ("Cisco", "SG300-26P", "Default")
    if _TLSG108E_RE.match(description):
        return ("TP-Link", "TL-SG108E", "Default")
    match = re.match(r"^([\w.\-]+) ([\w.\-]+) \(([^)]+)\)$", description)
    if match is None:
        raise CommandError(f"Cannot independently classify switch {description!r}.")
    return match.group(1), match.group(2), match.group(3)


@dataclass
class _Mismatch:
    check: str
    detail: str


class _Findings:
    def __init__(self) -> None:
        self.mismatches: list[_Mismatch] = []
        self.counters: dict[str, int] = {}

    def fail(self, check: str, detail: str) -> None:
        self.mismatches.append(_Mismatch(check, detail))

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + n


def _check_device_type_identity(
    findings: _Findings, label: str, actual: NetworkDevice, expected_identity: tuple[str, str, str]
) -> None:
    actual_identity = (
        actual.device_type.device_model.manufacturer,
        actual.device_type.device_model.model,
        actual.device_type.name,
    )
    if actual_identity != expected_identity:
        findings.fail("type_assignment", f"{label}: device type {actual_identity} != {expected_identity}.")


class Command(BaseCommand):
    help = (
        "Verify a completed `import_prod_data` run against the source CSVs "
        "(PLAN-prod-import.md's ## Verification). Every check here re-derives its own "
        "expected values from the CSVs independently of the importer."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--data-dir", default="prod", help="Directory containing the three source CSVs.")

    def handle(self, *args: Any, **options: Any) -> None:
        data_dir = Path(options["data_dir"])
        addressing_path = data_dir / ADDRESSING_CSV_NAME
        calc_lookups_path = data_dir / CALC_LOOKUPS_CSV_NAME
        switch_ports_path = data_dir / SWITCH_PORTS_CSV_NAME
        for path in (addressing_path, calc_lookups_path, switch_ports_path):
            if not path.is_file():
                raise CommandError(f"Missing source CSV: {path}")

        vlan_rows = parse_vlan_table(read_csv_rows(calc_lookups_path))
        rack_offset_rows = parse_rack_offsets(read_csv_rows(calc_lookups_path))
        addressing_rows = _dedupe(parse_addressing_rows(read_csv_rows(addressing_path)))
        switch_port_tables = {t.name: t for t in parse_switch_port_tables(read_csv_rows(switch_ports_path))}

        # ADR 0026 decision 5 — optional, like the importer's own copy.
        device_models_path = data_dir / DEVICE_MODELS_CSV_NAME
        device_model_rows = (
            parse_device_models(read_csv_rows(device_models_path)) if device_models_path.is_file() else []
        )

        vlan_id_by_function = {row.function: row.vlan_id for row in vlan_rows}
        vlan_subnet_by_id = {row.vlan_id: row.subnet for row in vlan_rows}

        findings = _Findings()
        _check_rack_ranges(findings, rack_offset_rows, vlan_rows)
        _check_owner_seeding(findings, rack_offset_rows)
        _check_hostname_slugs(findings)
        _check_device_model_descriptions(findings, device_model_rows)
        _check_no_equipment_hostname_seeding(findings)
        _check_hostnames_and_types_and_addresses(
            findings,
            addressing_rows,
            rack_offset_rows,
            vlan_id_by_function,
            vlan_subnet_by_id,
            switch_port_tables,
        )
        real_vlan_ids = frozenset(vlan_subnet_by_id.keys())
        _check_switch_port_matrices(findings, switch_port_tables, vlan_id_by_function, real_vlan_ids)
        _check_device_type_ports(findings, vlan_id_by_function)
        _check_dhcp_server_enabled(findings)
        _check_cross_vlan_alignment(findings)
        _check_broadcast_dhcp_gateway(findings, vlan_rows)

        self._report(findings)

    def _report(self, findings: _Findings) -> None:
        for name, value in sorted(findings.counters.items()):
            self.stdout.write(f"{name}: {value}")
        if findings.mismatches:
            self.stdout.write(self.style.ERROR(f"\n{len(findings.mismatches)} verification failure(s):"))
            for m in findings.mismatches:
                self.stdout.write(self.style.ERROR(f"  [{m.check}] {m.detail}"))
            raise CommandError(f"{len(findings.mismatches)} verification failure(s) — see above.")
        self.stdout.write(self.style.SUCCESS("\nAll verification checks passed."))


def _dedupe(rows: list[AddressingRow]) -> list[AddressingRow]:
    seen: set[tuple[str, int]] = set()
    result = []
    for row in rows:
        key = (row.rack, row.slot)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _is_switch_row(row: AddressingRow) -> bool:
    return row.description in SWITCH_DESCRIPTIONS or bool(_TLSG108E_RE.match(row.description))


# -- Rack ranges: all 63 (rack, VLAN, CIDR) triples --------------------------------


def _check_rack_ranges(findings: _Findings, rack_offset_rows: list[Any], vlan_rows: list[Any]) -> None:
    """Every rack's base on every VLAN is the VLAN's network address plus
    the *same* offset recorded once in the Calc Lookups CSV — this is the
    production sheet's own guarantee (PROD-DATA-ANALYSIS.md §2.1: "one
    offset, applied to every VLAN base"), not anything computed by the
    app's suggester. A rack of 30 slots needs a block of 32 addresses
    (``max(30 + 2, 32)``) — a floor at 32/``/27`` regardless of occupancy
    (ADR 0015) — which is asserted directly here rather than imported from
    ``suggestions.required_block_size`` so this check owes the importer
    nothing.
    """
    audio_functions = (FN_CONTROL, FN_DANTE_PRIMARY, FN_DANTE_SECONDARY)
    vlan_by_function = {row.function: row for row in vlan_rows}
    checked = 0
    for rack_row in rack_offset_rows:
        try:
            rack = Rack.objects.get(name=rack_row.rack)
        except Rack.DoesNotExist:
            findings.fail("rack_ranges", f"{rack_row.rack}: no such Rack in the database.")
            continue
        for function in audio_functions:
            vlan_row = vlan_by_function[function]
            network = ipaddress.IPv4Network(vlan_row.subnet, strict=True)
            expected_base = network.network_address + rack_row.offset
            expected_cidr = f"{expected_base}/27"
            actual = rack.vlan_ranges.filter(vlan__vlan_id=vlan_row.vlan_id).first()
            checked += 1
            if actual is None:
                findings.fail("rack_ranges", f"{rack.name}/{function}: no RackVlanRange exists.")
            elif actual.address_range != expected_cidr:
                findings.fail(
                    "rack_ranges",
                    f"{rack.name}/{function}: expected {expected_cidr}, got {actual.address_range}.",
                )
    findings.count("rack_ranges_checked", checked)


# -- Owner seeding (ADR 0023 decision 10) ---------------------------------------------


def _check_owner_seeding(findings: _Findings, rack_offset_rows: list[Any]) -> None:
    """Both ``Owner`` rows exist with the expected name; every imported
    rack's ``owner`` is ``mps``; every rack's ``location_slug`` matches the
    independently re-derived slugify-plus-exceptions rule, ``CONSOLES``
    null.
    """
    for slug, name in OWNER_ROWS:
        try:
            owner = Owner.objects.get(slug=slug)
        except Owner.DoesNotExist:
            findings.fail("owner_seeding", f"No Owner with slug {slug!r} exists.")
            continue
        if owner.name != name:
            findings.fail("owner_seeding", f"Owner {slug!r}: expected name {name!r}, got {owner.name!r}.")
    findings.count("owners_checked", len(OWNER_ROWS))

    mps = Owner.objects.filter(slug="mps").first()
    if mps is None:
        return  # already reported above; nothing further to check against a missing mps

    checked = 0
    for rack_row in rack_offset_rows:
        try:
            rack = Rack.objects.get(name=rack_row.rack)
        except Rack.DoesNotExist:
            continue  # already reported by _check_rack_ranges
        checked += 1
        if rack.owner_id != mps.pk:
            actual_slug = rack.owner.slug if rack.owner else None
            findings.fail("owner_seeding", f"{rack.name}: expected owner 'mps', got {actual_slug!r}.")
        expected_slug = _expected_location_slug(rack_row.rack)
        if rack.location_slug != expected_slug:
            findings.fail(
                "owner_seeding",
                f"{rack.name}: expected location_slug {expected_slug!r}, got {rack.location_slug!r}.",
            )
    findings.count("rack_owners_and_locations_checked", checked)


def _check_hostname_slugs(findings: _Findings) -> None:
    """ADR 0023 decision 10, amended (phase 18 PR 4) — every
    ``(manufacturer, model)`` this independently re-declared
    ``HOSTNAME_SLUGS`` names has a matching Type row (of either
    hierarchy) carrying exactly that ``hostname_slug`` — both profiles of
    a two-profile model included, since the constant is keyed on the
    model, not the profile.
    """
    checked = 0
    for (manufacturer, model), expected_slug in HOSTNAME_SLUGS.items():
        switch_matches = list(NetworkSwitchType.objects.filter(manufacturer=manufacturer, model=model))
        # ADR 0026 — the identity moved off NetworkDeviceType onto its FK.
        device_matches = list(
            NetworkDeviceType.objects.filter(
                device_model__manufacturer=manufacturer, device_model__model=model
            )
        )
        matches = switch_matches + device_matches
        if not matches:
            findings.fail(
                "hostname_slugs", f"{manufacturer!r}/{model!r}: no Type found for a HOSTNAME_SLUGS entry."
            )
            continue
        for type_row in matches:
            checked += 1
            if type_row.hostname_slug != expected_slug:
                findings.fail(
                    "hostname_slugs",
                    f"{manufacturer!r}/{model!r} ({type_row.name}): expected hostname_slug "
                    f"{expected_slug!r}, got {type_row.hostname_slug!r}.",
                )
    findings.count("hostname_slugs_checked", checked)


def _check_device_model_descriptions(findings: _Findings, device_model_rows: list[DeviceModelRow]) -> None:
    """ADR 0026 decision 5 — when the Device Models CSV is present, every
    row's description matches the ``NetworkDeviceModel`` row it names.

    Reads the CSV rows handed in directly, never
    ``import_prod_data.py``'s own parsing/helpers, per this module's
    docstring: a check sharing the importer's own reading proves nothing.
    A no-op (not a failure) when the CSV wasn't found at all — the
    importer treats absence as valid, and so does this check.
    """
    checked = 0
    for row in device_model_rows:
        try:
            device_model = NetworkDeviceModel.objects.get(manufacturer=row.manufacturer, model=row.model)
        except NetworkDeviceModel.DoesNotExist:
            findings.fail(
                "device_model_descriptions",
                f"{row.manufacturer!r}/{row.model!r}: no NetworkDeviceModel found for a Device "
                "Models CSV row.",
            )
            continue
        checked += 1
        if device_model.description != row.description:
            findings.fail(
                "device_model_descriptions",
                f"{row.manufacturer!r}/{row.model!r}: expected description {row.description!r}, "
                f"got {device_model.description!r}.",
            )
    findings.count("device_model_descriptions_checked", checked)


def _check_no_equipment_hostname_seeding(findings: _Findings) -> None:
    """ADR 0023 decision 10's negative half: no ``NetworkDevice`` or
    ``NetworkSwitch`` carries an ``owner``, ``hostname_purpose`` or
    ``hostname_sequence`` — this seeding boundary is what's most likely to
    drift under a well-meaning future backfill, so asserting its absence
    catches that rather than assuming it.
    """
    seeded_something = Q(owner__isnull=False) | ~Q(hostname_purpose="") | Q(hostname_sequence__isnull=False)
    for switch in NetworkSwitch.objects.filter(seeded_something):
        findings.fail(
            "no_equipment_hostname_seeding",
            f"NetworkSwitch {switch}: unexpectedly carries owner/hostname_purpose/hostname_sequence.",
        )
    for device in NetworkDevice.objects.filter(seeded_something):
        findings.fail(
            "no_equipment_hostname_seeding",
            f"NetworkDevice {device}: unexpectedly carries owner/hostname_purpose/hostname_sequence.",
        )
    findings.count(
        "equipment_checked_for_no_hostname_seeding",
        NetworkSwitch.objects.count() + NetworkDevice.objects.count(),
    )


# -- Device Control pairs (ADR 0022) -------------------------------------------------


def _check_device_control_pairs(
    findings: _Findings,
    addressing_rows: list[AddressingRow],
    consumed: set[tuple[str, int]],
    actual_devices: dict[tuple[str, int], NetworkDevice],
    vlan_id_by_function: dict[str, int],
    expected_device_keys: set[tuple[str, int]],
) -> int:
    """Independent Device Control check (ADR 0022) — mirrors the SD12
    Control/Engine lookahead inside the main per-row loop below, but keyed
    on the ``-device-control`` hostname suffix rather than slot adjacency
    (the row can sit either below or above its host — DM7C/DM3
    respectively — which slot-adjacency can't express in both directions
    at once), and re-declared independently of ``import_prod_data.py``'s
    own pre-pass.

    Runs as a genuine pre-pass over *every* row, not a lookahead triggered
    while iterating — CSV order isn't reliable (the DM7C's Device Control
    row precedes its host, the DM3's follows it), so this must find pairs
    regardless of which row it meets first. Marks both rows' ``(rack,
    slot)`` keys consumed so the main loop's ordinary per-row branch never
    re-processes either one — mirroring how the SD12 branch consumes its
    own pair inline.

    Unlike ADR 0018's superseded shape, the Device Control row's address is
    folded into its console's own device row rather than a second device
    (ADR 0022) — this consumes both CSV rows but locates and verifies
    **one** device: the host's hostname, type identity, its three ordinary
    ports, and — independently, by description, since it shares a VLAN
    with Dante Primary — the Device Control port's own address.

    Adds the host's ``(rack, slot)`` to ``expected_device_keys`` (Codex
    review, P2) — unconditionally, whether or not the host device is
    actually found — so the caller's final actual-vs-expected device-slot
    comparison can prove the *complete* claim this PR rests on: not just
    that the host exists, but that the Device Control row's own ``(rack,
    slot)`` never gets a key added at all, so a stale device sitting there
    (a released slot, e.g. ``CONSOLES`` 4/16, that this import should have
    left empty) is caught as an unexplained extra rather than silently
    ignored.

    Also asserts the Device Control row's own ``control``/``dante_
    secondary`` columns are blank (Codex review, P2) — only ``dante_
    primary`` is ever read below; a populated column would otherwise
    describe a real address this check silently discards.

    Returns the number of byte-identical static addresses verified here,
    so the caller can fold it into its own running total — this pass runs
    *before* that total exists, so the two can't be accumulated any other
    way, and ``total_placed``'s reconciliation at the end of that function
    otherwise undercounts by exactly the addresses checked here.
    """
    byte_identical = 0
    pairs_checked = 0
    for row in addressing_rows:
        key = (row.rack, row.slot)
        if key in consumed or not row.description.lower().endswith(DEVICE_CONTROL_SUFFIX):
            continue
        stem = row.description[: -len(DEVICE_CONTROL_SUFFIX)]
        host_row = next(
            (
                other
                for other in addressing_rows
                if other.rack == row.rack and other is not row and other.description.lower() == stem.lower()
            ),
            None,
        )
        if host_row is None:
            findings.fail(
                "device_control_link", f"{row.rack} slot {row.slot}: no host row matching {stem!r} found."
            )
            consumed.add(key)
            continue
        host_key = (host_row.rack, host_row.slot)
        consumed.add(key)
        consumed.add(host_key)
        expected_device_keys.add(host_key)
        pairs_checked += 1

        if row.control or row.dante_secondary:
            findings.fail(
                "address_diff",
                f"{row.rack}/{row.slot} ({row.description}): Device Control row has a "
                f"control={row.control!r}/dante_secondary={row.dante_secondary!r} value — only "
                "dante_primary is ever read from this row, so a populated column here would be "
                "silently discarded.",
            )

        host = actual_devices.get(host_key)
        if host is None:
            findings.fail(
                "devices", f"{host_row.rack} slot {host_row.slot}: expected host device, none found."
            )
            continue

        # Case-insensitive (ADR 0023 decision 8, amended) — phase 18
        # lowercases hostname on write and backfills it, but this verifier
        # compares against the raw CSV, which is still whatever case the
        # sheet happened to use. Independence intact: nothing here imports
        # from the importer, this just stops comparing casing the importer
        # never controlled anyway.
        if host.hostname.strip().lower() != host_row.description.strip().lower():
            findings.fail(
                "hostnames",
                f"{host_row.rack} slot {host_row.slot}: hostname {host.hostname!r} != "
                f"{host_row.description!r}.",
            )
        expected_identity = _expected_device_identity(host_row)
        _check_device_type_identity(
            findings, f"{host_row.rack}/{host_row.slot} ({host_row.description})", host, expected_identity
        )

        for function, sheet_value in (
            (FN_CONTROL, host_row.control),
            (FN_DANTE_PRIMARY, host_row.dante_primary),
            (FN_DANTE_SECONDARY, host_row.dante_secondary),
        ):
            actual_value = _device_address(host, description=FUNCTION_TO_PORT_DESCRIPTION[function])
            if not sheet_value:
                if actual_value is not None:
                    findings.fail(
                        "address_diff",
                        f"{host_row.rack}/{host_row.slot} {function}: unexpected address {actual_value}.",
                    )
                continue
            byte_identical += _compare(
                findings,
                "address_diff",
                f"{host_row.rack}/{host_row.slot} {function}",
                sheet_value,
                actual_value,
            )

        # The Device Control port itself — asserted independently of Dante
        # Primary above even though both live on the same VLAN (both
        # addresses in production point in opposite directions from their
        # console, ADR 0022), which is exactly why _device_address()
        # selects by description rather than (vlan, offset) here.
        device_control_actual = _device_address(host, description="Device Control")
        byte_identical += _compare(
            findings,
            "address_diff",
            f"{host_row.rack}/{host_row.slot} Device Control",
            row.dante_primary,
            device_control_actual,
        )
    findings.count("device_control_pairs_checked", pairs_checked)
    return byte_identical


# -- Hostnames, type assignment, address manifest ----------------------------------


def _check_hostnames_and_types_and_addresses(
    findings: _Findings,
    addressing_rows: list[AddressingRow],
    rack_offset_rows: list[Any],
    vlan_id_by_function: dict[str, int],
    vlan_subnet_by_id: dict[int, str],
    switch_port_tables: dict[str, SwitchPortTable],
) -> None:
    by_key = {(r.rack, r.slot): r for r in addressing_rows}
    consumed: set[tuple[str, int]] = set()

    actual_switch_addresses: dict[tuple[str, int, int], str | None] = {}
    for a in NetworkSwitchAddress.objects.select_related("switch__rack", "vlan"):
        if a.switch.rack is None or a.switch.rack_slot is None:
            continue  # unracked switches never materialize addresses (ADR 0016)
        actual_switch_addresses[(a.switch.rack.name, a.switch.rack_slot, a.vlan.vlan_id)] = a.address

    actual_switches: dict[tuple[str, int], NetworkSwitch] = {}
    for s in NetworkSwitch.objects.select_related("rack", "switch_type"):
        if s.rack is not None and s.rack_slot is not None:
            actual_switches[(s.rack.name, s.rack_slot)] = s

    actual_devices: dict[tuple[str, int], NetworkDevice] = {}
    for d in NetworkDevice.objects.select_related("rack", "device_type__device_model", "host"):
        if d.rack is not None and d.rack_slot is not None:
            actual_devices[(d.rack.name, d.rack_slot)] = d

    # Every rack's range CIDR, keyed by (rack name, VLAN id) — lets the
    # "extra" switch-address branch below derive base + slot independently
    # rather than merely confirming *something* is there.
    rack_range_cidr: dict[tuple[str, int], str] = {}
    for rack_obj in Rack.objects.prefetch_related("vlan_ranges__vlan"):
        for rng in rack_obj.vlan_ranges.all():
            rack_range_cidr[(rack_obj.name, rng.vlan.vlan_id)] = rng.address_range

    total_placed = (
        NetworkSwitchAddress.objects.count() + NetworkDevicePort.objects.filter(address__isnull=False).count()
    )
    findings.count("total_addresses_placed", total_placed)

    byte_identical = 0
    differs_by_design = 0
    extra_verified = 0
    dmi_dante_rack_slot: dict[str, int] = {}
    #: ADR 0022 PR 3 — the console (an already-verified NetworkDevice)
    #: whose row carried the marker for each rack in dmi_dante_rack_slot,
    #: so the final loop below can assert the DMI-DANTE card's ``host`` is
    #: linked to *that* console specifically, not merely that a card exists.
    dmi_dante_console_by_rack: dict[str, NetworkDevice] = {}

    # Every (rack, slot) an actual NetworkDevice is expected to occupy,
    # built up alongside the loops below and compared against
    # actual_devices' own key set at the end (Codex review, P2) — the only
    # thing that actually proves a released slot (CONSOLES 4/16) is empty,
    # as opposed to merely never being asserted present.
    expected_device_keys: set[tuple[str, int]] = set()

    # ADR 0022 — Device Control pairs, verified (and their two (rack, slot)
    # keys consumed) *before* the main per-row loop below, mirroring how
    # the SD12 branch inside that loop self-contains its own pair. See
    # _check_device_control_pairs()'s own docstring for why this can't be
    # a lookahead from inside the loop the way SD12's is.
    byte_identical += _check_device_control_pairs(
        findings, addressing_rows, consumed, actual_devices, vlan_id_by_function, expected_device_keys
    )

    for row in addressing_rows:
        key = (row.rack, row.slot)
        if key in consumed:
            continue

        if row.description == NETGEAR_DESCRIPTION:
            consumed.add(key)
            continue

        if _is_switch_row(row):
            consumed.add(key)
            switch = actual_switches.get(key)
            if switch is None:
                findings.fail(
                    "switches",
                    f"{row.rack} slot {row.slot}: expected switch {row.description!r}, none found.",
                )
                continue
            # Case-insensitive — see the identical comparison above for why.
            if switch.hostname.strip().lower() != row.description.strip().lower():
                findings.fail(
                    "hostnames",
                    f"{row.rack} slot {row.slot}: hostname {switch.hostname!r} != {row.description!r}.",
                )
            expected_identity = _expected_switch_identity(row.description)
            actual_identity = (
                switch.switch_type.manufacturer,
                switch.switch_type.model,
                switch.switch_type.name,
            )
            if actual_identity != expected_identity:
                findings.fail(
                    "type_assignment",
                    f"{row.rack} slot {row.slot}: switch type {actual_identity} != {expected_identity}.",
                )
            for function, sheet_value in (
                (FN_CONTROL, row.control),
                (FN_DANTE_PRIMARY, row.dante_primary),
                (FN_DANTE_SECONDARY, row.dante_secondary),
            ):
                vlan_id = vlan_id_by_function[function]
                actual_value = actual_switch_addresses.get((row.rack, row.slot, vlan_id))
                if actual_value is None:
                    findings.fail(
                        "address_diff",
                        f"switch {row.rack}/{row.slot} has no address on {function} at all.",
                    )
                    continue
                if sheet_value:
                    byte_identical += _compare(
                        findings,
                        "address_diff",
                        f"switch {row.rack}/{row.slot} {function}",
                        sheet_value,
                        actual_value,
                    )
                    continue
                # Sheet leaves this column blank — the "extra" switch
                # address §8 documents. Derive base + slot independently
                # and assert it exactly, rather than merely confirming
                # *something* is there (that let a wrong third address, or
                # arbitrary extra addressed equipment, pass silently).
                range_cidr = rack_range_cidr.get((row.rack, vlan_id))
                if range_cidr is None:
                    findings.fail(
                        "address_diff",
                        f"switch {row.rack}/{row.slot} {function}: no RackVlanRange to derive the "
                        "expected extra address from.",
                    )
                    continue
                expected_extra = suggest_slot_address(range_cidr, row.slot)
                if actual_value == expected_extra:
                    extra_verified += 1
                else:
                    findings.fail(
                        "address_diff",
                        f"switch {row.rack}/{row.slot} {function}: expected derived {expected_extra} "
                        f"(sheet blank), got {actual_value}.",
                    )
            continue

        # -- device rows ------------------------------------------------------
        if row.description.endswith("-Control"):
            stem = row.description[: -len("-Control")]
            engine_key = (row.rack, row.slot + 1)
            engine_row = by_key.get(engine_key)
            if engine_row is not None and engine_row.description == f"{stem}-Engine":
                consumed.add(key)
                consumed.add(engine_key)
                expected_device_keys.add(key)
                device = actual_devices.get(key)
                if device is None:
                    findings.fail(
                        "devices", f"{row.rack} slot {row.slot}: expected SD12 device {stem!r}, none found."
                    )
                    continue
                # Case-insensitive — see the SD12 host comparison above for
                # why (ADR 0023 decision 8, amended). Not one of the two
                # comparisons the plan named at PR-planning time, but the
                # same casing divergence reaches this one too.
                if device.hostname.strip().lower() != stem.strip().lower():
                    findings.fail(
                        "hostnames", f"{row.rack} slot {row.slot}: hostname {device.hostname!r} != {stem!r}."
                    )
                _check_device_type_identity(
                    findings, f"{row.rack}/{row.slot} ({stem})", device, SD12_IDENTITY
                )
                control_actual = _device_address(device, description="Control")
                engine_actual = _device_address(device, description="Engine")
                byte_identical += _compare(
                    findings, "address_diff", f"{stem} Control", row.control, control_actual
                )
                byte_identical += _compare(
                    findings, "address_diff", f"{stem} Engine", engine_row.control, engine_actual
                )
                if row.dante_primary or row.dante_secondary:
                    # Pinned, not derived (PLAN-prod-import.md §9) — see
                    # DMI_DANTE_RACK/DMI_DANTE_SLOT. A marker found anywhere
                    # else is refused rather than guessed at.
                    if row.rack != DMI_DANTE_RACK:
                        findings.fail(
                            "devices",
                            f"DMI-DANTE marker found on {row.rack}/{row.slot}, but it's pinned to "
                            f"{DMI_DANTE_RACK} slot {DMI_DANTE_SLOT} (PLAN-prod-import.md §9).",
                        )
                    else:
                        dmi_dante_rack_slot[row.rack] = DMI_DANTE_SLOT
                        dmi_dante_console_by_rack[row.rack] = device
                        expected_device_keys.add((DMI_DANTE_RACK, DMI_DANTE_SLOT))
                continue

        # ordinary device row
        consumed.add(key)
        expected_device_keys.add(key)
        device = actual_devices.get(key)
        if device is None:
            findings.fail(
                "devices", f"{row.rack} slot {row.slot}: expected device {row.description!r}, none found."
            )
            continue
        # Case-insensitive — see the SD12 host comparison above for why.
        if device.hostname.strip().lower() != row.description.strip().lower():
            findings.fail(
                "hostnames",
                f"{row.rack} slot {row.slot}: hostname {device.hostname!r} != {row.description!r}.",
            )
        expected_identity = _expected_device_identity(row)
        _check_device_type_identity(
            findings, f"{row.rack}/{row.slot} ({row.description})", device, expected_identity
        )
        for column_name, function, sheet_value in (
            ("control", FN_CONTROL, row.control),
            ("dante_primary", FN_DANTE_PRIMARY, row.dante_primary),
            ("dante_secondary", FN_DANTE_SECONDARY, row.dante_secondary),
        ):
            dropped = (
                (column_name == "control" and row.description in NO_CONTROL_PORT_DESCRIPTIONS)
                or (column_name != "control" and key in NO_DANTE_CARD_SLOTS)
                or (column_name == "control" and row.rack == "AVIO")
            )
            actual_value = _device_address(device, description=FUNCTION_TO_PORT_DESCRIPTION[function])
            if dropped:
                if actual_value is not None:
                    findings.fail(
                        "address_diff",
                        f"{row.rack}/{row.slot} {function}: expected this address dropped "
                        f"(PROD-DATA-ANALYSIS.md §5), but found {actual_value}.",
                    )
                continue
            if not sheet_value:
                if actual_value is not None:
                    findings.fail(
                        "address_diff",
                        f"{row.rack}/{row.slot} {function}: unexpected address {actual_value}.",
                    )
                continue
            byte_identical += _compare(
                findings, "address_diff", f"{row.rack}/{row.slot} {function}", sheet_value, actual_value
            )

    # -- DMI-DANTE: differs by design (§9) ---------------------------------------
    for rack, slot in dmi_dante_rack_slot.items():
        device = actual_devices.get((rack, slot))
        if device is None:
            findings.fail("devices", f"{rack} slot {slot}: expected the DMI-DANTE card device, none found.")
            continue
        _check_device_type_identity(findings, f"DMI-DANTE {rack}/{slot}", device, DMI_DANTE_IDENTITY)
        # ADR 0022 PR 3 — the card is linked to the console whose row
        # carried the marker (settled decision 7), not merely to *some*
        # console. #41 stays open: no address changes here, and this check
        # must never be widened to expect the card's addresses to match its
        # console's.
        expected_console = dmi_dante_console_by_rack.get(rack)
        if expected_console is None or device.host_id != expected_console.pk:
            findings.fail(
                "devices",
                f"DMI-DANTE {rack}/{slot}: expected host {expected_console} "
                f"(pk={getattr(expected_console, 'pk', None)}), got host_id={device.host_id}.",
            )
        else:
            findings.count("device_control_pairs_linked", 1)
        for function in (FN_DANTE_PRIMARY, FN_DANTE_SECONDARY):
            vlan_id = vlan_id_by_function[function]
            range_cidr = rack_range_cidr.get((rack, vlan_id))
            if range_cidr is None:
                findings.fail("address_diff", f"DMI-DANTE {rack}: no RackVlanRange for {function}.")
                continue
            expected = suggest_slot_address(range_cidr, slot)
            actual_value = _device_address(device, description=FUNCTION_TO_PORT_DESCRIPTION[function])
            if actual_value != expected:
                findings.fail(
                    "address_diff",
                    f"DMI-DANTE {rack}/{slot} {function}: expected {expected}, got {actual_value}.",
                )
            else:
                differs_by_design += 1

    findings.count("byte_identical", byte_identical)
    findings.count("differs_by_design", differs_by_design)
    findings.count("extra_unrecorded_switch_addresses", extra_verified)
    accounted = byte_identical + differs_by_design + extra_verified
    if accounted != total_placed:
        findings.fail(
            "address_diff",
            f"{total_placed} addresses are placed in the database, but only {accounted} were "
            f"accounted for by byte_identical ({byte_identical}) + differs_by_design "
            f"({differs_by_design}) + verified-extra ({extra_verified}) — "
            f"{total_placed - accounted} placed address(es) match no expected row at all.",
        )

    # The complete actual device-slot set against the complete expected
    # one (Codex review, P2) — every earlier check here confirms an
    # *expected* row's device exists and is correct, but none of them ever
    # asked the opposite question: does an actual device exist at a key
    # nothing expects one at? That's the only thing that actually proves a
    # released slot (CONSOLES 4/16, ADR 0022) is empty rather than merely
    # unasserted — a stale device sitting there would otherwise pass
    # silently, since no CSV row ever names that key to check "expected
    # device, none found" against.
    actual_device_keys = set(actual_devices.keys())
    missing_devices = expected_device_keys - actual_device_keys
    extra_devices = actual_device_keys - expected_device_keys
    if missing_devices or extra_devices:
        findings.fail(
            "devices",
            "actual device-slot set does not match the expected set — "
            f"missing {sorted(missing_devices)}, extra {sorted(extra_devices)}.",
        )
    findings.count("expected_device_keys_checked", len(expected_device_keys))


def _device_address(device: NetworkDevice, *, description: str) -> str | None:
    """The address of one of ``device``'s already-materialized ports,
    selected by ``description`` — not ``(vlan, slot_offset)`` (ADR 0022):
    a console's Dante Primary and its Device Control interface now share
    both, which made that selector ambiguous. Every port description in
    this dataset's independently-declared catalog
    (``EXPECTED_DEVICE_TYPE_PORTS``) is unique per device type, so
    ``description`` alone disambiguates.
    """
    port = device.ports.filter(description=description).first()
    return port.address if port is not None else None


def _compare(findings: _Findings, check: str, label: str, expected: str, actual: str | None) -> int:
    if actual is None:
        findings.fail(check, f"{label}: expected {expected}, found no address.")
        return 0
    if actual != expected:
        findings.fail(check, f"{label}: expected {expected}, got {actual}.")
        return 0
    return 1


# -- Switch port matrices, with an independently-derived secondary oracle ----------


def _check_switch_port_matrices(
    findings: _Findings,
    switch_port_tables: dict[str, SwitchPortTable],
    vlan_id_by_function: dict[str, int],
    real_vlan_ids: frozenset[int],
) -> None:
    control_id = vlan_id_by_function[FN_CONTROL]
    dante_p_id = vlan_id_by_function[FN_DANTE_PRIMARY]
    dante_s_id = vlan_id_by_function[FN_DANTE_SECONDARY]

    for table_name, type_name in PRIMARY_TABLE_TO_TYPE_NAME.items():
        table = switch_port_tables[table_name]
        real_ports = tuple(_restrict_to_real_vlans(p, real_vlan_ids) for p in table.ports)
        _check_one_switch_type_matrix(findings, table_name, type_name, real_ports)

    for table_name, type_name in SECONDARY_TABLE_TO_TYPE_NAME.items():
        table = switch_port_tables[table_name]
        derived = tuple(
            _restrict_to_real_vlans(
                _derive_secondary_port(p, control_id, dante_p_id, dante_s_id), real_vlan_ids
            )
            for p in table.ports
        )
        _check_one_switch_type_matrix(findings, table_name, type_name, derived)


def _restrict_to_real_vlans(port: SwitchPortRow, real_vlan_ids: frozenset[int]) -> SwitchPortRow:
    """Only real VLANs can ever be recorded as ``allowed_vlans`` — it's a
    ``PROTECT``-FK through model — so the CSV's own future-proofing range
    (``202-207`` names four VLAN ids that don't exist) is narrowed here,
    independently of ``import_prod_data.py``'s equivalent narrowing.
    """
    restricted = port.allowed_vlan_ids & real_vlan_ids
    if restricted == port.allowed_vlan_ids:
        return port
    return SwitchPortRow(
        port.description,
        port.port_number,
        port.native_vlan_id,
        port.mode,
        restricted,
        port.port_type,
        port.note,
    )


def _derive_secondary_port(
    port: SwitchPortRow, control_id: int, dante_p_id: int, dante_s_id: int
) -> SwitchPortRow:
    """The secondary switch's own port matrix, computed straight from the
    Switch Ports CSV's documented rule ("ports with Native VLAN 201 become
    Native VLAN 202; Update Allowed VLANs list accordingly") — written from
    scratch here, not shared with ``import_prod_data.py``'s equivalent.
    """
    if port.mode == "access" and port.native_vlan_id == control_id:
        return port  # control ports are unpatched but keep their configuration
    if port.mode == "access" and port.native_vlan_id == dante_p_id:
        return SwitchPortRow(
            port.description, port.port_number, dante_s_id, port.mode, frozenset(), port.port_type, port.note
        )
    if port.mode == "trunk" and port.native_vlan_id == dante_p_id:
        allowed = (port.allowed_vlan_ids - {dante_s_id}) | {dante_p_id}
        return SwitchPortRow(
            port.description, port.port_number, dante_s_id, port.mode, allowed, port.port_type, port.note
        )
    raise CommandError(f"Cannot derive a secondary oracle port for {port.description!r}.")


def _check_one_switch_type_matrix(
    findings: _Findings, table_name: str, type_name: str, expected_ports: tuple[SwitchPortRow, ...]
) -> None:
    manufacturer, model = _manufacturer_model_from_table(table_name)
    try:
        switch_type = NetworkSwitchType.objects.get(manufacturer=manufacturer, model=model, name=type_name)
    except NetworkSwitchType.DoesNotExist:
        findings.fail("switch_port_matrix", f"No NetworkSwitchType {manufacturer} {model} — {type_name}.")
        return
    actual_ports = list(
        switch_type.type_ports.select_related("profile", "profile__native_vlan").order_by("port_number")
    )
    numbers = [p.port_number for p in actual_ports]
    if numbers != list(range(1, switch_type.port_count + 1)):
        findings.fail(
            "switch_port_matrix",
            f"{switch_type}: port numbers not contiguous 1..{switch_type.port_count}: {numbers}.",
        )
        return
    by_number = {p.port_number: p for p in actual_ports}
    for expected in expected_ports:
        actual = by_number.get(expected.port_number)
        if actual is None:
            findings.fail("switch_port_matrix", f"{switch_type} port {expected.port_number}: missing.")
            continue
        if actual.port_type != expected.port_type:
            findings.fail(
                "switch_port_matrix",
                f"{switch_type} port {expected.port_number}: port_type {actual.port_type} != "
                f"{expected.port_type}.",
            )
        profile = actual.profile
        if profile.port_mode != expected.mode:
            findings.fail(
                "switch_port_matrix",
                f"{switch_type} port {expected.port_number}: mode {profile.port_mode} != {expected.mode}.",
            )
        if profile.native_vlan.vlan_id != expected.native_vlan_id:
            findings.fail(
                "switch_port_matrix",
                f"{switch_type} port {expected.port_number}: native {profile.native_vlan.vlan_id} != "
                f"{expected.native_vlan_id}.",
            )
        actual_allowed = {v.vlan_id for v in profile.allowed_vlans.all()}
        if actual_allowed != set(expected.allowed_vlan_ids):
            findings.fail(
                "switch_port_matrix",
                f"{switch_type} port {expected.port_number}: allowed {sorted(actual_allowed)} != "
                f"{sorted(expected.allowed_vlan_ids)}.",
            )
    findings.count("switch_port_matrices_checked")


def _manufacturer_model_from_table(table_name: str) -> tuple[str, str]:
    match = re.match(r"^([\w.\- ]+?) ([\w.\-]+)(?: \(.*\))?$", table_name)
    if match is None:
        raise CommandError(f"Cannot read manufacturer/model out of {table_name!r}.")
    return match.group(1), match.group(2)


# -- Device type ports: count, VLAN, port_type, slot_offset ------------------------


def _check_device_type_ports(findings: _Findings, vlan_id_by_function: dict[str, int]) -> None:
    """Every ``NetworkDeviceType``'s port list against
    ``EXPECTED_DEVICE_TYPE_PORTS`` — count, VLAN, ``port_type`` *and*
    ``slot_offset``, per PLAN-prod-import.md's ``## Verification`` ("Device
    type ports — count, VLAN, port_type, and slot_offset"). Checking
    ``slot_offset`` alone would pass a wrong port count, VLAN, description
    or ``port_type`` in the importer's hardcoded catalog.

    "Count" is checked twice, deliberately: ``device_type.port_count`` (the
    stored field) against the expected count, *and* the number of actual
    ``NetworkDeviceTypePort`` rows against the expected count. The model's
    own ``_validate_device_type_port_profile()`` (``inventory/models.py``)
    already keeps these two in sync on every path that creates an instance
    — but this command's job is to verify what it advertises, not to lean
    on an upstream guarantee it never actually reads. A row-count match
    with a corrupted stored ``port_count`` is exactly the state that
    checking only ``len(actual_ports)`` sails past.
    """
    for device_type in NetworkDeviceType.objects.select_related("device_model").prefetch_related(
        "type_ports__vlan"
    ):
        identity = (device_type.device_model.manufacturer, device_type.device_model.model, device_type.name)
        expected_ports = EXPECTED_DEVICE_TYPE_PORTS.get(identity)
        if expected_ports is None:
            findings.fail(
                "device_type_ports", f"{device_type}: not in the independently-declared expected catalog."
            )
            continue
        actual_ports = list(device_type.type_ports.all())
        if device_type.port_count != len(expected_ports):
            findings.fail(
                "device_type_ports",
                f"{device_type}: stored port_count {device_type.port_count} != expected "
                f"{len(expected_ports)}.",
            )
        if len(actual_ports) != len(expected_ports):
            findings.fail(
                "device_type_ports",
                f"{device_type}: port count {len(actual_ports)} != expected {len(expected_ports)}.",
            )
            continue
        actual_by_description = {p.description: p for p in actual_ports}
        for description, function, expected_offset in expected_ports:
            actual = actual_by_description.get(description)
            if actual is None:
                findings.fail("device_type_ports", f"{device_type}: missing expected port {description!r}.")
                continue
            expected_vlan_id = vlan_id_by_function[function]
            if actual.vlan.vlan_id != expected_vlan_id:
                findings.fail(
                    "device_type_ports",
                    f"{device_type} / {description}: vlan {actual.vlan.vlan_id} != {expected_vlan_id}.",
                )
            if actual.port_type != EXPECTED_DEVICE_PORT_TYPE:
                findings.fail(
                    "device_type_ports",
                    f"{device_type} / {description}: port_type {actual.port_type} != "
                    f"{EXPECTED_DEVICE_PORT_TYPE}.",
                )
            if actual.slot_offset != expected_offset:
                findings.fail(
                    "device_type_ports",
                    f"{device_type} / {description}: slot_offset {actual.slot_offset} != {expected_offset}.",
                )
    findings.count("device_type_ports_checked", NetworkDeviceTypePort.objects.count())


# -- dhcp_server_enabled -------------------------------------------------------------


def _check_dhcp_server_enabled(findings: _Findings) -> None:
    for switch in NetworkSwitch.objects.select_related("rack"):
        expected = switch.rack is not None and switch.rack.name in DHCP_SERVER_RACKS
        if switch.dhcp_server_enabled != expected:
            findings.fail(
                "dhcp_server_enabled",
                f"{switch} (rack {switch.rack}): dhcp_server_enabled "
                f"{switch.dhcp_server_enabled} != {expected}.",
            )
    findings.count("switches_checked_for_dhcp_server", NetworkSwitch.objects.count())


# -- Cross-VLAN alignment ------------------------------------------------------------


def _check_cross_vlan_alignment(findings: _Findings) -> None:
    """Every device's static ports should share the same offset from their
    respective VLAN's network address (PROD-DATA-ANALYSIS.md §6.1) — a
    coincidence of per-VLAN first-fit here, not an enforced invariant, so
    this is exactly the kind of drift the import is the one cheap moment to
    catch.

    Excludes ``OPERATOR``-sourced ports (ADR 0022), resolved through
    ``source_type_port`` — a console's Device Control interface has no
    derivable relationship to the other ports' offset at all (production
    points *both ways*: a DM7C's Device Control sits below its own Dante
    Primary, a DM3's above), so including it here would make a correct
    import fail this check. A port with ``source_type_port=None`` stays
    included — the exemption is for ``OPERATOR`` specifically, not for
    "no type port on record," so a corrupted ordinary ``SLOT`` port on the
    same console still trips this.
    """
    for device in NetworkDevice.objects.prefetch_related("ports__vlan", "ports__source_type_port"):
        offsets: dict[int, str] = {}
        for port in device.ports.all():
            if port.address is None:
                continue
            source_type_port = port.source_type_port
            if source_type_port is not None and source_type_port.address_source == PortAddressSource.OPERATOR:
                continue
            network = ipaddress.IPv4Network(port.vlan.subnet, strict=True)
            offset = (
                int(ipaddress.IPv4Address(port.address)) - int(network.network_address) - port.slot_offset
            )
            offsets[offset] = f"{port.description} ({port.address})"
        if len(offsets) > 1:
            findings.fail("cross_vlan_alignment", f"{device}: inconsistent host offsets: {offsets}.")
    findings.count("devices_checked_for_alignment", NetworkDevice.objects.count())


# -- .255 / DHCP / gateway ------------------------------------------------------------


def _check_broadcast_dhcp_gateway(findings: _Findings, vlan_rows: list[Any]) -> None:
    dhcp_ranges: dict[int, tuple[str, str]] = {}
    gateways: dict[int, str] = {}
    for vlan in VLAN.objects.all():
        if vlan.dhcp_range_start and vlan.dhcp_range_end:
            dhcp_ranges[vlan.vlan_id] = (vlan.dhcp_range_start, vlan.dhcp_range_end)
        if vlan.default_gateway:
            gateways[vlan.vlan_id] = vlan.default_gateway

    checked = 0
    for address, vlan_id in list(NetworkSwitchAddress.objects.values_list("address", "vlan__vlan_id")) + list(
        NetworkDevicePort.objects.filter(address__isnull=False).values_list("address", "vlan__vlan_id")
    ):
        if address is None:
            continue  # DB CheckConstraint guarantees this can't happen; satisfies mypy
        checked += 1
        if address.endswith(".255"):
            findings.fail("broadcast_dhcp_gateway", f"{address} (VLAN {vlan_id}) ends in .255.")
        if vlan_id in gateways and address == gateways[vlan_id]:
            findings.fail("broadcast_dhcp_gateway", f"{address} (VLAN {vlan_id}) is the default gateway.")
        if vlan_id in dhcp_ranges:
            start, end = dhcp_ranges[vlan_id]
            if ipaddress.IPv4Address(start) <= ipaddress.IPv4Address(address) <= ipaddress.IPv4Address(end):
                findings.fail(
                    "broadcast_dhcp_gateway", f"{address} (VLAN {vlan_id}) falls inside the DHCP range."
                )
    findings.count("addresses_checked_for_255_dhcp_gateway", checked)
