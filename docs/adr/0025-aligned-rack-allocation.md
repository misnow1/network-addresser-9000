# Aligned rack allocation: one offset per rack, not one first-fit per (rack, VLAN)

`PROD-DATA-ANALYSIS.md` §6.1 names a gap this tool has carried since ADR 0001: the production
spreadsheet gives each rack a single `Address Offset` applied to every VLAN base, which
*guarantees* a device's Control, Dante Primary and Dante Secondary addresses share a host
portion. This tool allocates per `(rack, VLAN)` by independent first-fit
(`suggest_rack_vlan_range()`), so that guarantee holds only by coincidence — while every VLAN a
rack sits on happens to have identical sibling ranges, identical DHCP geometry and identical
creation order. One VLAN with a DHCP range its neighbours lack, one rack deleted and recreated,
one hand-entered range on a single VLAN, and the host octets silently diverge. Nothing detects
it.

Rack Templates (ADR 0014) and the `/27` floor (ADR 0015) both make divergence *less likely* —
the first by allocating a rack's VLANs in one ordered pass, the second by removing the per-VLAN
block-size variation that made first-fit results diverge most easily — but neither closes the
gap, and §6.1 is explicit that neither should be read as if it did.

This matters more than it looks because audio is the pilot, not the scope
(`PROD-DATA-ANALYSIS.md` §7.1): video and lighting VLANs will be added to racks that already
exist, which is precisely the path that reintroduces divergence — a rack created when it held
only Control and Dante Primary, joined later by a Dante Secondary VLAN whose DHCP geometry
differs, is exactly the shape of the point test below.

## The invariant is the offset from the VLAN's network address, not the third octet

It is tempting to describe the goal as "every device in a rack shares a third octet," because
that is what the production data looks like. It isn't the rule. **16 of the 21 production racks
don't start on a `/24` boundary** — six of them share third octet `1` — and `WPC1SRU`'s offset of
288 spans two octets (`1`×256 + `32`). The invariant is *the rack block's offset from its VLAN's
network address*, which only looks octet-shaped because `/21` subnets and 32-address blocks
happen to keep each rack inside a single `/24`. An offset-based rule survives a VLAN that isn't a
`/21`; a third-octet rule doesn't.

## Static addresses only, and this needs no special handling for it

Alignment is a property of rack VLAN ranges, and therefore of the static addresses computed from
them. A DHCP port stores no address at all — the `device_port_dhcp_xor_static_address`
constraint forces `address = NULL` when `is_dhcp` is set — so there is no DHCP address for this
guarantee to cover or to get wrong. And because the report this ADR adds is rack-level (decision
5), not device-level, the "ignore DHCP ports or every mixed device gets flagged" hazard
`PROD-DATA-ANALYSIS.md` §6.1 warns about for a *device*-level check cannot arise here at all:
there is no per-port reasoning to get wrong.

## Decision

### 1. Allocate once, and stick to it: both batch and sticky

A rack's offset is the lowest offset free on **every** VLAN it is getting a range on at the
moment those ranges are created — whether that's several VLANs from a Rack Template, several
inline rows on the rack-creation form, or both together. That is the *batch* half.

The *sticky* half: a blank range added later to an already-existing rack — a video VLAN arriving
after the rack was created audio-only, say — adopts that rack's established offset, if a block at
that offset is still free on the new VLAN. It does not re-run a fresh joint search over every VLAN
the rack happens to touch; it reads what the rack already agreed to and tries to extend it.

### 2. Never guess an offset when the rack disagrees with itself

If a rack's existing ranges don't already agree on one offset, there is no "the rack's offset" to
be sticky about. The sticky path (decision 1) falls back to today's per-VLAN first-fit rather than
picking one of the disagreeing values arbitrarily, and the report (decision 5) is what surfaces the
disagreement. Guessing here would launder an already-misaligned rack into looking like a
considered choice.

### 3. Fall back, and say so

When no offset is free on every VLAN being allocated — one VLAN's aligned slot is already taken by
something else, say — allocation falls back to per-VLAN first-fit exactly as it works today, and
records a non-blocking advisory naming which VLAN landed on which offset. **Suggest, don't
enforce**, unchanged from ADR 0001: a hard constraint here would be the first place this system
refuses something an operator may legitimately need — importing a site that's already misaligned,
or a VLAN whose subnet is too small for the aligned offset — and aligned-by-default already
achieves the outcome in every case that matters. The existing blocking pre-flight in
`Rack._check_template_application_possible()` is untouched: it still refuses "this VLAN has no
free block of the required size at all," a different condition from "the racks' VLANs can't all
agree on the same one."

### 4. No "realign this rack" admin action

Declined, so it isn't re-proposed. A device's address is stored, not derived (ADR 0003) — the
device sitting in a rack slot holds whatever address was written into it, on whatever range
existed at the time. Rewriting a rack's ranges to force alignment would leave every device already
in that rack holding an address outside its own VLAN's new range, silently. The report (decision 5)
exists precisely because the honest fix is telling an operator a rack is misaligned, not silently
reshaping their addressing out from under already-racked equipment.

### 5. A rack-level, read-only report — mirroring `hostname_diverges`

