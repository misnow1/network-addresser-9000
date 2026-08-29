# Close the programmatic port-creation address bypass (#99)

> **Revision 2** — incorporates review notes from `REVIEW-1-PLAN-issue-99.md`. See "Review response"
> for the mapping. Two findings hit the escalation gate and were resolved with Mike on 2026-08-29:
> **decision 2 is narrowed** (the guard now verifies only where a derived address exists, matching
> migration `0023`'s own `device__rack__isnull=False` line), and **this work lands after ADR 0027
> PR 2**, which deletes the one test fixture the guard would otherwise make unconstructible.
> Revision 1's churn estimate was wrong in both directions — see finding 1.

## Sequencing: after ADR 0027 PR 2

`docs/plans/PLAN-adr-0027.md`'s PR 2 deletes the issue-#60 `taken_by` axis — `_build_taken_address_
map()`, `ElevationCell.taken_by`, `taken-by-label` — and with it `TakenAddressMarkerTests`
(`inventory/test_ui.py:1154`), every assertion in which is on `taken-by-label`. That class holds
`test_address_outside_the_racks_range_marks_nothing` (`test_ui.py:1522`), whose fixture is a racked
device with a range and a deliberately out-of-range address — the single fixture in the suite that
this guard makes unconstructible and that has no conformant rewrite, because being non-conformant is
the scenario.

Building #99 first would mean deciding the fate of a test another plan already owns. Building it
after PR 2 means the fixture is gone before the guard meets it. **This plan does not start until PR 2
is merged.**

## Context

[Issue #99](https://github.com/misnow1/network-addresser-9000/issues/99) — found by the ADR 0027
PR 1 review council, deferred out of that PR, and since re-validated against merged `main`
(`bb88d3c`) by running the probe in the issue's own comment.

[ADR 0027](../adr/0027-the-ordinal-is-the-unit.md) decision 1 says a static address is derived from
`range_base + rack_slot + slot_offset` and system-written only — the operator never types one. That
holds through the admin and at materialization. It does **not** hold on the programmatic create
path: `NetworkDevicePort.clean()` derives only when `self.pk is None and not self.address`
(`inventory/models.py:5101`), so a caller that supplies an in-range address at creation keeps it,
never compared against the formula. `_locked_fields()` (`:5146`) then locks `address`.

**The lock is not permanent, and revision 1 said it was.** The remedy is two saves, not
delete-and-recreate: `is_dhcp = True; save()` clears the address under the flip's lock exemption
(`models.py:4977-4996`), then `is_dhcp = False; save()` derives the correct value
(`:5218-5276`). `is_dhcp` is one of only two fields the admin device-port inline leaves editable
(`admin.py:1529`), so an operator can do this, not just a script. What survives of the issue is the
real half: a non-conformant address enters the database unchallenged and stays until somebody
notices.

The admin cannot reach the create path at all — `NetworkDevicePortInline.has_add_permission()`
returns `False` unconditionally (`admin.py:1536`), not merely on the Add page, and there is no
standalone `NetworkDevicePortAdmin`. So this is a bypassed-write-shaped gap, not an operator-facing
one — but it is the "address that disagrees with its ordinal" state ADR 0027 exists to make
unrepresentable, reachable through ordinary ORM use. Migration `0023`'s conformance assertion proves
that state was absent *at migration time*, not that it stays absent.

### The importer does not supply addresses

The issue left this open, flagging that `import_prod_data.py` "may rely on supplying them", which
would have favoured a laxer fix. It does not, and the review confirmed it independently: the only
constructions of `NetworkDevicePort` in production code anywhere are `models.py:4533` and `:4551`,
both inside `_materialize_ports()`. `import_prod_data.py` contains no reference to the class at all;
`verify_prod_import.py`'s two (`:876`, `:1444`) are reads. The importer creates `NetworkDevice` rows
and lets materialization do the arithmetic, which is the stated point of that command's docstring.

Nothing in the tree depends on hand-supplying a device port address.

### The blast radius, counted

Revision 1 guessed "~31 of 43 constructions pass an explicit `address=`" and called the failure count
unknowable statically. The review counted it properly: **38 of 43** pass an explicit address, but
14 of the 43 use *historical* models via `apps.get_model()` (`tests.py:3129-3343`, `:8091-8531`) and
have no custom `save()`/`clean()` at all, so they are immune. Under revision 1's decision 2, 14 sites
became hard failures. **Under the narrowed decision 2 below, one does** — `test_ui.py:1522`, which
PR 2 deletes before this plan starts.

What remains is not churn but three tests that keep passing for a *new* reason (finding 5 below), and
they need message assertions rather than fixture rewrites.

## Decisions this plan settles

### 1. The `save()` guard is verify-only, never derive-when-blank

The guard fires on insert when the row is **static and already carries an address**: derive, compare,
refuse on mismatch. It never fills a blank one. `clean()` keeps both jobs it has today —
fill-if-blank, then (new) verify. `save()` takes only the second.

Deriving-when-blank in `save()` would close no part of #99 — the issue is a *supplied* address kept
and locked — and would hand the ORM a new capability, minting a correctly-addressed port outside
`_materialize_ports()` with no `full_clean()`. That cuts against ADR 0027's grain, where a port comes
into existence by materializing a type.

**Corrected from revision 1:** the justification "a blank static address is already refused loudly by
the DB `CHECK`" holds for `None` but not for `""`. `device_port_dhcp_xor_static_address` tests
`address__isnull=False` (`models.py:4932-4938`), and `GenericIPAddressField.get_prep_value()` returns
`str("")` rather than `None`, so `objects.create(is_dhcp=False, address="")` inserts an empty string
past the constraint. The guard therefore gates on **`self.address is not None`**, not on truthiness:
`""` is treated as a supplied value, compared, and refused. That closes the empty-string hole as a
by-product rather than leaving it to a truthiness test that would skip it.

### 2. The guard verifies only where a derived address exists

**Narrowed from revision 1 after review (escalation E1 and finding 1); resolved with Mike,
2026-08-29.**

`_suggest_rack_slot_address()` (`models.py:301`) returns `None` for four causes: unracked device, no
`RackVlanRange` for the VLAN, a malformed range CIDR, and `rack_slot + slot_offset` overflowing the
block. When it returns `None` there is nothing to compare against, and **the guard does not fire** —
the insert falls through to exactly today's behaviour (`clean()`'s own unracked and containment
checks on the `full_clean()` path; the DB `CHECK` on the bare-`save()` path).

Revision 1 refused those inserts instead, reusing the flip path's two error messages. Three things
were wrong with that:

- **It polices a state ADR 0027 deliberately does not.** `_assert_full_conformance()` in
  `inventory/migrations/0023_derived_addresses.py:266` filters `device__rack__isnull=False`, and
  `tests.py:8183 test_conformance_assertion_ignores_unracked_devices` pins that exclusion. The ADR's
  own migration draws the line at "racked, with a range"; the guard now draws the same one.
- **It cost a structural fixture rewrite for no gain in scope.** Ten of the fourteen failures were
  ports on unracked devices or racks with no range — `NetworkDevicePortTests.setUp` (`tests.py:404-411`)
  creates no `Rack` at all, and `RemovalSemanticsTests` / `DeleteConfirmationTests` are about
  `on_delete` semantics, not addressing. Adding a rack and a range to those `setUp`s to satisfy an
  addressing guard is coupling those suites to a concern they were written to avoid.
- **One case had no conformant form at all.** `tests.py:413 test_description_unique_per_device`
  creates two ports on the *same device and same VLAN* at offset 0; under ADR 0027 both derive to the
  same address, so no conformant pair exists — it would have needed re-shaping onto two VLANs with
  two ranges to keep testing description uniqueness.

**Accepted consequence, stated plainly:** a bare `.save()` insert of a static port on an unracked
device, or on a rack with no range, still succeeds silently with whatever address the caller supplied.
That is a bypassed-write gap, but it is not #99 — there is no derived address for it to disagree with
— and it is the state migration `0023` already declines to police. Closing it belongs to whatever
decides that unracked static ports are illegal, which no ADR has yet said.

### 3. The duplicated derivation is accepted; no memoization, no bypass flag

Per static port on the `_materialize_ports()` path: `_derive_addresses()` does one
`rack.vlan_ranges.get()` (`models.py:301`), `clean()`'s `_validate_static_address()` →
`_address_containment_error()` (`:463`) does another plus two uniqueness selects, and the two new
verifies add one each.

Accepted as the price of `save()` being independently trustworthy, which is the point of the fix.
Explicitly rejected: marking instances as system-derived so the verifies skip — that is
`_deriving_address` returning under a new name, and ADR 0027 deleted that flag on purpose
(`_locked_fields()`'s docstring, `models.py:5158-5159`: "no longer a privileged *writer* to exempt").
Also rejected: memoizing the range lookup, which buys a rare path a saving it does not need inside a
transaction already taking row locks.

**Recorded as a risk, not solved (finding 7):** the three reads are unlocked and this project runs
READ COMMITTED, so a concurrent committed edit to the `RackVlanRange` mid-transaction can make the
third read disagree with the first and abort a device create with a message naming two values the
caller never chose. The failure is fail-closed — the transaction rolls back, nothing is written — and
rack-range edits are a rare admin action, so it is accepted rather than fixed by locking
`RackVlanRange` alongside the type row (`_lock_type_rows`, `models.py:4181`).

### 4. One derivation helper, two contracts, three callers

Extract `NetworkDevicePort._derived_static_address() -> str | None`: computes
`range_base + rack_slot + slot_offset` for this port, or `None` when it is not derivable. Like
`_derive_address_on_flip_to_static()` today, it reads the **persisted** `slot_offset`/`vlan_id` for a
persisted row and `self`'s for an insert — that subtlety is load-bearing and protected by
`tests.py:2522 test_flip_to_static_derives_from_persisted_slot_offset_not_forged_self`, so it lives in
exactly one place.

Two contracts over one helper:

- `_derive_address_on_flip_to_static()` keeps its raise-on-`None` behaviour and its two existing
  messages, unchanged, and becomes a thin wrapper: call the helper, raise if `None`, assign, return.
- The two verifies (`clean()`, `save()`) take the `None` as "not derivable, do not fire" per
  decision 2.

Revision 1 had the helper raising and both verifies inheriting that; the narrowing in decision 2 is
what splits the contracts. The formula itself stays single-sourced in `_suggest_rack_slot_address()`.
`clean()`'s **fill-if-blank** path is deliberately left calling `_suggest_rack_slot_address()`
directly — it has suggest-or-fall-through semantics ending in "Static ports must have an address",
and routing it through the helper would change an existing message on a path this issue is not about.

### 5. The refusal message names the derived value, and cites no ADR

Field-keyed on `address`, matching `_validate_static_address()`'s shape for every other address
complaint (`models.py:520`):

> `The port's static address must be 10.99.0.1 (range_base + rack_slot + slot_offset), not 10.99.0.20.`

No ADR reference: a validation message can surface in a form, and an operator does not know what an
ADR is. The rationale goes in the code comment beside the guard.

Under the narrowed decision 2 this is now the **only** message the guard raises — revision 1's reuse
of the flip path's two messages is gone, and with it that reuse's two defects (revision 1 quoted a
`{vlan}` form that exists on neither helper, and the string it actually named ends "before converting
this port to static", a non-sequitur on an insert).

### 6. The insert test is `self.pk is None or self._state.adding`

**Corrected from revision 1 (finding 2).** Revision 1 used `self.pk is None` and wrote off the
explicit-pk insert as a pre-existing gap "uniform across the class". That was factually wrong:
`models.py:3040-3047` documents `self.pk is None or self._state.adding` as this module's idiom
*specifically for* "fixtures, **scripted inserts**" with a pre-assigned pk, and five other sites use
it (`:1494`, `:3079`, `:3444`, `:4173`, `:4233`).

#99 is *about* scripted inserts. `objects.create(pk=999, is_dhcp=False, address="10.99.0.20")` reaches
`save()` with `pk` set, `_persisted_is_dhcp()` returning `None`, both flip flags `False`, and
`_check_locked_fields_unchanged()` early-returning because the row is invisible — so under revision 1
the fix would have shipped with its own headline bug reachable through one extra kwarg. Both new
guards use the full idiom.

### 7. `NetworkSwitchAddress` is out of scope, and gets a follow-up issue

Verified: it has no `save()` override and no `_locked_fields()` (`models.py:3307-3369`; the next such
definitions at `:3442`/`:3497` belong to `NetworkSwitchPort`). Its `address` carries
`help_text="Leave blank to suggest rack range base + rack slot"`, its docstring cites ADR 0003's
computed-but-stored pattern (`:3310`), and the admin inline keeps add/edit on the change page
(`admin.py:400`). ADR 0027 decision 1 is scoped in its own words to "every static address a racked
**device** holds".

Fixing it here would silently extend an ADR to a model it did not cover. A follow-up issue records
the divergence as a question for a future ADR, not as a defect.

**Noted, since the review raised it:** the "correctable in place" distinction is weaker than revision 1
claimed — a device port is correctable too, via the flip (see Context). The load-bearing half of
decision 7 is the scope of ADR 0027 and ADR 0003's continued governance of switch addresses, not the
lock.

## The change

### `inventory/models.py`

1. **New** `NetworkDevicePort._derived_static_address() -> str | None` per decision 4 — derive-or-
   `None`, reading persisted identity fields for a persisted row. The persisted-read explanation
   moves here from `_derive_address_on_flip_to_static()`'s docstring, with a pointer left behind.
2. **Rewrite** `_derive_address_on_flip_to_static()` (`:5218-5276`) as the raise-on-`None` wrapper.
   No behaviour change on the flip path.
3. **`clean()`** (`:5067`) — in the static branch, add the insert verify beside the existing
   fill-if-blank `elif` (`:5101`). Gate: `(self.pk is None or self._state.adding)`,
   `self.address is not None`, **and `device is not None and vlan is not None`**. Those null guards
   are not optional (finding 3): `full_clean()` runs `clean()` even after `clean_fields()` has
   produced errors, this file already carries a regression test for that hazard, and both siblings
   in this branch (`:5101`, `:5109`) carry the same guards. Ordering: the existing unracked check
   (`:5094-5098`) must still fire first.
4. **`save()`** (`:4945`) — inside the existing `transaction.atomic()`, before
   `_lock_switch_port_rows()`, the same verify gated on the same idiom plus `not self.is_dhcp and
   self.address is not None`. It calls the helper itself rather than leaning on `clean()` having run,
   which is the whole reason it exists — the same argument the flip path's in-`save()` validation
   already makes (`:4956`).

Both verifies do nothing when the helper returns `None` (decision 2), and both raise decision 5's
message on mismatch.

### Tests — `inventory/tests.py`

Against a device at a known `rack_slot` in a rack with a known `RackVlanRange`:

1. `full_clean()` create with a non-conformant **in-range** address → `ValidationError` keyed on
   `address`, message naming the derived value. *(the test #99 names as missing)*
2. `full_clean()` create with the **conformant** address → accepted. Proves the rule is "must match",
   not "must be blank".
3. `objects.create()` with a non-conformant address, no `full_clean()` → refused by `save()`.
4. `objects.create()` with the conformant address → accepted.
5. `objects.create(pk=<explicit>, …)` with a non-conformant address → refused (decision 6's gap; this
   test is what proves the idiom, and it fails against a `self.pk is None` gate).
6. A **non-zero `slot_offset`** port verifies against `range_base + rack_slot + slot_offset`
   (finding 9). Without this, an implementation that dropped the offset term passes every other test.
   `_make_console()` (`tests.py:8035`) already builds the DM7C shape.
7. `objects.create(is_dhcp=False, address="")` on a derivable port → refused (decision 1's
   empty-string hole).
8. Underivable inserts **fall through unchanged** (decision 2): a static port with a supplied address
   on an unracked device, and one on a rack with no `RackVlanRange`, are each accepted by
   `objects.create()` exactly as today. These pin the narrowing so a later change cannot tighten it by
   accident.
9. A **DHCP** insert is unaffected — no refusal. Note (finding 13) that "no derivation attempted" is
   not observable from an exception-free create; if that half is wanted, it needs `assertNumQueries`,
   so this test asserts only what it can see.
10. `assertNumQueries` on materialization of a device with **two static ports on two VLANs, both with
    ranges, one at a non-zero offset**, pinning decision 3's accepted cost. The fixture shape is
    written down here so the measured number is reviewable; the existing counts (`tests.py:7003`,
    `:7035`, `:7068`, `test_ui.py:2990`) are all on `port_count=0` devices and do not move.

**Three existing tests keep passing for a new reason and must be made to say so (finding 5).** The
new verify sits before `_validate_static_address()`, and each of these wraps `full_clean()` in a bare
`assertRaises(ValidationError)`:

| Test | Line | Now raises for | Fix |
|---|---|---|---|
| `test_new_device_port_address_inside_dhcp_range_raises` | `tests.py:982` | mismatch, not DHCP-range overlap | move the fixture to a conformant address that still lands in the DHCP range, or `assertRaisesMessage` |
| `test_device_port_outside_rack_range_raises` | `tests.py:1419` | mismatch, not containment | same |
| `test_device_port_address_cannot_collide_with_switch_address_on_same_vlan` | `tests.py:1448` | mismatch, not cross-table collision | **put the colliding switch address at the device's own derived ordinal** — this is the only device-side test of `_validate_static_address()`'s switch-conflict branch (`:522-529`), which has no DB constraint behind it |

`test_device_port_address_manually_entered_without_rack_range_still_raises` (`tests.py:1381`) was a
fourth under revision 1; the narrowing leaves it untouched.

**Fixture churn policy:** a failing fixture is made conformant. No test-only escape hatch, for the
same reason decision 3 rejects a bypass flag. With the narrowing there is exactly one such fixture
(`test_ui.py:1522`), and PR 2 deletes it before this plan starts.

## Non-goals

- `NetworkSwitchAddress` — decision 7; a follow-up issue, not a code change here.
- The unracked / no-range insert gap — decision 2's accepted consequence.
- ADR and doc citations in other model-layer messages: the offset-0 delete guard's "(ADR 0010/0017)"
  (`models.py:5062`), `RackTemplateForm.clean_vlans()`'s "(ADR 0012)" (`admin.py:566`), and the
  spare-pool message's "(DHCP-configured per CONTEXT.md)" (`:5095`, `:5258`) — which is the same
  defect as an ADR citation under decision 5's own reasoning (finding 12). All predate this issue;
  sweeping them is a separate pass.
- Locking `RackVlanRange` to close decision 3's READ COMMITTED race.
- Anything about #91 beyond the single query-count assertion.
- `ROADMAP.md` — this enforces a committed decision rather than adding a phase.

## Verification

```bash
set -a; source .env; set +a
python manage.py test inventory
```

Green. Baseline count recorded before any code is touched. No migration to run.

## Risks and what could still be wrong

- **The `clean()` ordering is delicate.** The unracked check, the fill-if-blank branch and the new
  verify share one branch, and both flips must keep bypassing the verify. They do so by construction —
  both flips require `self.pk is not None` (`:4948`, `:5069-5071`), mutually exclusive with the insert
  gate — and the tests that actually prove it are the existing flip suite: `tests.py:2475`, `:2505`,
  `:2522`, `:2551`, `:2574`, plus `test_ui.py:3898`, `:4211`, `:4227`. Revision 1 named the DHCP-insert
  test as the evidence, which was wrong (finding 13).
- **The READ COMMITTED triple-derive race** in decision 3 — accepted, fail-closed, unfixed.
- **`assertNumQueries` is brittle by nature.** Pinned deliberately; if it fights a later change, the
  answer is to look at why the count moved.
- **The narrowing is a smaller fix than #99's title implies.** A non-conformant address on an unracked
  device or a range-less rack still enters the database silently. That is stated in decision 2 and in
  the issue's eventual closing comment, so the residue is on the record rather than assumed closed.

## Review response

| Note | Resolution | Section |
|---|---|---|
| 1 (P0) — churn is structural; 14 hard failures, 2 unfixable; count is 38 not ~31 | **Escalated, resolved with Mike.** Decision 2 narrowed, which removes 13 of the 14; the last (`test_ui.py:1522`) is deleted by ADR 0027 PR 2, behind which this plan is now sequenced. Corrected count and the historical-model distinction recorded | Sequencing; blast radius; decision 2 |
| 2 (P1) — `self.pk is None` leaves #99 reachable via explicit pk; module idiom is `pk is None or _state.adding` | **Folded.** Verified at `models.py:3040-3047` and five other sites. Both guards use the idiom; revision 1's "Known gap" note deleted as factually wrong. New test 5 | Decision 6; tests |
| 3 (P1) — new `clean()` verify lacks `device`/`vlan` null guards | **Folded.** Both guards required, with the `full_clean()`-after-`clean_fields()` reasoning | The change, item 3 |
| 4 (P2) — "deleted and recreated" is false; correctable via the flip, in the admin | **Folded.** Context rewritten; noted that it weakens decision 7's "correctable in place" framing without touching its scope argument | Context; decision 7 |
| 5 (P2) — four tests keep passing for a new reason | **Folded**, reduced to three by the narrowing. The cross-table collision test gets a specific rewrite rather than an assertion, since it is the only device-side cover for an unconstrained invariant | Tests |
| 6 (P2) — the reused message is misquoted and its wording is wrong on an insert | **Resolved by the narrowing** — decision 2 no longer raises those messages at all. Recorded rather than silently dropped | Decision 5 |
| 7 (P2) — triple derivation under READ COMMITTED is a new failure mode, not only cost | **Folded as an accepted risk**, with the reasoning (fail-closed, rack-range edits are rare) and the rejected alternative (lock `RackVlanRange`) | Decision 3; Risks; Non-goals |
| 8 (P2) — no test for the block-overflow cause | **Resolved by the narrowing** — overflow now falls through instead of raising. Test 8 pins the fall-through generally | Decision 2; tests |
| 9 (P2) — every core test is `slot_offset = 0`, so dropping the offset term passes | **Folded.** New test 6 on a non-zero offset | Tests |
| 10 (P3) — five line citations off by 1-2; one method cited at its body line | **Folded.** All corrected against `bb88d3c` | throughout |
| 11 (P3) — the "second gap" is the same gap, and `0023` tolerates unracked ports | **Folded**, and it became the strongest argument for the narrowing rather than a footnote | Decision 2 |
| 12 (P3) — the spare-pool message cites `CONTEXT.md`, which decision 5's own logic condemns | **Folded** as an explicit non-goal rather than an assertion of compliance | Non-goals |
| 13 (P3) — test 7 cannot observe "no derivation attempted"; the flip evidence is misnamed | **Folded.** Test 9 asserts only what it can see; the real flip tests are named in Risks | Tests; Risks |
| 14 (P3) — `address=""` slips past the `CHECK` | **Folded.** The guard gates on `is not None`, not truthiness, which refuses `""`. New test 7 | Decision 1; tests |
| 15 (P3) — existing query counts unaffected (confirmed), but the new one has no fixture | **Folded.** Fixture shape written into test 10 | Tests |
| E1 — decision 2 makes two `TakenAddressMarkerTests` fixtures unconstructible | **Escalated, resolved with Mike.** Narrowing rescues one; PR 2 deletes the class before this plan starts, so neither the escape hatch nor the coverage loss is needed | Sequencing; decision 2 |

Verified independently while folding: the module idiom and its comment; `NetworkDevicePortTests.setUp`
creating no rack; migration `0023`'s `device__rack__isnull=False` filter; `is_dhcp` remaining editable
in the admin inline; and that every assertion in `TakenAddressMarkerTests` is on `taken-by-label`, so
PR 2's deletion list takes the whole class.
