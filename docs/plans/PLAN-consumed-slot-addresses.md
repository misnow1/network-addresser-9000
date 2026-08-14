# Show which addresses an operator-set port has consumed — issue #60

## Context

ADR 0019 makes the rack *the address pool*: an ordinal produces an address, `range_base + rack_slot`.
ADR 0022 PR 1's `address_source=OPERATOR` is the first thing in this model that can consume a pool
address **without occupying the slot that maps to it**, and PR 2 put real data into that shape.

`DM7C-1` sits at `CONSOLES` slot 5 and carries its Device Control interface at `10.201.6.4` — the
address belonging to slot **4**. Slot 4 is genuinely free as a *slot*, and the elevation renders it
`empty` with a greyed would-be address of `10.201.6.4` and a "+ add device" link. Put anything there
and the suggested address collides; `unique_device_port_vlan_address_value` refuses it at save.

Nothing is corrupted. The failure is that the system **offers a value it will then reject**, and the
page gives an operator no way to see why. Mike hit this reading a Yamaha console's page after #59
merged.

**Builds:** the two display fixes from #60 — a tag on operator-set ports in the device port table,
and an "address consumed" encoding on the affected elevation cells.

**Does not build:** issue **#62**, making the slot suggester skip those ordinals. That reverses a
position three plans have held deliberately (the allocator never consults address state) and runs
into ADR 0001/0019's suggest-don't-enforce stance, so it wants an ADR rather than a patch. This plan
makes the situation *visible*; #62 decides whether the system should also stop proposing it.

**Nothing here changes behaviour.** No model field, no migration, no validation, no suggester, no
allocator. Two templates, one view function, one dataclass, one model property, and CSS. If a diff
in this plan reaches `models.py` beyond a read-only property, or `suggestions.py` at all, something
has gone wrong.

## Decisions this plan settles

1. **The consumed marker is a new field on `ElevationCell`, not a new `state`.** The slot really is
   empty — no occupant claims it — and it is the *address* that is taken. `state` enumerates what
   occupies the cell; overloading it would conflate two different questions and force every existing
   `state == "empty"` assertion in `test_ui.py` to be re-read. `ElevationCell` gains
   `consumed_by: str | None`, populated only when `state == "empty"`.
2. **Detection is in-memory, from data the view already prefetches.** `rack_detail()` already loads
   every device with `ports__vlan` and every switch with `addresses__vlan` (`views.py:559-573`).
   Building a `{(vlan_id, address): occupant_label}` map from those costs **no additional queries**,
   which matters because `QueryBudgetTests` asserts this view's count is independent of rack size.
   A test must prove the count is unchanged, not merely bounded.
3. **Scope is the rack's own occupants.** An address held by a device in a *different* rack is not
   consulted. Rack ranges are disjoint per VLAN (`unique_rack_vlan_range`), so a foreign device
   holding an address inside this rack's range is already a data error that the import verifier and
   the save-time uniqueness check both catch — and reaching across racks would turn a free in-memory
   pass into a query per VLAN.
4. **The port tag reads `source_type_port__address_source`, and tolerates `NULL`.** A directly
   constructed port can have no `source_type_port` (`models.py:3949` already documents this case).
   A port with no type port is not operator-addressed and gets no tag.
5. **Row-level marking is included.** A cell-level marker alone is easy to miss when scanning a
   50-row elevation, and Mike's report was about scanning, not about one cell. `ElevationRow` gains
   a derived `has_consumed_address` so the row can carry a marker in the ordinal column.

## Model — `inventory/models.py`

One read-only property on `NetworkDevicePort`, beside the existing derived `hostname`:

```python
@property
def is_operator_addressed(self) -> bool:
    """Whether this port's address was supplied by an operator rather than
    computed from its device's ordinal (ADR 0022). Consumes a pool address
    that no rack slot accounts for — see issue #60.
    """
    type_port = self.source_type_port
    return type_port is not None and type_port.address_source == PortAddressSource.OPERATOR
```

Nothing else in `models.py` changes.

## View — `inventory/views.py`

