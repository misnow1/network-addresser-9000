# Read-only UI: what to build next

**Survey, not a plan.** Deliberately named `PRIORITIES-` rather than `PLAN-` so
`/plan-cycle` doesn't mistake it for an implementable plan — it has no `## Verification`
section and no single topic. Each tier-1 and tier-2 item below still needs its own
`PLAN-*.md` (or an ADR first, where noted) before anything gets built.

Written 2026-08-25 against `main` at d10207e, with no PRs open.

> **Tier 1 was overtaken on the day this was written.** Grilling #84 found that its behaviour
> was a *recorded decision* (`PLAN-consumed-slot-addresses.md` decision 1, guarded by a test at
> `test_ui.py:1149`) rather than an oversight. Pulling that thread produced **ADR 0027 — the
> ordinal is the unit**, which closes #84, #83, #62 and #28 together by deriving every static
> address from its rack slot. See `docs/adr/0027-the-ordinal-is-the-unit.md` and
> `PLAN-adr-0027.md`. Tier 1 below is kept as written, because its diagnosis is what produced
> the ADR; the **order has been revised**. Tiers 2 and 3 are unaffected.

## Scope

Everything here is **user-facing and read-only**: the purpose-built UI at `/` plus field
help text. Explicitly excluded:

- Anything behind ADR 0020 decision 2 (*"no forms, no `POST` handlers, no validation, no
  audit-trail plumbing of its own"*) — the write surface, the rack creation wizard, the
  password change form, and user documentation. See ROADMAP "Later / not yet designed".
- Phase 20 and 21 model work, and the model-shaped issues (#78, #79, #80).
- Admin-side issues (#6, #25, #28, #62) — the admin is not the read-only UI.

That filter leaves six items out of the 29 open issues, plus one entry from the ROADMAP's
"Design deferred" section.

## Suggested order

1. **ADR 0027** — closes #84, #83, #62 and #28 as one coherent change, in the PR sequence
   `PLAN-adr-0027.md` sets out. Absorbs everything tier 1 originally listed.
2. #75 — clickable IP addresses
3. Device model description in three views, and #81's help text
4. A design pass on the rack elevation cell, covering #82 and **#93** (the tether encoding
   ADR 0027 defers)

Rationale, revised: the two tier-1 bugs turned out to be one incoherence seen from two angles,
so they are fixed once at the model rather than twice at the surface. After that, the item
costing you time daily; then everything needing a layout decision, in one pass instead of three.

The original rationale — front-load what is *wrong* about views people already use — still
holds. ADR 0027 is that work; it just goes deeper than the issues suggested.

---

## Tier 1 — fix what's already shipped and wrong

### #84 — "add device" link on a slot whose address is already taken

**Do this one first.** Genuinely small.

`_build_elevation_rows` (`inventory/views.py:546`) attaches `add_url` to any ordinal with
no *occupant*, while `_empty_cell` (`views.py:438`) separately computes `taken_by` for the
same row. The template therefore prints "address used by …" (`rack_detail.html:105`)
directly above a "+ add device" link (`rack_detail.html:92`).

Fix: suppress `add_url` when any cell in the row has `taken_by`. Roughly five lines plus
tests.

This is the Yamaha console case from the issue — the Device Control interface became a
port with `address_source=OPERATOR` (phase 17, ADR 0022 PR 2), which forces `slot_offset=0`
(`models.py:306`), so the console occupies one ordinal while its hand-set control address
lands on a *different* ordinal's address. That ordinal is genuinely unoccupied and
genuinely unusable, which is exactly the state `taken_by` already detects.

### #83 — devices with `slot_offset > 1` block every intervening ordinal

Labeled `area:frontend`; **it isn't**. This is a model bug with a UI symptom.

`NetworkDeviceType.slot_span` is `max(slot_offset) + 1` (`models.py:3659`), and that same
expression is the write-path occupancy check in two more places (`models.py:388`,
`models.py:3229`). So Shure receivers at offset 64 don't just *render* `CONT'D` across
.1–.64 — they reserve those ordinals, and `clean()` will refuse anything placed in them.
A placement blocker on live data.

**Root cause:** `slot_span` conflates *"which address offsets does this device consume"*
with *"which ordinals does it occupy."* Those coincided for ADR 0017's DiGiCo case
(offset 1) and diverge completely at 64. A device with offsets `{0, 64}` consumes two
addresses and occupies two ordinals — not 65.

**Shape of the fix:** occupancy becomes a *set of claimed offsets* rather than a contiguous
span. `Max` becomes distinct values in the elevation builder and in both validation
queries. Two things ride along:

- ADR 0017's `.255` bound (`rack_slot + max(slot_offset) <= slot_count`) stays as-is and is
  still correct — it is about the highest address reached, not about occupancy.
- The rack elevation's **bracket** encoding is wrong for a sparse offset. A bracket says
  "contiguous run"; offset 64 wants the tether treatment instead. See `Occupant.bracketed`
  (`views.py:268`).

Needs an ADR 0017 amendment before implementation, since it changes what `slot_span` means.

**Second-order finding, worth recording but *not* fixing here.** Offset 64 is a site
convention (Dante .1 -> control .65), not a hardware-derived address. ADR 0017's own scope
test — *"does the hardware compute the second address from the first and refuse to let
anyone change it?"* — says the Shure control port should be `address_source=OPERATOR`,
which `models.py:306` explicitly forbids from carrying a non-zero offset. `slot_offset` got
reached for because it was the only field that produced the right address. That gap is
phase 21's territory (device-type addressing modes, which already names Shure), and it
should be left there rather than folded into this fix.

---

## Tier 2 — cheap, high daily value

### #75 — clickable IP addresses

Read-only, no ADR 0020 exposure, and the one item you've said costs you time every day.

One config setting holding the URL template, rendered at request time so config changes
show up without a redeploy (the issue asks for this explicitly).

The only real decision is scope: switch VIPs only, as the issue says, or every address in
the address map and device detail. Recommend switch VIPs first, widen after use.

### Device model description in three more views

From the ROADMAP's "Design deferred" section; the cost survey landed in #90 and does not
need re-deriving. Explicitly read-only and *not* blocked on ADR 0020 decision 2.

Take these three:

- `spare_pool.html:50-56` — template-only, join and codename already exist
  (`views.py:1022`, `:1044`). The emptiest device surface in the app.
- `device_detail.html:75-77` ("Cards fitted") — template-only, join and codename already
  there (`views.py:984`).
- `vlan_map.html:113` — the worst case and the only one not already paid for. Widen
  `_vlan_addresses_in_use`'s join to `device__device_type__device_model` (`views.py:753`),
  add `view_networkdevicetype` and `view_networkdevicemodel` to the permission list
  (`views.py:776-792`), add a field to the `AddressEntry` dataclass (`views.py:701`).

**Stop there.** The fourth surface (`rack_detail.html:78-80`) is the crowded one and belongs
with #82 — see tier 3.

House conventions to extend rather than reinvent (all recorded in the ROADMAP entry):
parenthesized suffix suppressed when blank, as `admin.py:980-983` already does; do *not*
append `— description` after a type string, because `NetworkDeviceType.__str__` already
contains an em-dash (`models.py:3629-3630`).

### #81 — Dante unit ID base-10 vs hex

Documentation-shaped and nearly free. The field's help text and the device detail page
should say that ID 65 renders as `Y041`. Better still: show the assembled Dante device name
beside the input, so the conversion is *visible* rather than explained.

---

## Tier 3 — needs a design pass, not a ticket

### #82 + the rack-elevation half of the description item

These two collide and should be one pass. Both want more text in the `ordinal-cell`, which
is already at `9rem` min-width carrying an ordinal, a link, and up to three badges. Doing
them separately means designing that cell twice.

The pass should decide: what the elevation row shows, what moves to a `title=` tooltip
(every advisory marker in the app already carries a full-sentence one), and whether the
muted second line generalizes — `.taken-by-label` (`na9k.css:396-400`) is the existing
precedent for muted text inside a dense cell, and `.tile__meta` (`:234-237`) is the
house muted-text class.

Note #83 also touches this cell's encoding (bracket vs tether), so this pass wants to land
*after* #83, not before.

For #82 specifically: `dante_device_name` is a derived property, so it costs no query — the
cost is entirely layout.

### JS as progressive enhancement

Filtering, expanding an elevation, hover detail. The ROADMAP already establishes this needs
no `POST` and does not touch ADR 0020 — it is separable from JS as a write surface, which
does.

The largest available read-only lever, but undesigned, and it will go better once the cell
design above is settled.
