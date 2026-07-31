# Rack address blocks are never smaller than a /27

Production has always allocated a uniform 32-address block per rack, regardless of how
much equipment the rack holds. The spreadsheet this tool replaces carries the rule as a
`Rack Increment` column reading `32` for every rack, and its 21 racks bear it out: rack
bases sit at offsets 256, 288, 320, … 832 — every one 32 apart — while actual occupancy
across those racks ranges from 1 to 19 slots.

`prefix_length_for_capacity()` sizes a block strictly to `slot_count`, so a rack holding
two devices gets a `/30` and a rack holding five gets a `/29`. Replaying the production
racks through the current suggester at their honest occupancy reproduces **1 of 21**
production rack bases. Replaying them with a `/27` floor reproduces **19 of 21**
automatically, and 21 of 21 once the two deliberately-reserved racks below are entered by
hand. The floor is not a preference about tidiness — it is the rule the existing
production addressing is built on, and without it this tool cannot describe the network it
is meant to take over.

## Decision

`required_block_size(slot_count)` returns `max(slot_count + 2, 32)`.

That is the whole change. The floor belongs in `required_block_size` rather than as a
clamp inside `prefix_length_for_capacity` because there are three call sites and all three
need it:

- `prefix_length_for_capacity()` → `suggest_rack_vlan_range()`, which sizes the *suggested*
  next free block.
- `RackVlanRange._validate_range()` (`inventory/models.py:1151`), which rejects a
  hand-entered block too small for its rack.
- `Rack.clean()` (`inventory/models.py:937`), which refuses to grow a rack's `slot_count`
  past what its existing ranges can hold.

Flooring the arithmetic at its source means the suggester and both validators agree by
construction, with no second constant to keep in sync and no path that can allocate below
the floor.

## The floor is enforced, not merely suggested

A hand-entered `/30` or `/29` is rejected, not warned about. This is a deliberate
narrowing of ADR 0001 and should not be read as contradicting it: ADR 0001's decision was
that an admin chooses **where** a rack's block sits rather than having it derived from the
rack number, and that remains completely true — the two racks discussed below exist
precisely because an admin can place a block anywhere. This ADR constrains only **how
small** a block may be. Those are different freedoms, and production exercises the first
while never once exercising the second.

The alternative — floor the suggester and leave validation sized to `slot_count` — was
considered and declined. It would leave a footgun with no upside: a rack given a `/30` by
hand can never grow past two slots without re-addressing everything in it, and the
`slot_count` growth guard in `Rack.clean()` would be the thing that finally reported the
problem, long after the block was committed and equipment was addressed inside it. Nobody
wants a sub-`/27` block; there is no case to preserve the ability to create one.

## `slot_count` stays honest

An obvious way to get uniform `/27`s without touching any code is to set every rack's
`slot_count` to 30, since `required_block_size(30) == 32` already. That is rejected.
`slot_count` is an addressing-ordinal cap (`CONTEXT.md`), and inflating it to 30 on a rack
holding two devices would be a lie told to the schema to work around a missing rule —
destroying the `Rack.clean()` guard's meaning (every rack would accept equipment at any
ordinal up to 30) and making "how big is this rack" unanswerable from the data. Racks keep
their real slot counts; the floor lives in the block arithmetic where it belongs.

## The `.255` guarantee is unaffected — and is narrower than it looks

`required_block_size` reserves both a block's own base address (index 0) and its top index
so that no slot address reads as a block-relative broadcast, which is how `DESIGN.md`'s
"avoid octets of all 1s" guidance is satisfied structurally rather than by anyone
remembering it. The `/27` floor neither strengthens nor weakens this.

Worth recording because the production spreadsheet annotates exactly two racks with
"Contains an octet of all 1's, avoid .255", and they are precisely the two whose `/27`
block tops are `x.255` (bases `10.200.1.224` and `10.200.2.224`). That annotation is a
human remembering something this arithmetic already handles.

**But the guarantee only holds for blocks of `/24` or smaller**, and that limit was not
previously written down anywhere. A block that fits inside one `/24` contains exactly one
address ending `.255`, and it is the top index, which is reserved. A block spanning several
`/24`s contains several — a `/23` at `10.200.0.0` contains both `10.200.0.255` and
`10.200.1.255`, and only the latter is its top index. The former is an ordinary interior
address that `suggest_slot_address()` will hand to a slot.

Reaching that requires a rack with 255 or more slots (`required_block_size(255) == 257`,
which rounds to a `/23`). Nothing prevents one: `Rack.slot_count` is bounded below at 1 and
not bounded above at all. This is left open rather than fixed here — it is orthogonal to the
floor this ADR is about, no such rack exists or is plausible at this site, and closing it
means either capping `slot_count` at 254 or teaching the suggester to skip interior `.255`
addresses, which is a different decision with its own trade-offs. Recorded so the guarantee
isn't over-trusted later.

