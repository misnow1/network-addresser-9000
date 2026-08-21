> **Revision 2** — incorporates review notes from `REVIEW-1-PLAN-aligned-rack-allocation.md`.
> See "Review response" for the mapping.

# Phase 19 — Aligned rack allocation

## Context

`PROD-DATA-ANALYSIS.md` §6.1: the production spreadsheet gives each rack **one**
`Address Offset` applied to every VLAN base, which *guarantees* a device's Control, Dante
Primary and Dante Secondary addresses share a host portion. This tool instead allocates per
`(rack, VLAN)` by independent first-fit (`suggest_rack_vlan_range()`,
`inventory/suggestions.py:56`), so that alignment holds only while every VLAN has identical
sibling ranges, identical DHCP geometry and identical creation order. One VLAN with a DHCP
range its neighbours lack, one rack deleted and recreated, one hand-entered range — and the
host octets silently diverge. **Nothing detects it.**

Phase 19 removes the mechanism rather than policing the outcome: allocate a rack's offset
**once**, as the lowest offset free on every VLAN it is getting a range on. No schema change —
a suggester change plus a report.

This matters more than it looks because audio is the pilot, not the scope (§7.1): video and
lighting VLANs will be added to racks that already exist, which is precisely the path that
reintroduces divergence.

The review confirmed the two facts this plan most depends on, so they are no longer
assumptions:

- **The Django ordering holds** on the pinned Django 6.0.7 — inline `instance.full_clean()`
  precedes formset `clean()`, and `all_valid()` precedes `save_model()` → `_apply_template()`
  → `save_related()` (`django/forms/models.py:479`, `django/forms/formsets.py:423`,
  `django/contrib/admin/options.py:1847`).
- **The production import cannot change.** The three automatic VLANs are identical `/21`s with
  identical relative DHCP geometry, and `SHURE`/`CONSOLES` take explicit ranges
  (`import_prod_data.py:727`, `:840`), so aligned allocation cannot move an imported base.
- The **point test is real**: with one VLAN lacking DHCP and its neighbour reserving
  `.2`–`.254`, today's code genuinely produces offsets 0 and 256.

## Decisions settled during grilling

Resolved with Mike, 2026-08-20. Out of scope to relitigate.

1. **Both batch and sticky.** The batch picks one offset across the VLANs being allocated; a
   blank range added later to an existing rack follows that rack's offset if it is free.
2. **Never guess an offset.** If a rack's existing ranges disagree, there is no "the rack's
   offset" — fall back to today's first-fit and let the report show it.
3. **Fall back, and say so.** When no offset is free on every VLAN, allocate per-VLAN first-fit
   as today and emit a non-blocking advisory. The existing blocking pre-flight (a VLAN with no
   free block at all) is unchanged.
4. **Rack-level report only.** Device-level divergence is a symptom with two legitimate causes
   (ADR 0017 `slot_offset`, ADR 0022 operator-set addresses) and is #28's territory.
5. **Mirror `hostname_diverges`.** Property + admin column + `SimpleListFilter` + read-only UI
   markers. No new route.
6. **Search only this rack's VLANs**, not every VLAN in the system.
7. **The inline formset aligns too**, via the same allocator.
8. **No realign action.** Device addresses are stored, not derived (ADR 0003), so rewriting a
   rack's range would leave every device in it holding an address outside its own block.
   Recorded in the ADR as declined so it isn't re-proposed.
9. **ADR 0025, amending ADR 0001** — 0001's suggest-with-override and stored-not-recomputed
   stances survive intact; only the *search* changes from per-VLAN to joint.
