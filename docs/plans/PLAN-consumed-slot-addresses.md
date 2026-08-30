> **Superseded, 2026-08-29.** ADR 0027 (`docs/adr/0027-the-ordinal-is-the-unit.md`) makes every
> static device-port address derived from the device's own rack slot, which makes decisions 1-4 of
> this plan, its `## View`, `## Templates and CSS` and `## Tests` sections, and review notes 1, 2, 4,
> 5, 6 and 8 moot: the state this plan's marker exists to surface — an empty ordinal whose would-be
> address is already held by another *device* — is no longer reachable through the admin. **PR 1**
> (`docs/plans/PLAN-adr-0027.md`) built the derivation; its decision 5 and this plan's
> `is_operator_addressed` property and `operator-set` tag went with it, without a note left here at
> the time. **PR 2** deleted the marker itself — `taken_by`, `taken-by-label`, `cell-taken`,
> `tag-address-taken`, `_build_taken_address_map()`, `ElevationRow.has_taken_address`, and
> `TakenAddressMarkerTests` — since the state it existed to show no longer arises for the case it
> was built for. The gap is **not** fully closed: `NetworkSwitchAddress` is untouched by ADR 0027
> and can still be hand-set to another ordinal's address (issue **#103**, ADR 0027's amended
> consequence bullet). This document is kept for its historical decisions and review record, not as
> a live spec.

> **Revision 2** — incorporates review notes from `REVIEW-1-PLAN-consumed-slot-addresses.md`.
> See "Review response" for the mapping.

# Show which addresses are already taken — issue #60

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

**Rev 2 widens what this is for** (review note 2). Operator-set ports are not the only way an
ordinal's address gets taken: an ordinary `SLOT` port's `address` stays editable after creation
(`models.py:3791`, ADR 0003's stored-not-immutable rule), so a hand-moved ordinary address consumes
another ordinal exactly the same way. The elevation marker is therefore about **"this ordinal's
address is already held"**, whoever holds it and for whatever reason. Issue #60 described the
operator-set case because that is the one Mike found; the honest implementation covers both, and the
rev-1 test asserting "no operator-set ports ⇒ no markers" would have failed a correct build.

**Builds:** a tag on operator-set ports in the device port table, and an "address already taken"
encoding on affected elevation cells and rows.

**Does not build:** issue **#62**, making the slot suggester skip those ordinals. That reverses a
position three plans have held deliberately and wants an ADR. This plan makes the situation
*visible*; #62 decides whether the system should stop proposing it. **A marked ordinal keeps its
"+ add device" link** — removing it would be #62's decision, taken here by accident.

**Nothing here changes behaviour.** No model field, no migration, no validation, no suggester, no
allocator. Two templates, one view function, two dataclasses, one model property, and CSS.

## Decisions this plan settles

1. **The marker is a new field on `ElevationCell`, not a new `state`.** The slot really is empty —
   no occupant claims it — and it is the *address* that is taken. `state` enumerates what occupies
   the cell; overloading it would conflate two questions and force every existing `state == "empty"`
   assertion in `test_ui.py` to be re-read. `ElevationCell` gains `taken_by: list[str]`, populated
   only when `state == "empty"`.
2. **A list, not a single label** (review note 8). Device-port and switch-address uniqueness are
   separate constraints (`models.py:2528`, `:3874`) and cross-table uniqueness is only a
   validation-time guard (`:472`), so a bypassed write can leave both a switch and a device holding
   one address. A dict of single labels would report whichever was written last. Sort the labels for
   deterministic rendering.
3. **Detection indexes every static address in the rack, not only `OPERATOR` ones** (review note 2,
   above). The rule is "is this ordinal's would-be address already held by somebody?"
4. **Detection is in-memory, from data the view already prefetches.** `rack_detail()` loads every
   device with `ports__vlan` and every switch with `addresses__vlan` (`views.py:559-573`). Labels
   must come from the **enclosing device/switch** — their hostnames are already-loaded scalars.
   **Do not touch `port.source_type_port` while building this map**: that relation is *not*
   prefetched here (it is prefetched in `device_detail()`, `views.py:871`, which is a different
   view) and reading it is an N+1.
5. **`is_operator_addressed` describes the current address, not the type's provenance** (review note
   3). ADR 0022 permits an `OPERATOR` type port to materialize DHCP on an unracked device
   (`models.py:3503`, "on an unracked/DHCP device it materializes DHCP like any other port"). Such a
   port typed no address and consumed nothing, so it must not carry the tag. The predicate requires
   a non-DHCP port with a non-null address **and** an `OPERATOR` source.
