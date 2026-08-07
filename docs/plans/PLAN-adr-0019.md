> **Revision 2** — incorporates review notes from `REVIEW-1-PLAN-adr-0019.md`.
> See "Review response" for the mapping.

# Implement ADR 0019 — the rack ordinal is suggested, not typed

## Context

`ROADMAP.md` phase 14 has three unchecked implementation items. ADR 0019 is decided and
committed (`bb54a08`); its documentation consequences already landed in that same commit
(`CONTEXT.md`'s Rack entry sharpened, `docs/RACK-MUSINGS.md` removed), so what remains is
purely code.

Today, placing equipment means typing a rack ordinal by hand and finding out on submit
whether it collided. `RackSlotAssignmentMixin.clean()` (`inventory/models.py:1332`) refuses
a rack with no `rack_slot` outright — *"rack and rack_slot must both be set (racked) or both
be empty (spare pool)"* — so there is no way to say "put it in the next free spot." Every
piece needed to compute that answer already exists and is used twice, in both directions,
for collision *detection* (`models.py:2360` and `models.py:3400`); nothing turns it around
into a suggestion.

Outcome: on the add forms, choosing a rack and leaving `rack_slot` blank fills in the lowest
free run of `slot_span` consecutive ordinals. It is a default, not a lock — a typed ordinal
is never overwritten (ADR 0019 decision 3, ADR 0001, ADR 0003). Nothing stored changes, no
migration, no model field.

## Decisions

Settled with Mike, 2026-08-05. **Out of scope to relitigate.**

1. **No JavaScript.** A live/visible prefill was considered and declined. ADR 0020 decision 3
   already routes the visible-before-save experience through a deep link
   (`admin:inventory_networkdevice_add?rack=3&rack_slot=6`, *"which picks up ADR 0019's ordinal
   suggestion for free"*), and ADR 0020 v1 is strictly read-only `GET` views and templates. The
   submit-time fallback built here is the substrate any future progressive enhancement would
   need underneath it, so deferring costs nothing. ADR 0014 decision 9's *"this project has no
   JavaScript anywhere in it"* premise therefore still holds and is **not** amended.

2. **Both the host's and the companion's ordinal are suggested.** A blank `companion_rack_slot`
   on a racked companion-declaring type (ADR 0018) gets the next free run after the host's.
   Suggesting only the host would leave the feature half-working on exactly the console types
   ADR 0017/0018 exist for.

3. **Add forms only.** Not the change form. Blank `companion_rack_slot` there already means
   *"preserve the current relative offset"* (`admin.py:483-484`, ADR 0018 decision 1);
   overloading blank to also mean "pick for me" would give one field two meanings.

4. **ADR 0019's "as an initial value" prediction is corrected, not honoured.** Lines 183–184
   predict the helper is *"wired into the admin add forms as an initial value."* A literal
   `initial=` cannot work: Django renders the add page before the operator picks anything, so
   at render time there is no rack (no occupancy to search) and no `device_type` (no
   `slot_span`). This gets a visible `## Follow-up` correction on the ADR rather than a silent
   departure — the convention ADR 0015 set, *"so the gap between 'reading a diff' and 'running
   the suite' stays visible."* Decisions 2 and 3 of ADR 0019 stand exactly as written.

5. **The suggester is ordinal-only, and that is a real boundary** (added in rev 2 — see review
   note 3). It answers "which ordinals are unoccupied," not "which addresses are free." Because
   ADR 0003 makes stored addresses editable, equipment at ordinal 1 may hold the address that
   ordinal 2's arithmetic would produce, so a suggested-free ordinal can still be refused by
   `_validate_static_address`'s cross-table uniqueness check (`models.py:418-434`). This is
   accepted and pinned by a test, not fixed: ADR 0019 already frames the suggestion as *"a
   default, not a constraint,"* and closing it would mean checking materialized addresses across
   every VLAN the rack carries — a different and much larger mechanism.

## Review response

| Note | Resolution | Section |
|---|---|---|
| 1 (P1) — companion sequencing undefined when host suggestion fails | **Folded in.** Verified: the rev 1 text listed the host-failure `add_error` only as step 4, leaving a literal implementer free to build `host_range` from `None`. Step 4 now short-circuits explicitly, and verification 10 covers a full rack with a companion-declaring type. | Work §3 |
| 2 (P2) — `add_error()` produces a secondary model-level error | **Folded in.** Verified against `django/forms/forms.py:315-316` (the errored field is deleted from `cleaned_data`), so `_post_clean` then validates `rack` set with `rack_slot=None` and the mixin's all-or-neither non-field error fires too. The plan's "an error on its own field" claim was incomplete; corrected, and tests now assert the field error rather than the absence of others. | Work §3, Verification 10 |
| 3 (P2) — a free ordinal can still be rejected by address validation | **Folded in as an accepted limitation; the "define different mechanics" half rejected.** Verified real: `_validate_static_address` (`models.py:418-434`) enforces cross-table address uniqueness, and ADR 0003 makes addresses editable, so ordinal-free does not imply address-free. Rejected as a *fix* because ADR 0019 explicitly makes the suggestion a default rather than a constraint, and fixing it means checking materialized addresses across every VLAN the rack carries — new scope, and a different feature. Recorded as decision 5 and pinned by verification 12. | Decisions §5, Verification 12 |
| 4 (P2) — verification does not prove existing device spans are extracted | **Folded in.** Correct: rev 1's spanning test exercised a *new* span-2 candidate, so an implementation treating already-stored devices as span 1 would have passed it. New verification 5. | Verification 5 |
| 5 (P2) — the `construct_instance` "trap" and its claimed test are wrong | **Folded in; the plan was wrong.** Verified against `django/forms/models.py:75-82`: the skip needs a *third* condition rev 1 omitted — `cleaned_data.get(f.name) in empty_values` — so a suggested non-empty value is assigned regardless of any default, and `rack_slot` has no default anyway. The misleading note is removed. The substantive half stands: rev 1's "typed ordinal wins" test would pass with no suggestion code at all, so verification 3 now asserts the suggested value reached `form.instance.rack_slot`. | Work §3, Verification 3 |
| 6 (P2) — missing boundary and override cases | **Folded in.** An exact terminal fit (`occupied=[(1,2)], span=2, slot_count=4 → 3`) is the case that distinguishes `<` from `<=`; rev 1's "runs off the end" case did not. Typed `companion_rack_slot` preservation was likewise untested. | Verification 1, 8 |
| 7 (P3) — companion submission repeats occupancy queries | **Folded in, and used to simplify the API.** Rather than accept four bounded queries, `suggest_rack_slot()` is dropped entirely: both forms gather occupancy once with `occupied_rack_slot_ranges()` and call `lowest_free_run()` directly. This also removes the `also_occupied` parameter, whose job becomes obvious list concatenation at the call site. Two functions, one composition rule. | Work §2, §3 |
| 8 (P3) — misplaced citation | **Folded in.** `admin.py:479` is the field declaration; the blank-means-preserve wording is at `admin.py:483-484`. Corrected. | Decisions §3 |

The review confirmed the `lowest_free_run()` algorithm correct for empty, unsorted and
overlapping input, oversized spans and the `slot_count` boundary; confirmed the successful-path
ModelForm ordering claim; and confirmed the plan introduces no migration, model-field change,
constraint change or stored-data rewrite.

## Work

### 1. `inventory/suggestions.py` — the pure part

Add `lowest_free_run(occupied, span, slot_count) -> int | None`. This module's contract is
explicit — *"no DB queries and raise no ValidationErrors — callers own translating an absent
(None) result"* — and interval packing is the only non-trivial logic here, so it belongs where
it can be unit-tested without a database.

```python
def lowest_free_run(
    occupied: Iterable[tuple[int, int]], span: int, slot_count: int
) -> int | None:
    """Lowest 1-based start of ``span`` consecutive free ordinals in 1..slot_count."""
    if span < 1 or slot_count < span:
        return None
    cursor = 1
    for start, end in sorted(occupied):
        if end < cursor:
            continue                      # already behind the cursor
        if start - cursor >= span:
            return cursor                 # the gap before this range fits
        cursor = max(cursor, end + 1)
    return cursor if cursor + span - 1 <= slot_count else None
```

Tolerates unsorted and overlapping input on purpose — the caller unions two tables plus an
in-flight range and should not have to normalise first.

### 2. `inventory/models.py` — the query part

One module-level function beside `_suggest_rack_slot_address` (`models.py:289`), which is the
established home for rack-slot suggestion helpers. It is public (unlike its `_`-prefixed
neighbour) because `admin.py` calls it.

`occupied_rack_slot_ranges(rack) -> list[tuple[int, int]]` — every occupied `(start, end)` in
one rack, unioning both equipment tables:

- Switches always span 1 (`RackSlotAssignmentMixin.slot_span`), so they need no aggregate.
- Devices **reuse the existing span annotation verbatim** rather than inventing a second one:
  `Coalesce(models.Max("device_type__type_ports__slot_offset"), 0) + 1`, exactly as at
  `models.py:2362` and `models.py:3403`. Getting this wrong is invisible to most tests, which is
  what verification 5 exists to catch.
- Filter `rack_slot__isnull=False` defensively even though `rack`/`rack_slot` are all-or-neither.

Two bounded queries, no aggregate per row. There is deliberately **no** `suggest_rack_slot()`
wrapper (review note 7): callers gather occupancy once and call `lowest_free_run()` as many
times as they need, which is what makes the companion path cost the same two queries as the
host path.

No change to any model, field, constraint or `clean()`. The mixin's all-or-neither rule stays
exactly as it is — it is what makes "rack set, slot blank" an unambiguous signal to repurpose,
since today that combination is *always* an error.

### 3. `inventory/admin.py` — wire into the two add forms

Both follow `RackAddForm.clean()` (`admin.py:561`), which is this repo's existing answer to the
same problem: fill a blank field from other submitted data at submission, and `add_error` when
nothing can.

**`NetworkSwitchAddForm.clean()`** — a switch always spans 1:

```python
rack = cleaned_data.get("rack")
if rack is not None and cleaned_data.get("rack_slot") is None:
    slot = lowest_free_run(occupied_rack_slot_ranges(rack), 1, rack.slot_count)
    if slot is None:
        self.add_error("rack_slot", ...)   # names the span and the rack's slot_count
    else:
        cleaned_data["rack_slot"] = slot
```

**`NetworkDeviceAddForm.clean()`** — same shape, then the companion:

1. Bail unless both `rack` and `device_type` cleaned (a field error on either means the span is
   unknowable; let the ordinary error surface).
2. Gather `occupied = occupied_rack_slot_ranges(rack)` **once**.
3. Host: if `rack_slot` is blank, `lowest_free_run(occupied, device_type.slot_span, rack.slot_count)`.
4. **If the host ordinal is still unresolved — the suggestion returned `None` — `add_error` and
   stop.** Do not attempt the companion (review note 1): there is no host range to exclude, and
   building one from `None` raises.
5. Companion: only when `device_type.companion_type_id is not None` and `companion_rack_slot` is
   blank — `lowest_free_run([*occupied, host_range], companion_type.slot_span, rack.slot_count)`,
   where `host_range` is built from the host's ordinal whether suggested *or* operator-typed.
6. Either failing to find a run is an `add_error` on its own field.

Check `companion_type_id` before touching `companion_type` so an ordinary type costs no extra
query — the same reason `models.py:2347` uses `profile_id` over `profile`.

**Expect a second, non-field error alongside any `add_error` here** (review note 2).
`Form.add_error` deletes the field from `cleaned_data` (`django/forms/forms.py:315-316`), so
`_post_clean` goes on to validate an instance with `rack` set and `rack_slot=None`, and the
mixin's all-or-neither check adds an `__all__` error; the companion case likewise re-triggers
`_check_companion_creation_possible`'s "companion_rack_slot is required". This is acceptable —
the operator still gets a specific message on the right field — but tests must assert the field
error rather than asserting the *absence* of others.

Ordering needs no new hooks: `clean()` runs inside `_clean_form`, before `_post_clean` — so
`construct_instance` picks up `cleaned_data["rack_slot"]` and the existing `_post_clean`
(`admin.py:441`) picks up `cleaned_data["companion_rack_slot"]`.

### 4. Docs

- `ROADMAP.md`: tick phase 14's three unchecked boxes.
- `docs/adr/0019-rack-is-the-address-pool.md`: add `## Follow-up` recording decision 4 above —
  what "as an initial value" predicted, why it was not buildable, and what shipped instead —
  and decision 5's ordinal-only boundary.

No new ADR. No amendment to ADR 0014, ADR 0015 or ADR 0020.

## Verification

```bash
set -a; source .env; set +a
python manage.py test inventory
pre-commit run --all-files
```

Baseline the suite count *before* touching code, so "tests fail" is distinguishable from
"tests already failed."

Pure-function tests join the existing `SuggestionFunctionTests` (`tests.py:539`); the rest go in
a new `RackSlotSuggestionTests`, following the `Form(data={...})` style the `RackAddForm` tests
use (`tests.py:5986`) rather than driving the admin over HTTP.

1. `lowest_free_run`: empty rack → 1; a gap too small is skipped; unsorted and overlapping input
   give the same answer as normalised; `span > slot_count` → `None`; a run that would overrun the
   end → `None`; and **an exact terminal fit** — `occupied=[(1, 2)], span=2, slot_count=4 → 3` —
   the case that distinguishes `<` from `<=` at the boundary.
2. **A plain device takes the lowest free ordinal** (ROADMAP item 3).
3. **The suggested value actually reaches the instance** — assert `form.instance.rack_slot`
   equals the suggestion after `is_valid()`, not merely that the form validates. Without this,
   every other form test here would pass with the suggestion code absent.
4. **A spanning device (ADR 0017) skips a run that would overlap** (ROADMAP item 3) — a
   `slot_span` 2 type offered the gap after a device at 1 and a switch at 3 must get 4, not 2.
5. **An already-stored span-2 device contributes its whole range** — a device occupying 1–2, then
   a blank switch submission must resolve to 3, not 2. This is what catches an implementation
   that annotates existing devices as span 1; verification 4 alone does not.
6. **An operator-typed ordinal still wins** (ROADMAP item 3) — never overwritten.
7. Cross-table: a switch blocks a device's suggestion and a device blocks a switch's.
8. Companion gets the next free run after the host's and never overlaps it — both when the
   host's ordinal was suggested and when it was typed; and an explicitly typed
   `companion_rack_slot` is preserved untouched.
9. A companion type whose own `slot_span` > 1 gets a run that size, not a single ordinal.
10. **A full rack errors cleanly rather than raising** — no free run for the host on a
    companion-declaring type must produce a `rack_slot` field error naming the span and
    `slot_count`, with the companion step skipped entirely (review note 1). Assert the field
    error is present; do not assert `__all__` is empty (review note 2). Same for a rack with room
    for the host but not the companion.
11. Rack left blank → nothing suggested, both fields stay `None`, the form is still valid
    (spare pool).
12. **The ordinal-only boundary is pinned** (decision 5): equipment at ordinal 1 whose stored
    address was edited to ordinal 2's address, then a new device suggested into free ordinal 2 —
    the suggestion succeeds and `_validate_static_address` rejects the resulting address. This
    documents the limitation as known behaviour rather than leaving it to be rediscovered.
13. The change form is unaffected — moving equipment auto-suggests nothing.
14. `GET .../networkdevice/add/?rack=<pk>&rack_slot=6` prefills both fields. This pins the
    Django behaviour ADR 0020 decision 3 depends on (`get_changeform_initial_data` reads
    `request.GET`) before phase 15 builds on it.

## Out of scope

- Any JavaScript, and any live/visible prefill (decision 1).
- The change form and equipment moves (decision 3).
- Making the suggester address-aware rather than ordinal-aware (decision 5).
- Phase 15's UI and its deep links — this plan only makes sure the ordinal they will pass is
  computable, and pins the query-param behaviour they rely on.
- A DB-level rack-slot overlap guarantee. ADR 0019 is explicit that suggesting the lowest free
  run *"makes the common path correct but is a default, not a constraint"*; the gap stays open
  as #40.
- Aligned rack allocation — a different axis (where a rack's block sits, not the ordinal inside
  it), and ADR 0019 says so explicitly.
