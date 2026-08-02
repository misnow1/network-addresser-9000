"""Pure CSV-parsing helpers for the production-data importer and verifier.

Deliberately dumb: everything here turns raw CSV rows into typed rows and
does no domain-level judgement (which rows are switches vs. devices, which
addresses are spurious, how a secondary switch's ports derive from its
primary). That judgement lives separately in ``import_prod_data.py`` and
(independently re-implemented, not imported from here) in
``verify_prod_import.py`` — see ``PLAN-prod-import.md``'s "the verification
oracle must be independent of the importer". Sharing pure I/O/parsing
between the two is fine; sharing the derivation itself would not be.

No Django imports here on purpose — these helpers touch no models and no
database, so they're trivially unit-testable against small in-memory CSV
text.
"""

import csv
import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Addressing CSV's "Port Type"/"Device Description" free-text -> the
#: structured ``PortType`` choice values in ``inventory.models``. Copied as
#: plain strings (not the ``PortType`` enum) so this module stays free of
#: Django/model imports.
PORT_TYPE_BY_LABEL: dict[str, str] = {
    "1GbE Copper": "1gbe_rj45",
    "Combo Port (1GbE + SFP)": "1gbe_combo",
}

#: Addressing CSV's "Mode" column -> ``PortMode`` choice values.
PORT_MODE_BY_LABEL: dict[str, str] = {
    "Access": "access",
    "Trunk": "trunk",
}

_LEADING_INT = re.compile(r"^\s*(\d+)")


def read_csv_rows(path: Path) -> list[list[str]]:
    """Every row of ``path`` as a list of stripped-of-nothing-but-newline strings.

    Uses ``newline=""`` per the ``csv`` module's own recommendation so
    embedded commas/quotes in a cell (this export has none, but a future
    edit might) are handled correctly rather than by accident.
    """
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


@dataclass(frozen=True)
class VlanRow:
    """One row of the Calc Lookups CSV's VLAN table (columns A-D)."""

    function: str
    vlan_id: int
    subnet: str  # CIDR, e.g. "10.200.0.0/21"


@dataclass(frozen=True)
class RackOffsetRow:
    """One row of the Calc Lookups CSV's rack table (columns E-F)."""

    rack: str
    offset: int


@dataclass(frozen=True)
class AddressingRow:
    """One data row of the IP Addressing CSV, before any dedup/collapse."""

    description: str
    rack: str
    slot: int
    control: str
    dante_primary: str
    dante_secondary: str
    notes: str


@dataclass(frozen=True)
class SwitchPortRow:
    """One data row of a Switch Ports CSV table."""

    description: str
    port_number: int
    native_vlan_id: int
    mode: str  # "access" or "trunk"
    allowed_vlan_ids: frozenset[int]
    port_type: str  # PortType choice value
    note: str


@dataclass(frozen=True)
class SwitchPortTable:
    """One named table (one switch profile's worth of ports) from the
    Switch Ports CSV.
    """

    name: str
    ports: "tuple[SwitchPortRow, ...]" = field(default_factory=tuple)


def parse_vlan_table(rows: list[list[str]]) -> list[VlanRow]:
    """The Calc Lookups CSV's VLAN table (columns A-D), in file order."""
    result = []
    for row in rows[2:]:  # row 0 is blank, row 1 is the header
        if len(row) < 4:
            continue
        function, vlan_id_text, base, netmask = row[0], row[1], row[2], row[3]
        if not (function and vlan_id_text and base and netmask):
            continue
        subnet = str(ipaddress.ip_network(f"{base}/{netmask}", strict=True))
        result.append(VlanRow(function=function.strip(), vlan_id=int(vlan_id_text), subnet=subnet))
    return result


def parse_rack_offsets(rows: list[list[str]]) -> list[RackOffsetRow]:
    """The Calc Lookups CSV's rack table (columns E-F), sorted ascending by
    offset — the order ``PLAN-prod-import.md`` requires racks be created in,
    since ``suggest_rack_vlan_range()`` is first-fit and creation order is
    load-bearing.
    """
    result = []
    for row in rows[2:]:
        if len(row) < 6:
            continue
        rack, offset_text = row[4], row[5]
        if not (rack and offset_text):
            continue
        result.append(RackOffsetRow(rack=rack.strip(), offset=int(offset_text)))
    return sorted(result, key=lambda r: r.offset)


