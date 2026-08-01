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

from inventory.models import (
    VLAN,
    NetworkDevice,
    NetworkDevicePort,
    NetworkDeviceTypePort,
    NetworkSwitch,
    NetworkSwitchAddress,
    NetworkSwitchType,
    Rack,
)
from inventory.suggestions import suggest_slot_address

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

ADDRESSING_CSV_NAME = "MPS Audio Network Standards - IP Addressing mk2.csv"
CALC_LOOKUPS_CSV_NAME = "MPS Audio Network Standards - IP Calc Lookups.csv"
SWITCH_PORTS_CSV_NAME = "MPS Audio Network Standards - Switch Ports.csv"

FN_CONTROL = "Audio Control"
FN_DANTE_PRIMARY = "Dante Primary"
FN_DANTE_SECONDARY = "Dante Secondary"

MANUAL_RANGE_RACKS = frozenset({"SHURE", "CONSOLES"})
DHCP_SERVER_RACKS = frozenset({"FOH Drive #1", "FOH Drive #2"})
NETGEAR_DESCRIPTION = "Netgear Managed Switch (For W8LM Rack)"

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

        vlan_id_by_function = {row.function: row.vlan_id for row in vlan_rows}
        vlan_subnet_by_id = {row.vlan_id: row.subnet for row in vlan_rows}

        findings = _Findings()
        _check_rack_ranges(findings, rack_offset_rows, vlan_rows)
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
        _check_device_type_ports(findings)
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
    max_slot_by_rack: dict[str, int] = {}
    for row in addressing_rows:
        max_slot_by_rack[row.rack] = max(max_slot_by_rack.get(row.rack, 0), row.slot)

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
    for d in NetworkDevice.objects.select_related("rack", "device_type"):
        if d.rack is not None and d.rack_slot is not None:
            actual_devices[(d.rack.name, d.rack_slot)] = d

    total_placed = (
        NetworkSwitchAddress.objects.count() + NetworkDevicePort.objects.filter(address__isnull=False).count()
    )
    findings.count("total_addresses_placed", total_placed)

    byte_identical = 0
    differs_by_design = 0
    dmi_dante_rack_slot: dict[str, int] = {}

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
            if switch.hostname != row.description:
                findings.fail(
                    "hostnames",
                    f"{row.rack} slot {row.slot}: hostname {switch.hostname!r} != {row.description!r}.",
                )
            expected_type_name = _expected_switch_type_name(row.description)
            if switch.switch_type.name != expected_type_name:
                findings.fail(
                    "type_assignment",
                    f"{row.rack} slot {row.slot}: switch type name {switch.switch_type.name!r} != "
                    f"{expected_type_name!r}.",
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
                    if actual_value == sheet_value:
                        byte_identical += 1
                    else:
                        findings.fail(
                            "address_diff",
                            f"switch {row.rack}/{row.slot} {function}: sheet {sheet_value}, "
                            f"got {actual_value}.",
                        )
                # else: sheet leaves this column blank (the "extra" switch
                # address §8 documents) — nothing to compare against.
            continue

        # -- device rows ------------------------------------------------------
        if row.description.endswith("-Control"):
            stem = row.description[: -len("-Control")]
            engine_key = (row.rack, row.slot + 1)
            engine_row = by_key.get(engine_key)
            if engine_row is not None and engine_row.description == f"{stem}-Engine":
                consumed.add(key)
                consumed.add(engine_key)
                device = actual_devices.get(key)
                if device is None:
                    findings.fail(
                        "devices", f"{row.rack} slot {row.slot}: expected SD12 device {stem!r}, none found."
                    )
                    continue
                if device.hostname != stem:
                    findings.fail(
                        "hostnames", f"{row.rack} slot {row.slot}: hostname {device.hostname!r} != {stem!r}."
                    )
                control_vlan = vlan_id_by_function[FN_CONTROL]
                control_actual = _device_address(device, control_vlan, offset=0)
                engine_actual = _device_address(device, control_vlan, offset=1)
                byte_identical += _compare(
                    findings, "address_diff", f"{stem} Control", row.control, control_actual
                )
                byte_identical += _compare(
                    findings, "address_diff", f"{stem} Engine", engine_row.control, engine_actual
                )
                if row.dante_primary or row.dante_secondary:
                    dmi_dante_rack_slot[row.rack] = max_slot_by_rack[row.rack] + 1
                continue

        # ordinary device row
        consumed.add(key)
        device = actual_devices.get(key)
        if device is None:
            findings.fail(
                "devices", f"{row.rack} slot {row.slot}: expected device {row.description!r}, none found."
            )
            continue
        if device.hostname != row.description:
            findings.fail(
                "hostnames",
                f"{row.rack} slot {row.slot}: hostname {device.hostname!r} != {row.description!r}.",
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
            vlan_id = vlan_id_by_function[function]
            actual_value = _device_address(device, vlan_id, offset=0)
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
        rack_obj = Rack.objects.get(name=rack)
        for function in (FN_DANTE_PRIMARY, FN_DANTE_SECONDARY):
            vlan_id = vlan_id_by_function[function]
            rack_range = rack_obj.vlan_ranges.filter(vlan__vlan_id=vlan_id).first()
            if rack_range is None:
                findings.fail("address_diff", f"DMI-DANTE {rack}: no RackVlanRange for {function}.")
                continue
            expected = suggest_slot_address(rack_range.address_range, slot)
            actual_value = _device_address(device, vlan_id, offset=0)
            if actual_value != expected:
                findings.fail(
                    "address_diff",
                    f"DMI-DANTE {rack}/{slot} {function}: expected {expected}, got {actual_value}.",
                )
            else:
                differs_by_design += 1

    findings.count("byte_identical", byte_identical)
    findings.count("differs_by_design", differs_by_design)
    findings.count("extra_unrecorded_switch_addresses", total_placed - byte_identical - differs_by_design)


def _device_address(device: NetworkDevice, vlan_id: int, *, offset: int) -> str | None:
    port = device.ports.filter(vlan__vlan_id=vlan_id, slot_offset=offset).first()
    return port.address if port is not None else None


def _compare(findings: _Findings, check: str, label: str, expected: str, actual: str | None) -> int:
    if actual is None:
        findings.fail(check, f"{label}: expected {expected}, found no address.")
        return 0
    if actual != expected:
        findings.fail(check, f"{label}: expected {expected}, got {actual}.")
        return 0
    return 1


def _expected_switch_type_name(description: str) -> str:
    if description == "Spare SG300-26P":
        return "Default"
    if _TLSG108E_RE.match(description):
        return "Default"
    match = re.search(r"\(([^)]+)\)$", description)
    if match is None:
        raise CommandError(f"Cannot read a type name out of switch description {description!r}.")
    return match.group(1)


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


# -- Device type ports, incl. slot_offset ------------------------------------------


def _check_device_type_ports(findings: _Findings) -> None:
    """Every ``NetworkDeviceTypePort`` should carry ``slot_offset=0`` except
    the DiGiCo SD12's Engine port (offset 1) — ADR 0017's only offset in
    this dataset.
    """
    for type_port in NetworkDeviceTypePort.objects.select_related("device_type"):
        is_sd12_engine = type_port.device_type.model == "SD12" and type_port.description == "Engine"
        expected_offset = 1 if is_sd12_engine else 0
        if type_port.slot_offset != expected_offset:
            findings.fail(
                "device_type_ports",
                f"{type_port.device_type} / {type_port.description}: slot_offset "
                f"{type_port.slot_offset} != {expected_offset}.",
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
    """
    for device in NetworkDevice.objects.prefetch_related("ports__vlan"):
        offsets: dict[int, str] = {}
        for port in device.ports.all():
            if port.address is None:
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