## What production still needs by hand

Two of the 21 racks sit behind deliberate reservations — `FLOATSWITCH` at offset 832 is
followed by `SHURE` at 1280 (a 14-increment gap) and then `CONSOLES` at 1536 (a further 8).
A first-fit suggester will never leave a gap, so these two ranges must be entered
manually. That is ADR 0001 working as intended, not a shortfall of this decision.

## Consequences for existing databases

Any `RackVlanRange` narrower than a `/27` created before this change becomes permanently
invalid: `Rack.clean()` will refuse every `slot_count` edit on its rack, because the
comparison at `inventory/models.py:937` now floors at 32 regardless of the rack's actual
slot count. There is no production data (see ADR 0010's migration-workflow note — local
databases are rebuilt, not migrated over), so no backfill is written. If that ever stops
being true, sub-`/27` ranges need widening before this change is applied.

## Follow-up

Implementation is a separate, independently reviewed plan, per this project's convention.
An earlier version of this section predicted five failing tests from reading the change;
that prediction was wrong. Applying the one-line change and running the suite shows only
three of the five actually fail. The list below is the measured result, corrected after
implementation, not the original prediction — it's kept here rather than silently fixed so
the gap between "reading a diff" and "running the suite" stays visible.

Genuinely failing:

- `inventory/tests.py:555` — `prefix_length_for_capacity(1) == 30` becomes `27`.
- `:564` — `prefix_length_for_capacity(3) == 29` becomes `27`. This test exists to prove
  the top address is reserved (a naive `slot_count + 1` rule would give a `/30`); it needed
  a rewrite, not a new expected number, since at slot count 3 the floor now decides the
  answer regardless of the reservation. Re-expressed above the floor: `slot_count=31` must
  give a `/26`, not the `/27` a naive `slot_count + 1` rule (with no top-address reservation)
  would give it.
- `:470` — an admin formset asserting a suggested `10.200.0.0/29` becomes `/27`.

Originally listed as failures, but they don't fail — they pass for the wrong reason, which
is worse than failing, because nothing draws attention to it:

- `:890` (`test_explicit_range_too_small_for_rack_slot_count_raises`) still raises
  `ValidationError` for a `/30` against a 4-slot rack, but now because the range is below
  the `/27` floor, not because it's too small for that rack's `slot_count` — the
  slot-count arm of `_validate_range()` is left with no coverage at all. Split into two
  tests: the original `/30` case (now asserting the floor's message), and a new case above
  the floor (a `/27` against a 40-slot rack, which needs 42 addresses) exercising the
  slot-count branch the original test was named for.
- `:968` (`test_increasing_slot_count_beyond_existing_range_capacity_raises`) builds a
  `/29` for a 4-slot rack via `RackVlanRange.objects.create()`, which skips
  `clean()`/`full_clean()` and therefore the floor — the fixture is now constructed in a
  state `full_clean()` would reject. The test's own assertion still passes, coincidentally,
  because growing the rack's `slot_count` further is refused either way. Restructured
  around a `/27`: a 4-slot rack with a `/27`, grown to 40 slots, is refused for the
  capacity reason the test is named for.

`:550` (`slot_count=30` → `/27`) and `:558` (`slot_count=62` → `/26`) are unaffected, both
being at or above the floor already.

One more bug, pre-existing and unrelated to this change but made permanent by it: `:859`
(`test_explicit_overlap_with_sibling_range_raises`) builds a `/28` for a **30-slot** rack.
`required_block_size(30)` was already `32` before this ADR (`30 + 2`), so the range was
already rejected for being too small, before ever reaching the overlap check the test is
named for — the bare `assertRaises(ValidationError)` hid this. The floor doesn't cause the
bug, but does make the fix mandatory: nudging the `/28` to a `/27` at the same base doesn't
work (`10.200.0.16/27` has host bits set, and `IPv4Network(..., strict=True)` rejects it
before any overlap check runs), so the range needs to both clear the size check and
genuinely overlap its sibling — e.g. a `/26` at the same base, which contains it.

New coverage added: the floor applied at every slot count below 30 (and pinned at 31,
where it stops applying); the floor enforced against a hand-entered sub-`/27` range; and
an end-to-end case replaying the production racks — 19 racks at honest slot counts, DHCP
occupying the bottom `/24`, asserting the 19 automatically-reproduced bases plus the
counter-assertion that a 20th rack keeps packing sequentially rather than jumping to either
of the two manually-reserved gaps — since that replay is the entire evidentiary basis for
this decision and should fail loudly if the arithmetic ever drifts.
