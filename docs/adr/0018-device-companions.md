# Device companions: hardware that cannot exist without its host

A Yamaha DM7C or DM3 console reaches stage boxes for head-amp control through a second Dante
interface, addressed separately from the console's own Dante Primary address. The production
data has each one as its own row:

```
dm7c-1-device-control,CONSOLES,4,,10.201.6.4,,Only on Dante Primary for controlling snakes
DM7C-1,CONSOLES,5,10.200.6.5,10.201.6.5,10.202.6.5,
...
bej-dm3-1,CONSOLES,15,10.200.6.15,10.201.6.15,10.202.6.15,
bej-dm3-1-device-control,CONSOLES,16,,10.201.6.16,,
```

The import models these as separate `NetworkDevice`s on separate `NetworkDeviceType` profiles
(`"Yamaha DM7C — Device Control Interface"`, `"Yamaha DM3 — Device Control Interface"`), which
is correct and is what ADR 0017 requires. What is missing is that nothing connects them. A
console can be created with no interface; an interface can be created with no console; either
can be deleted without the other; and the only thing recording that `bej-dm3-1-device-control`
belongs to `bej-dm3-1` is the hostname, which enforces nothing and is not even a convention the
model knows about.

`slot_offset` (ADR 0017) is the wrong instrument here, and ADR 0017 says so by name in its
scope-boundary section. Its test is "does the hardware compute the second address from the
first and refuse to let anyone change it?" It does not: the operator types both values. The
production data settles it outright — **the DM7C's interface sits one address *below* its
console** (`10.201.6.4` against `10.201.6.5`) **and the DM3's one *above*** (`10.201.6.16`
against `10.201.6.15`). There is no offset to declare, because the hardware has no opinion.
Reaching for `slot_offset` anyway would impose a fixed, derived, read-only relationship on two
addresses that are independent in fact.

So the concept the model is missing is not an addressing one. It is **existence and lifecycle**:
this device cannot exist without that one.

## Decision

`NetworkDeviceType` gains `companion_type` — a self-referential `ForeignKey`, `null=True`,
`on_delete=PROTECT` — and `NetworkDevice` gains `host`, a self-referential `ForeignKey`,
`null=True`, `on_delete=CASCADE`. A type declaring a `companion_type` requires exactly one
companion device per instance; a type that is some other type's `companion_type` can never be
instantiated on its own.