`ElevationCell` gains `consumed_by: str | None = None`; `ElevationRow` gains
`has_consumed_address: bool = False` (or a property over its cells — implementer's choice, but it
must not re-walk the grid per row in a way that reintroduces an N² pass over a 50-row rack).

A new helper builds the taken map from the already-loaded occupants:

- every `NetworkDevicePort` with `address is not None` on each device in `rack.devices`
- every `NetworkSwitchAddress` with `address is not None` on each switch in `rack.switches`

keyed `(vlan_id, address)` → a short occupant label (the device/switch hostname or `str()`), and
`_empty_cell()` — or its caller, which has the map — sets `consumed_by` when
`(column.vlan_id, would_be_address)` is present.

**A cell whose `would_be_address` is held by the occupant of that very ordinal cannot arise**, since
that ordinal would not be `empty`. If it somehow does, the marker is still correct: the address is
taken.

`device_detail()` needs no query change — PR 1 already added `source_type_port` to its ports
prefetch, which is exactly what the new property reads. **Verify that**, and if it is not there, add
it rather than accepting an N+1.

## Templates and CSS

- `device_detail.html` (`:40-46`) — beside the existing `derived` tag, render an
  `operator-set` tag where `port.is_operator_addressed`, with a `title` explaining in operator
  language that this address was typed in rather than worked out from the slot, and that it
  therefore uses an address belonging to another slot. **No ADR references in on-screen copy**
  (`CONTEXT.md` convention); the rationale goes in a template comment.
- `rack_detail.html` — an empty cell with `consumed_by` renders its greyed would-be address plus a
  marker naming the taker, e.g. `10.201.6.4 — used by DM7C-1`. The row's ordinal cell carries a
  quieter marker when `has_consumed_address`.
- `na9k.css` — a `.tag-operator-set` beside the existing `.tag-derived` (`:357`), and a
  `.cell--consumed` treatment. Match the existing muted-tag visual language rather than inventing a
  new one; this is information, not an error, and must not read as a conflict — `row-conflict`
  already means something specific and stronger.

## Tests

`inventory/test_ui.py` unless noted.

- A console with an `OPERATOR` port renders the `operator-set` tag; an ordinary static port and a
  DHCP port do not; a port with `source_type_port = None` does not (settled decision 4).
- `inventory/tests.py`: `is_operator_addressed` is `True`/`False`/`False` for the three cases above.
- The elevation marks the exact ordinal whose address is consumed and **only** that one — a rack
  with one Device Control at `.4` marks slot 4 and leaves 1-3 and 6+ unmarked.
- The marker names the taking device.
- A rack with **no** operator-set ports renders no markers anywhere — the test that would fail if
  the map were built wrongly and matched everything.
- A consumed ordinal still renders `state == "empty"` and keeps its `would_be_address` (settled
  decision 1) — the assertion that proves the existing encoding was not disturbed.
- **Query count for `rack_detail` is unchanged**, asserted against the existing budget test's
  numbers, for both a 2-device and a 50-device rack (settled decision 2).
- An address consumed on one VLAN does not mark the same ordinal's cells on the rack's *other*
  VLANs.
- A switch address consuming an ordinal is marked too, not just a device port.

## Verification

```bash
set -a; source .env; set +a
python manage.py test inventory
python manage.py makemigrations --check --dry-run   # must report no changes: this plan adds no field
```

The production round-trip is **not** re-run — nothing here touches the importer, the verifier, or
any stored value, and re-running it rebuilds the dev database for no reason.

## Risks

**The query budget is the thing to watch.** The whole design rests on the taken map being free.
An implementer who reaches for `NetworkDevicePort.objects.filter(...)` instead of walking the
already-prefetched occupants will pass every functional test and quietly add a query per rack view.

**The elevation's encodings are load-bearing and documented.** `ElevationCell`'s five states each
guard a specific failure recorded in its docstring, and `ElevationEncodingTests` locks them in.
This adds an orthogonal axis; it must not perturb any existing state.

## Out of scope

Issue #62 (address-aware slot suggestion). Issue #41. Hostname work of any kind. Any change to what
the system *permits* — this plan only changes what it *shows*.