`Rack.range_offsets_diverge` — a stateless property, modelled directly on
`NetworkSwitch.hostname_diverges` (ADR 0023 decision 9): `True` when the rack's ranges don't all
share one offset. A rack with zero or one range is never divergent — there's nothing to disagree
with itself about. Surfaced as an admin `list_display` column, a `SimpleListFilter` copied from
the existing hostname-divergence filter, and read-only markers in the purpose-built UI (ADR 0020):
the index tile and the rack elevation page. No new route — this rides the same pages that already
render a rack.

**Device-level divergence is deliberately out of scope here.** It's a real symptom, but one with
two legitimate causes already recorded elsewhere — ADR 0017's `slot_offset` (an SD12's engine
address is *supposed* to differ from its control address by a fixed amount) and ADR 0022's
operator-set ports (a Yamaha Device Control interface's address is chosen independently). Telling
those apart from genuine drift is issue #28's territory, not this one's; folding it in here would
either misreport ADR 0017/0022's intentional shapes as bugs, or need this ADR to re-litigate #28's
design to avoid that. Rack-level divergence needs neither: it only asks whether the ranges
themselves agree.

### 6. The search is scoped to this rack's own VLANs

Aligned allocation only ever considers the VLANs a given rack is being (or has been) given ranges
on — never every VLAN in the system. A rack that never touches Lighting has no reason for its
offset choice to be constrained by Lighting's occupancy, and searching the whole VLAN set would
make one rack's allocation depend on unrelated addressing elsewhere.

### 7. The inline formset aligns too

A rack created through the admin's Add page, with a template chosen *and* manually-entered inline
VLAN ranges on the same submission, computes one joint offset across the template's VLANs and the
manually-entered blank rows together — not the template's own offset followed by an independent
first-fit for the inline rows. Otherwise a fourth VLAN added by hand in the same request as a
templated rack would silently pick its own offset instead of the rack's, defeating decision 1's
"batch" half in the one case it exists to cover. This is mechanically the harder of the three
allocation paths — see the ADR's implementation plan for how Django's actual form/formset
validation order makes it work.

### 8. This amends ADR 0001; nothing else about it changes

ADR 0001's stances survive intact: the system suggests a range and an admin can override it, and
once set a range does not recompute automatically. The only thing this ADR changes is **what the
suggester searches for** — a joint offset across a rack's VLANs in place of an independent
first-fit per `(rack, VLAN)`. Everything ADR 0001 says about manual override and static-once-set
still governs the range that search produces.

### 9. Department scoping stays declined

`ROADMAP.md` phase 19 already gives three reasons; they're restated here because this ADR is where
a reader would look for them. ADR 0014 decision 1 declined the nearest thing to department-scoped
templates deliberately. All 21 production racks carry only audio VLANs today, so department-scoped
and global alignment are identical on the only real data this tool has. And the spreadsheet's own
model — the thing being replaced — is one offset per rack applied to *every* VLAN base, so scoping
alignment by department would depart from current practice rather than formalise it. Declined here
too, for the same reasons, so it isn't re-proposed alongside this change.

## What this does not cover

- **Device-level divergence detection** (decision 5) — issue #28's territory, deliberately, for
  the reasons given there.
- **A "realign this rack" action** (decision 4) — deliberately declined; addresses are stored, not
  derived.
- **Department-scoped alignment** (decision 9) — deliberately declined, restating
  `ROADMAP.md` phase 19's reasons.
- **Concurrent allocation locking.** `suggest_rack_vlan_range()`'s read of sibling ranges was
  already unlocked before this ADR (ADR 0014's own "Known gap"), and the joint search inherits the
  same posture rather than closing it — two concurrent requests can still each observe the same
  free offset as available and both commit it. Not worsened in kind by this change, just exercised
  by one more code path.
- **Address regions.** Production's offset gaps before `SHURE` and `CONSOLES`
  (`PROD-DATA-ANALYSIS.md` §7.2) are mnemonic, not technical, and issue #43 (the DHCP range
  recorded wider than reality) is the same class of gap — both still want a "reserved but
  unallocated" concept `ROADMAP.md`'s "Later" section tracks separately. Aligned allocation
  consumes those gaps exactly as today's first-fit does; this ADR doesn't change that.

## Consequences

- **No schema change.** `RackVlanRange`, `Rack` and every stored range are untouched — this is a
  suggester change plus a read-only report.
- **The suggester's contract changes for every new rack.** A rack created going forward gets one
  offset across all the VLANs it's allocated on at creation, instead of the lowest offset
  independently free on each. Every already-existing `RackVlanRange` is unaffected: nothing
  recomputes on this ADR landing, consistent with ADR 0001's "static once set."
  `PROD-DATA-ANALYSIS.md`'s replay shows this reproduces the same production bases the current
  first-fit already does, for the same reason ADR 0015's floor does: the three automatic
  production VLANs are identical `/21`s with identical relative DHCP geometry, so nothing about a
  joint search can move where an already-honest replay lands.
- **A misaligned rack becomes visible for the first time**, via the admin column/filter and the
  read-only UI markers — where before it was silent regardless of cause.
- **Video and lighting VLANs joining an existing audio-only rack are now more likely to land
  aligned** than they would under independent first-fit, which is the scenario `PROD-DATA-ANALYSIS.md`
  §7.1 names as the one audio's pilot status is standing in for.