1. **Creating the host creates the companion**, in the same transaction as the host and its
   ports. The add form carries the companion's rack slot (required when the host is racked, with
   no default) and hostname (prefilled from the host's, editable). Materialization lives in
   `NetworkDevice.save()` beside `_materialize_ports()` (`inventory/models.py:3043`), not in the
   form, so `objects.create()` behaves identically.

2. **The companion's addresses are its own.** Nothing is derived, nothing is offset, nothing is
   read-only. Its ports materialize from its own type exactly as any other device's do, and its
   slot may sit above or below its host's, or nowhere near it.

3. **Deleting the host deletes the companion; deleting the companion alone is refused**, with an
   error naming the host.

4. **The assembly moves as a unit.** The companion's rack and slot are host-managed — read-only
   on its own change form — and the host's change form carries a companion-slot field prefilled
   to preserve the current relative offset. Unracking the host unracks both. This moves rows
   between slots; it never recomputes an address.

5. **`companion_type` is locked once the type has instances**, like every other identity field on
   a type (ADR 0010). Chains and cycles are refused: a companion type may not itself declare a
   companion, and a type may not be its own.

6. **Existing production pairs are linked by data migration**, matched on the `-device-control`
   hostname suffix that produced them, rather than grandfathered as permanent orphans.

### The mutual requirement deadlocks, and materialization is the escape

State the rule plainly and it does not work: a console requires its interface, and an interface
may never stand alone, so **neither row can be created first**. Any design that leaves the
operator to create two rows in sequence has to legalise the orphan state in one direction, which
is precisely the state this ADR exists to forbid.

Creating both at once is the escape, and it is not a new idea in this codebase — it is ADR 0010's
seed-once materialization, applied one level up. A type already materializes its ports into an
instance at creation; a type now also materializes its companion device. The transaction boundary
is the one `NetworkDevice.save()` already opens, so a failure anywhere rolls back the host, its
ports, the companion, and the companion's ports together, satisfying ADR 0013's "never a
half-configured device" for the assembly as a whole rather than merely for each row in it.

The deletion rule needs an escape of exactly the same kind, for the same reason: guarding both
directions would make the pair undeletable. The asymmetry — cascade from the host, refuse from
the companion — is what breaks it, and the mechanism already exists. `NetworkDevicePort.delete()`
(`:3378`) and its queryset twin refuse to orphan an offset sibling while deliberately **not**
firing during the parent device's cascade, because Django's `Collector` issues child deletes
directly rather than through `model.delete()` (`:3163`). The companion guard is that pattern
verbatim, one table up.

Both halves of this design turn out to be the same lesson: every mutual invariant needs a
declared escape, or it becomes a rule that forbids its own satisfaction.

### What this asks of ADR 0007

ADR 0007 blocks removal of non-empty containers and unassigns leaf references rather than
cascading, "because this tool's core job is to prevent equipment from silently vanishing from the
inventory." A companion is neither a container nor a leaf reference, and the cascade in decision 3
runs directly at that stated motivation. It deserves an answer rather than a footnote.

The answer is that a companion is not independently tracked equipment. ADR 0007 protects things
that have a life of their own — a device that survives the switch it was plugged into, a rack's
worth of equipment that must be moved out deliberately before the rack goes. A device-control
interface has no such life: it was created by its host, it cannot be created without one, it
cannot be moved away from one, and an unassigned companion is not a recoverable state but the
orphan this ADR forbids. Unassigning it, ADR 0007's leaf treatment, would produce a row that no
subsequent operation could make valid again.

Nor is the removal silent, which is the specific harm ADR 0007 names. A real `CASCADE` FK puts the
companion on Django's delete-confirmation page by construction, and this app already renders
`_scary_warning.html` on every inventory delete confirmation. The operator is told exactly what is
going, before it goes.

The narrow extension, then: **a host is a container of its companion**, in the same sense a device
is already a container of its ports and cascades to them. ADR 0007's rule for equipment that can
stand alone is untouched.

### Why not `slot_offset`

ADR 0017 already answered this, and this section exists so the next person does not re-open it.
Its test is whether the hardware computes the second address from the first and refuses to let
anyone change it. For a console's audio engine, yes — always control + 1, assigned by the console
software, uneditable on the hardware. For a device-control interface, no: the operator sets it,
and production sets it in opposite directions on two consoles from the same manufacturer.

Using an offset would buy the lifecycle link at the price of a false constraint on the address —
and would drift `slot_offset` toward being a general multi-part-hardware mechanism, which ADR 0017
explicitly forbids and `CONTEXT.md` records. Companions are the mechanism for multi-part hardware;
`slot_offset` is the mechanism for derived addresses. They are orthogonal, and a device type may
use either, both, or neither.

### Scope boundary: this is existence, not addressing

Issue #42 ("Model two independent static addresses on one VLAN") describes the same hardware and
proposes the other cure: collapse the pair into one device whose type carries a second,
operator-settable port on the Dante Primary VLAN. **This ADR does not close #42 and does not
depend on it.** If #42 ever lands, the two consoles here would become single devices and their
companion links would fall away — that would supersede this decision for those types, not
contradict it, and companions would remain the answer wherever the two parts are genuinely two
pieces of trackable hardware with their own serial numbers.

A companion is also **not** an optional accessory. A DM7-EX extender is separate hardware, bought
and racked independently, and a DM7C without one is an ordinary console — so the EX is two
ordinary devices' worth of nothing new, exactly as ADR 0017 has it. Optionality already has a home
in this model: ADR 0010 type profiles. "A DM7C with device control" and "a DM7C without" are two
profile names, one declaring a `companion_type` and one not. Companions express *mandatory*
composition only; anything an operator may or may not have is a profile choice, not a link.

### What this costs the importer

`_stage9_devices()` (`inventory/management/commands/import_prod_data.py:887`) creates every device
flat, in row order, including `dm7c_devctrl` and `dm3_devctrl` as standalone rows. Under this
decision those two creations are refused as orphans, so **the importer must pair rows before
creating them.** This is recorded as a consequence, not offered as a choice.

The work is small and already patterned: the importer performs the same kind of lookahead to
collapse the SD12 `-Control`/`-Engine` pair (`:791`). A companion pass matches a
`<host>-device-control` row to its host, takes the host's slot for the host and the interface's
own slot for the companion — the CSV states both, in either direction, which is the point — and
emits one paired entry. `verify_prod_import.py` re-declares the expected catalog independently, by
design, so it needs the companion expectation stated there too rather than derived from the
importer.

## Follow-up

Implementation is a separate, independently reviewed plan. Coverage that plan must include:

- A host materializing its companion and both sets of ports in one transaction, and a failure
  anywhere rolling back the entire assembly.
- A companion type refused on the bare add-device page, and refused via `objects.create()`.
- Deleting a host removing both rows; deleting a companion alone refused through both
  `delete()` and `QuerySet.delete()`.
- A host move relocating both rows with every address unchanged, and
  `_validate_existing_addresses_still_fit()` still governing whether the move is allowed at all.
- `companion_type` locked once instances exist; a companion-of-a-companion refused; a
  self-referential companion refused.
- The migration linking both production pairs, and a no-op on an empty database.
- The importer producing linked pairs, and `verify_prod_import` asserting the link.
- Every device type without a `companion_type` creating exactly as it does today.

One open question for that plan, not for this ADR: whether the companion-slot field belongs on
the host's change form unconditionally or only when the rack is being changed. Both satisfy
decision 4; it is a form-ergonomics call best made against the actual admin.