def parse_addressing_rows(rows: list[list[str]]) -> list[AddressingRow]:
    """Every data row of the IP Addressing CSV with a rack assigned.

    Skips blank spacer rows and any row with no rack (e.g. the unracked
    "Used by BEJ for his gear" ad-hoc row — settled decision #10: ignored,
    it has no rack or slot to key off). Does **not** dedupe repeated
    ``(rack, slot)`` rows or collapse anything — that's the importer's job,
    since what counts as "the same occupant" is a domain judgement, not a
    parsing one.
    """
    result = []
    for row in rows[1:]:  # row 0 is the header
        if len(row) < 6:
            continue
        description, rack, slot_text = row[0].strip(), row[1].strip(), row[2].strip()
        if not rack or not slot_text:
            continue
        control, dante_primary, dante_secondary = row[3].strip(), row[4].strip(), row[5].strip()
        notes = row[6].strip() if len(row) > 6 else ""
        result.append(
            AddressingRow(
                description=description,
                rack=rack,
                slot=int(slot_text),
                control=control,
                dante_primary=dante_primary,
                dante_secondary=dante_secondary,
                notes=notes,
            )
        )
    return result


def parse_native_vlan(text: str) -> int:
    """``"200 (Control)"`` -> ``200``."""
    match = _LEADING_INT.match(text)
    if not match:
        raise ValueError(f"cannot parse a VLAN id out of {text!r}")
    return int(match.group(1))


def parse_allowed_vlans(text: str) -> frozenset[int]:
    """``"200, 202-207"`` -> ``frozenset({200, 202, 203, 204, 205, 206, 207})``.

    Blank text (an access port, or a trunk with no extra allowed VLANs)
    yields an empty set. Ranges (``"202-207"``) are inclusive on both ends.
    """
    if not text.strip():
        return frozenset()
    ids: set[int] = set()
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            ids.update(range(int(start_text.strip()), int(end_text.strip()) + 1))
        else:
            ids.add(int(token))
    return frozenset(ids)


def parse_switch_port_tables(rows: list[list[str]]) -> list[SwitchPortTable]:
    """Every named table in the Switch Ports CSV, each holding its ports in
    file order.

    The file's layout is irregular by hand-editing history: the *first*
    table's name rides in the header row's own trailing column, but every
    later table gets a dedicated name-only row immediately before its
    header. Both shapes are handled explicitly rather than assumed uniform.
    """
    tables: list[SwitchPortTable] = []
    current_name: str | None = None
    current_ports: list[SwitchPortRow] = []
    pending_name: str | None = None

    def _flush() -> None:
        if current_name is not None:
            tables.append(SwitchPortTable(name=current_name, ports=tuple(current_ports)))

    for row in rows:
        padded = row + [""] * max(0, 9 - len(row))
        description = padded[0].strip()
        trailing = padded[8].strip()

        if description == "" and trailing and all(not c.strip() for c in padded[1:8]):
            # A name-only marker row, ahead of a later table's header.
            pending_name = trailing
            continue
        if description == "Description":
            # A header row, possibly also embedding this table's own name
            # (only the very first table in the file does this).
            _flush()
            current_name = pending_name if pending_name is not None else trailing
            pending_name = None
            current_ports = []
            continue
        if description == "":
            continue  # blank separator row between tables

        allowed_text = padded[4]
        current_ports.append(
            SwitchPortRow(
                description=description,
                port_number=int(padded[1]),
                native_vlan_id=parse_native_vlan(padded[2]),
                mode=PORT_MODE_BY_LABEL[padded[3].strip()],
                allowed_vlan_ids=parse_allowed_vlans(allowed_text),
                port_type=PORT_TYPE_BY_LABEL[padded[5].strip()],
                note=padded[6].strip(),
            )
        )
    _flush()
    return tables