10. **Two PRs**: ADR first (docs-only, #72's lint/test skip applies), then the build.
11. **Department scoping stays declined** (`ROADMAP.md` phase 19 gives three surviving reasons).

## Review response

| Note | Resolution | Section |
|---|---|---|
| 1 (P1) — `free_offsets()` as a materialized set regresses first-fit's early return; `validate_ipv4_cidr` permits `/0`, i.e. 134M candidates | **Accepted.** Verified at `validators.py:9` — any IPv4 CIDR is legal, so this is a real regression, not a theoretical one. The helper becomes a **lazy ascending generator** `iter_free_offsets()`, and the joint search walks candidates ascending and returns the first free on every VLAN. `suggest_rack_vlan_range()` becomes `next(iter_free_offsets(...), None)`, preserving its early return exactly | The joint allocator |
| 2 (P1) — advisories only fire on the template path; and "which VLAN forced it" is not well defined | **Accepted, with a different resolution than proposed.** All three paths emit. But rather than attributing blame to a VLAN — which the reviewer correctly says has no unique answer — the advisory **reports the outcome**: which VLAN landed on which offset. That is well-defined, needs no arbitrary choice, and says exactly what the report says | Advisory surfacing |
| 3 (P1) — malformed stored ranges would 500 the rack page | **Accepted.** Verified: `RobustnessTests.test_malformed_stored_range_renders_200_with_blank_cells` (`test_ui.py:1600`) exists precisely because a bare `save()` bypasses `clean()`. `range_offset()` stays strict; a tolerant wrapper returns `None`, and a rack with ≥2 ranges where any offset is unavailable **counts as diverging** — it cannot be shown to be aligned | The report |
| 4 (P1) — inline anchor semantics imprecise (deleted rows, unchanged existing rows) | **Accepted.** Anchors are defined explicitly: every **non-deleted** form with a submitted non-blank range, **plus** the rack's already-saved ranges not represented in the formset. `DELETE` rows are excluded, matching `admin.py:163` | The inline formset |
| 5 (P1) — no tests for `range_offset()`/`range_at_offset()`; production replay can't cover unequal subnets since all three prod VLANs are `/21` | **Accepted.** Named round-trip tests added, including offset 288 across an octet boundary (the real `WPC1SRU` value) and a subnet not starting on a `/24` | Tests |
| 6 (P2) — new admin column N+1s the ordinary changelist; the copied filter only prefetches when a filter value is selected | **Accepted.** Verified: `RackAdmin` has `list_select_related = ["owner"]` and no `get_queryset()`. Unconditional `prefetch_related("vlan_ranges__vlan")` in `RackAdmin.get_queryset()`, plus a query-count test | The report |
| 7 (P1) — verification can pass without the read-only report existing; and the plan never says how the offset reaches the template | **Accepted.** Mechanism pinned: a tolerant `RackVlanRange.offset` property, which the existing `select_related("vlan")` already feeds — no view annotation, no template filter. Rendered-content tests added for every promised surface | The report; Tests |
| 8 (P2) — real-export verification is prose only; and the verifier reports 63 rack-range triples, not "19 of 19" | **Accepted.** `prod/` is gitignored so the real replay cannot live in the suite; it becomes an explicit operator command in Verification, and the claim is restated in the verifier's own terms (63 `(rack, VLAN, CIDR)` triples) | Verification |
| 9 (P3) — stale line citations | **Accepted.** Verified and corrected throughout: `_apply_template()` `:1393`, `RackVlanRange.clean()` `:1510`, `views.index` `:1040`, `rack_detail.html:25` | throughout |

Nothing was rejected, and nothing hit the escalation gate — no finding contradicts an ADR,
changes scope, or attacks a settled decision.

## PR 1 — `docs/adr/0025-aligned-rack-allocation.md`

Records decisions 1–9 and 11 above, plus the two invariants the roadmap already pinned:

- **The invariant is the offset from the VLAN's network address, not the third octet.** 16 of
  the 21 production racks don't start on a `/24` boundary; `WPC1SRU`'s offset of 288 spans two
  octets. An offset rule survives a VLAN that isn't a `/21`; a third-octet rule doesn't.
- **Static addresses only.** A DHCP port stores no address, so this needs no special handling —
  and because the report is rack-level (decision 4) the "ignore DHCP ports" hazard in §6.1
  cannot arise at all.
- **Department scoping stays declined**, with the three surviving reasons from `ROADMAP.md`.
- Known gaps carried forward, not closed: #43 (the DHCP range recorded wider than reality) and
  the mnemonic offset gaps of §7.2 both still want **address regions**; aligned allocation
  consumes the gaps exactly as first-fit does.

This is its own first commit on the branch, so the docs-only PR can be split off (#72's
lint/test skip applies to it).

## PR 2 — the build

### `inventory/suggestions.py` — the joint allocator

Extract the candidate walk `suggest_rack_vlan_range()` already performs, **as a lazy
generator**, then build the joint search on it, so both searches provably agree about what
"free" means without either losing its early return:

- `iter_free_offsets(subnet, prefixlen, used_ranges, dhcp_range) -> Iterator[int]` — yields, in
  **ascending order and lazily**, the offsets (from the subnet's network address) at which a
  `prefixlen` block overlaps neither `used_ranges` nor `dhcp_range`. Never materialises the
  candidate space: a legal `/0` subnet holds 134,217,728 `/27` candidates (`validators.py:9`
  permits it), so eager evaluation would be a catastrophic regression on a path that is O(1)
  today.
- `suggest_rack_vlan_range()` becomes `next(iter_free_offsets(...), None)` mapped back to a
  CIDR — behaviour-identical, including its `prefixlen < network.prefixlen` guard, its
  `dhcp_range` handling and its `None` returns.
- `range_offset(subnet, range_cidr) -> int` — a stored block's offset from its VLAN's network
  address. The one definition of "offset" the allocator and the report both use. **Strict**:
  raises on malformed input; tolerance lives in the model layer, not here (this module's
  docstring already promises purity and no error handling).
- `range_at_offset(subnet, offset, slot_count) -> str` — the inverse.
- `suggest_aligned_offset(vlans, slot_count) -> int | None` — the lowest offset free on **every**
  VLAN, where `vlans` is a list of `(subnet, used_ranges, dhcp_range)`. Implemented as an
  ascending merge over each VLAN's `iter_free_offsets()` — advance the laggards, return on
  agreement — so it stops at the first hit and never enumerates a whole subnet. `None` when no
  such offset exists, including when any VLAN's subnet is smaller than the block.

Candidate offsets are multiples of the block size given by the existing
`prefix_length_for_capacity()` / `required_block_size()`, so ADR 0015's `/27` floor is
inherited, not restated. Because each VLAN's network address is aligned to its own prefix and
the block is never larger than the subnet, `network_address + k × block_size` is always a valid
CIDR boundary on every VLAN — that is what makes one offset usable across VLANs of different
sizes.

### `inventory/models.py` — the three allocation paths

**`Rack._apply_template()` (`:1393`).** Build the per-VLAN inputs from the existing `links`
snapshot (do not re-query — the READ COMMITTED reasoning in that docstring still applies) and
call `suggest_aligned_offset()`. On a hit, construct each `RackVlanRange` with an explicit
`address_range=range_at_offset(...)` — which skips the per-row suggester while still passing
through `full_clean()`/`_validate_range()`. On a miss, construct blank exactly as today and
record an advisory. `_check_template_application_possible()` (`:1426`) is untouched: it stays
the *blocking* pre-flight for "this VLAN has no free block at all".

**`RackVlanRange.clean()` (`:1510`) — the sticky rule.** In the existing blank-and-unsaved
branch, and only when `rack.pk is not None`: read the rack's other ranges, map each through
`range_offset()`, and if they all agree on one offset *and* a block at that offset is inside
this VLAN's subnet and free, adopt it. Otherwise fall straight through to today's
`suggest_rack_vlan_range()` call, recording an advisory. Ranges whose stored value won't parse
are skipped, matching how the surrounding code already tolerates a sibling's malformed value.

**`RackAddForm` / `RackVlanRangeInlineFormSet` (`inventory/admin.py:130`, `:1194`) — one offset
for the whole rack at creation.** The ordering here is confirmed on Django 6.0.7 and must be
locked by tests, because the implementation depends on it:

- Each inline form is validated — including `instance.full_clean()`, which *already* fills a
  first-fit suggestion — **before** `formset.clean()` runs. So the joint pass identifies
  user-blank rows from `form.cleaned_data.get("address_range")` (the submitted value, still
  `""`) and **overwrites** `form.instance.address_range`, re-validating the instance and
  routing any failure to `form.add_error`.
- `all_valid(formsets)` runs before `save_model()` (rack save → `_apply_template()`), which runs
  before `save_related()` (inline rows). So at inline-clean time the template's rows do not
  exist yet, and an inline row for a fourth VLAN would otherwise be allocated independently of
  the template's offset.

  The fix makes it one decision: `RackVlanRangeInlineFormSet.clean()` computes a single joint
  offset over **the template's VLANs ∪ the blank inline rows' VLANs**, fills the inline blanks
  from it, and stashes it on `self.instance._aligned_offset`. `_apply_template()` prefers that
  stashed offset when it is still free, and computes its own otherwise. This reuses the exact
  `self.instance.template` stashing trick decision 11 already relies on, in the opposite
  direction — extend the docstring at `admin.py:130` to cover it.
- **Anchors are defined precisely**: every **non-deleted** form whose submitted `address_range`
  is non-blank, **plus** the rack's already-saved ranges not represented by a form in this
  formset (the change-page case). `DELETE` rows are excluded, matching the existing skip at
  `admin.py:163`. If the anchors agree they set the offset; if they disagree the blanks fall
  back to first-fit (decision 2 — a previously divergent rack must never be handed a guessed
  offset).

**Advisory surfacing.** All three paths append to `rack._range_alignment_advisories`;
`RackAdmin.save_model()` emits them via `messages.info`, beside the existing
`_emit_hostname_advisories()` call (`admin.py:650`) — same accepted limit: admin-only, with
programmatic callers able to read the attribute.

The advisory **reports the outcome rather than blaming a VLAN**: when several VLANs have
non-empty free-offset sets whose intersection is empty, no single VLAN "caused" it, so the
message names which VLAN landed on which offset. The sticky path's message names the rack's
offset and what the new VLAN got instead; the disagreeing-anchors path says no offset could be
inherited because the rack's existing ranges disagree.

### The report

- **`RackVlanRange.offset`** — a tolerant property returning `range_offset(vlan.subnet,
  address_range)` or `None` when either value is malformed, the VLAN has no subnet (L2-only,
  ADR 0012), or the range is outside its subnet. This is how the offset reaches the template:
  `rack_detail`'s columns *are* `RackVlanRange` objects with `select_related("vlan")` already
  applied (`views.py:640`), so no view annotation and no template filter is needed.
- **`Rack.range_offsets_diverge`** — a stateless property, `True` when the rack's ranges don't
  all share one offset. Zero or one range is never divergent. A rack with two or more ranges
  where **any** offset is `None` counts as **diverging**: it cannot be shown to be aligned, and
  silently reporting "aligned" for data the tool can't read would be worse than a false flag.
  Modelled on `NetworkSwitch.hostname_diverges` (`models.py:2688`), including its
  no-extra-queries posture when the caller has prefetched.
- **`RackAdmin`** (`admin.py:1580`) gains the column in `list_display`, a
  `RackRangeOffsetsDivergeFilter` copied from `_HostnameDivergesFilterBase` (`admin.py:1650`),
  and — because that filter returns the queryset untouched when no value is selected — an
  unconditional `prefetch_related("vlan_ranges__vlan")` in `get_queryset()`, so the ordinary
  changelist doesn't N+1.
- **Read-only UI.** `views.index` (`:1040`) prefetches `vlan_ranges__vlan` and the rack tiles in
  `index.html` carry a marker; `rack_detail.html`'s column headers (`:25`) print the offset
  beneath the existing `column.address_range`, so the odd VLAN out is identifiable by eye, plus
  a panel-level note when the rack diverges. Copy stays operator-facing with no ADR references
  (existing convention); the *why* goes in template comments.

### Tests

`inventory/tests.py` (pure + model) and `inventory/test_ui.py` (views + query budget):

**Pure helpers**
- `suggest_aligned_offset()`: lowest offset free on all; `None` when none exists; `None` when
  one VLAN's subnet is too small; agrees across VLANs of **unequal but sufficient** size.
- `range_offset()` / `range_at_offset()` round-trip, including **offset 288 across an octet
  boundary** (the real `WPC1SRU` value), a subnet that does not begin on a `/24`, boundary
  containment, and CIDR alignment. Production replay cannot cover these — all three production
  VLANs are `/21`.
- `iter_free_offsets()` is lazy: assert a large subnet yields its first offset without
  enumerating the space (e.g. `next()` on a `/8` returns promptly and `itertools.islice` of a
  few values terminates).
- `suggest_rack_vlan_range()` behaviour is unchanged: the existing tests (`tests.py:631`–`:647`,
  `:7435`–`:7510`) pass untouched.

**The point test** — two VLANs where one carries a DHCP range the other lacks: assert today's
per-VLAN first-fit gives 0 and 256, and the aligned allocation gives one offset. This is the
case §6.1 says the current model gets wrong, so it is the test that justifies the phase.

**Allocation paths**
- Fall-back: no aligned offset → ranges still created, advisory recorded, naming each VLAN's
  offset.
- Sticky: agreeing rack adopts its offset; disagreeing rack falls back with its own advisory;
  agreeing rack whose offset is taken on the new VLAN falls back.
- Inline formset: several blank rows share one offset; template + a blank inline row on a
  fourth VLAN share one offset (the ordering trap); **agreeing anchors** set the offset;
  **disagreeing anchors** force first-fit; an **unavailable anchored offset** falls back;
  **unchanged existing rows** count as anchors; **deleted rows** do not.

**Report**
- `range_offsets_diverge` across 0 / 1 / aligned / misaligned racks, and a rack holding a
  malformed range (diverging, not crashing).
- `RackVlanRange.offset` returns `None` rather than raising for malformed, out-of-subnet and
  L2-only cases.
- Rendered content for **every** promised surface: the admin column, the admin filter both
  ways, the index marker, the rack-detail per-column offsets, and the rack-detail divergence
  note.
- Extend `RobustnessTests` (`test_ui.py:1600`): a rack with a malformed stored range still
  renders 200 on both `/racks/<pk>/` and the index, now that both read offsets.
- Query-count parity for `index`, `rack_detail` and the Rack admin changelist under the new
  prefetches.

### Verification

```bash
set -a; source .env; set +a
python manage.py test inventory
pre-commit run --all-files
```

The suite's import coverage is **synthetic** — `test_prod_import.py:94` builds four
automatically-allocated racks, not the real 21 — so the real replay is an explicit operator
step, since `prod/` is gitignored and cannot live in the suite:

```bash
set -a; source .env; set +a
python manage.py import_prod_data --data-dir prod
python manage.py verify_prod_import --data-dir prod
```

Stated in the verifier's own terms: `_check_rack_ranges` must still report **63
`(rack, VLAN, CIDR)` triples checked** with no findings, and every other `## Verification`
check green. The reasoning behind expecting no change was confirmed in review — the 19
template-allocated racks share identical VLAN geometry and `SHURE`/`CONSOLES` carry explicit
ranges — but ADR 0015's `## Follow-up` records this project being wrong before about a
prediction made by reading a diff, so the command is run, not reasoned about.

## Definition of done

- ADR 0025 committed as its own first commit on the branch.
- `ROADMAP.md`: phase 19's five checkboxes ticked, and the **Current phase** line moved to 20.
- Both `python manage.py test inventory` and `pre-commit run --all-files` green, with the
  baseline test count recorded before any code changed.
- No migration. `RackVlanRange`, `Rack` and every stored range are untouched by this phase.