6. **Scope is the rack's own occupants.** An address held by a device in another rack is not
   consulted — reaching across racks turns a free in-memory pass into a query per VLAN, and a
   foreign device inside this rack's range is a data error the verifier and the save-time check
   already catch. (Note for accuracy, review note 9: `unique_rack_vlan_range` guarantees one range
   per `(rack, vlan)`; range *disjointness* is enforced in `clean()` at `models.py:1350`, not by the
   constraint.)

## Model — `inventory/models.py`

One read-only property on `NetworkDevicePort`, beside the existing derived `hostname`:

```python
@property
def is_operator_addressed(self) -> bool:
    """Whether this port is *currently holding* an address an operator
    typed rather than one computed from its device's ordinal (ADR 0022).

    False for a DHCP port even when its type port is ``OPERATOR``-sourced:
    such a port materialized DHCP (unracked device) and consumed no
    address, so nothing about it is operator-set yet. See issue #60.
    """
    if self.is_dhcp or self.address is None:
        return False
    type_port = self.source_type_port
    return type_port is not None and type_port.address_source == PortAddressSource.OPERATOR
```

A null `source_type_port` (a directly constructed port — `models.py:3949`) is not operator-addressed.
Nothing else in `models.py` changes.

## View — `inventory/views.py`

`ElevationCell` gains `taken_by: list[str] = field(default_factory=list)`; `ElevationRow` gains a
`has_taken_address` derived from its own cells (no second walk of the grid).

A helper builds `{(vlan_id, address): [labels]}` from the already-loaded occupants:

- every `NetworkDevicePort` on `rack.devices` with `address is not None`
- every `NetworkSwitchAddress` on `rack.switches` with `address is not None`

labelled from the enclosing device/switch (settled decision 4). `_empty_cell()` — or its caller,
which holds the map — sets `taken_by` when `(column.vlan_id, would_be_address)` is present.

**Only `empty` cells get `taken_by`.** `occupied`, `absent`, `blank` and `conflict` are built through
distinct branches (`views.py:451`) and must be left exactly as they are (review note 4).

## Templates and CSS

- `device_detail.html` — beside the existing `derived` tag at **`:74`** (review note 9; rev 1 cited
  `:40-46`, which PR 3 turned into the fitted-cards table), render an `operator-set` tag where
  `port.is_operator_addressed`. Copy per review note 7: **"Entered by hand rather than calculated
  from the slot."** Rev 1's "uses an address belonging to another slot" is a false categorical claim
  — validation requires only containment in the rack range (`models.py:423`), so such an address may
  equal its own ordinal's or fall outside `1..slot_count` entirely. No ADR references in on-screen
  copy (`CONTEXT.md` convention); rationale goes in a template comment.
- `rack_detail.html` — an empty cell with `taken_by` renders its greyed would-be address plus
  **"address used by DM7C-1"** (not bare "used by": the *slot* is free and still offers
  "+ add device"). Multiple holders render all labels, sorted. The row's ordinal cell carries a
  quieter marker when `has_taken_address`.
- `na9k.css` — `.tag-operator-set` beside `.tag-derived` (`:357`), and a cell treatment. Match the
  existing muted-tag language: this is information, not an error, and must not read as a conflict —
  `row-conflict` already means something specific and stronger.

## Tests

`inventory/test_ui.py` unless noted.

**The property** (`inventory/tests.py`): true for a static `OPERATOR` port; false for an ordinary
static `SLOT` port; false for a `SLOT` DHCP port; **false for an `OPERATOR`-sourced port that
materialized DHCP** (review note 3 — the case rev 1 missed, and the one that distinguishes
provenance from current state); false for a port with `source_type_port = None`.

**The tag**: rendered for the first case above, absent for each of the others.

**The elevation**, using a rack range with a **non-zero network base such as `/27` at `.32`**
(review note 5 — catches an implementation deriving the ordinal from the address's last octet
instead of matching `(vlan_id, would_be_address)`):

- The `.4`-held-by-slot-5 scenario marks **slot 4 on both axes** (cell and row) and marks **neither
  axis at slot 5**, nor at 1-3 or 6+.
- The marker names the holding device; two holders of one address render both labels, sorted.
- **A hand-moved ordinary `SLOT` address produces a marker** (review note 2) — the positive test for
  decision 3.
- A rack whose addresses all sit on their own ordinals renders no markers anywhere.
- **Existing states are untouched:** a taken address landing on an ADR 0017 continuation ordinal, on
  an ordinal occupied by a switch, and on a switch/device same-slot conflict each keep their
  existing state and receive **no** marker (review note 4).
- A marked ordinal still renders its `+ add device` link and still keeps `state == "empty"` and its
  `would_be_address` (review note 6, and settled decision 1).
- **Negative boundaries** (review note 6): a DHCP port (`address is None`), a device port on a VLAN
  the rack has no range for, an address outside the rack's range, and an operator-set address equal
  to its own holder's slot-derived address — none marks an empty cell.
- An address taken on one VLAN does not mark that ordinal's cells on the rack's other VLANs.

**Query budget** (review note 1): the existing test at `test_ui.py:1235` only compares a 2-device
rack against a 50-device one, so **one new constant query would pass it** — there are no "existing
numbers" to assert against, as rev 1 wrongly claimed. Record the pre-change absolute count with
`assertNumQueries` and assert the post-change count is **identical**, for both rack sizes.

## Verification

```bash
set -a; source .env; set +a
python manage.py test inventory
python manage.py makemigrations --check --dry-run   # must report no changes: this plan adds no field
```

The production round-trip is **not** re-run — nothing here touches the importer, the verifier, or
any stored value, and re-running it rebuilds the dev database for no reason.

## Review response

| Note | Resolution | Section |
|---|---|---|
| 1 (P1) | Accepted — confirmed `test_ui.py:1235` compares two rack sizes and asserts no absolute number, so a constant extra query passes it. The plan now requires a recorded pre-change count asserted as identical. Also records the N+1 trap explicitly: `source_type_port` is prefetched in `device_detail()`, **not** in `rack_detail()`. | Decision 4; Tests |
| 2 (P1) | Accepted, and it widens the feature. An ordinary `SLOT` address stays editable (`models.py:3791`), so a hand-moved one consumes an ordinal identically. Rev 1's "no operator-set ports ⇒ no markers" test would have failed a correct implementation. The rule is now "is this ordinal's address held", with a positive test for the ordinary case. | Context; Decision 3; Tests |
| 3 (P1) | Accepted — confirmed at `models.py:3503` that an `OPERATOR` type port materializes DHCP on an unracked device. The property now describes the *current address*, not the type's provenance, and the test covers the `OPERATOR`-sourced-but-DHCP case rev 1 missed. | Decision 5; Model; Tests |
| 4 (P2) | Accepted. `occupied`/`absent`/`blank`/`conflict` each get an explicit no-marker test, including a taken address landing on an ADR 0017 continuation ordinal and on a switch/device conflict. | View; Tests |
| 5 (P2) | Accepted. Both axes asserted at slot 4 and both asserted absent at slot 5, on a non-zero network base so a last-octet shortcut fails. | Tests |
| 6 (P2) | Accepted. Four negative boundaries added, plus an assertion that the `+ add device` link survives — hiding it would quietly take #62's decision. | Tests |
| 7 (P2) | Accepted. "Belonging to another slot" was a false categorical claim; copy is now "entered by hand rather than calculated from the slot" and "address used by …". | Templates and CSS |
| 8 (P2) | Accepted. `taken_by` is a sorted list, since cross-table address uniqueness is validation-time only and a bypassed write can produce two holders. | Decision 2 |
| 9 (P3) | Accepted. `device_detail.html:40-46` corrected to `:74` (PR 3 moved it), and the `unique_rack_vlan_range` description corrected — disjointness is a `clean()` check at `models.py:1350`, not a constraint. | Templates; Decision 6 |

No finding was rejected. Notes 2 and 3 changed the plan's substance: one widened what the marker
means, the other stopped the tag lying about a DHCP port.

## Risks

**The query budget is the thing to watch.** The design rests on the taken map being free. An
implementer who reaches for a `NetworkDevicePort.objects.filter(...)`, or who touches
`source_type_port` while building it, passes every functional test and quietly adds queries.

**The elevation's encodings are load-bearing and documented.** `ElevationCell`'s five states each
guard a specific failure recorded in its docstring, and `ElevationEncodingTests` locks them in. This
adds an orthogonal axis; it must not perturb any existing state.

## Out of scope

Issue #62 (address-aware slot suggestion) — including any change to the `+ add device` link. Issue
#41. Hostname work. Any change to what the system *permits* or *suggests* — this plan changes only
what it *shows*.
