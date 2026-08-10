> **UNPAUSED — decisions only, no plan yet.** `ROADMAP.md` phase 17 was paused on 2026-08-07 behind
> the device-model rework and resumed on 2026-08-08 once
> `docs/adr/0022-add-in-cards-and-operator-set-ports.md` settled it. This file holds the decisions
> settled with Mike before the pause, amended where the ADR changed them. The implementation plan
> gets written after `docs/plans/PLAN-adr-0022.md`'s three PRs land.

# Hostname ingredients (ROADMAP.md phase 17) — settled decisions

## What the pause changed

Phase 17 asked where `docs/MORE_MUSINGS.md`'s `-engine` and `-device-control` hostname suffixes
should live. Answering that required deciding whether a DiGiCo SD12's audio engine is a *port* or a
*device* — and the answer turned out to be that the model's port/companion/independent-device
boundaries were drawn by the wrong criteria. That is ADR 0022, which sorts hardware by *optional* ×
*removable* instead.

**The outcome for this plan:** an SD12's engine and a Yamaha console's Device Control interface are
both **ports on their console**, so `hostname_suffix` goes exactly where the roadmap originally put
it — on `NetworkDeviceTypePort`, derived read-only onto `NetworkDevicePort`, storing nothing. Phase
18's "derived read-only port hostname property" is *reinstated*, not withdrawn.

**`hostname_suffix` and `validate_dns_label` ship ahead of this plan**, in ADR 0022's PR 1 — the
Device Control cannot fold into its console without a label, or production loses the name
`dm7c-1-device-control`. Decisions 6 and 7 below record their settled shape; this plan no longer
builds them.

## Settled with Mike, 2026-08-07

1. **One ADR covers the whole hostname scheme** — ingredients *and* computation/collision rules —
   written before the ingredients ship, with phase 18 implementing against it rather than writing a
   second record. This is the ADR 0021 pattern (settle both axes on paper, build one). It also puts
   the ADR 0018 decision-3 amendment where the contradiction was found. **Numbering:** the companion
   ADR took 0022, so the hostname ADR becomes **0023**.

2. **`Owner` is `slug` + `name`, both required and unique.** `slug`: `CharField(63)`, unique,
   non-blank `CheckConstraint`, DNS-validated. `name`: `CharField(100)`, unique, non-blank.
   `__str__` returns `"Mike Snow (mps)"` — composite like `NetworkSwitchType`'s, not bare like
   `Department`'s, so pickers stay readable while still showing the component that lands in the
   hostname. No `description` field; `name` carries what a description would.

3. **The rack-derived owner default fires in the admin add forms only.** `NetworkDeviceAddForm.clean()`
   and `NetworkSwitchAddForm.clean()` fill a blank `owner` from `rack.owner` — exact parity with the
   `rack_slot` suggestion three lines away (ADR 0019's suggest-don't-lock). Programmatic
   `objects.create()` gets nothing, so existing tests construct exactly what they construct today.
   The change form never re-derives on a rack move. *(Amended by ADR 0022: the companion that used
   to copy its host's owner at materialization no longer exists. An add-in card is created through
   the ordinary add form and gets the same rack-derived default as anything else, which is the
   simpler answer the companion case was working around.)*

4. **`Rack.location_slug` is `CharField(63, null=True, blank=True, unique=True)`**, with `""`
   normalised to `None` in both `clean()` and `save()`.
   **Do not use a conditional `UniqueConstraint`** — this backend reports
   `supports_partial_indexes = False`, and Django 6.0.7's `_create_unique_sql()` returns `None` in
   that case, so the migration emits *no SQL at all* and the model claims a constraint the database
   does not have. `null=True` + `unique=True` is DB-enforced because MySQL permits unlimited NULLs
   in a unique index, and Django's own field docs name this as the one exception to "avoid null on
   string fields".

5. **`hostname_slug` is never auto-filled.** The roadmap's "prefilled by slugifying the model"
   contradicts the bullet below it ("blank means that Type offers no computed hostnames") — if blank
   auto-fills, blank is unreachable. This project has no JavaScript, so a prefill could only happen
   server-side after submission where nobody reads it, and `slugify("IK-42")` gives `ik-42` where
   the name in use is `ik42`. A wrong component nobody read is worse than a missing one, especially
   once hostnames are computed at materialization and stored. The example goes in `help_text`
   instead, including the trap.

6. **`hostname_slug` is not locked** on either Type model once instances exist — a stored, seed-once
   hostname cannot drift from it, and a typo'd abbreviation must stay fixable without creating a new
   named profile. *(ADR 0022 decision 4 applies the same reasoning to `hostname_suffix`, which is
   exempt from `NetworkDeviceTypePort`'s profile lock for the sharper reason that a derived label
   has no materialized counterpart to drift from. Shipped in ADR 0022's PR 1.)*

7. **One shared `validate_dns_label` in `validators.py`**, beside `validate_ipv4_cidr` — **shipped
   by ADR 0022's PR 1**, which needs it for `hostname_suffix`; this plan imports it:
   `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`, max 63. Values are stripped **and lowercased** in both
   `clean()` and `save()`, so `"MPS "` stores `"mps"` rather than erroring. This departs from
   `Department.name`, which strips but deliberately does not casefold — the difference is that
   Department's case is a display concern the collation handles, whereas an uppercase slug would be
   concatenated verbatim into a hostname and produce wrong output.
   Suffixes are stored **bare** (`engine`, not `-engine`); the join character is supplied at
   assembly. `max_length=63` on each component, and **no per-component cap can guarantee the
   assembled name fits in 63** — `mps-wpcsrl-ik42-sub-1` is a single label with no dots — so total
   length validation is phase 18's, and ADR 0023 should record that as a named gap. Phase 17 adds no
   validation to the existing `hostname` fields (`CharField(255)`, no validator, possibly-illegal
   existing rows).

8. **Full read-parity UI**, matching what `Department` got in phase 16.
   **Structural note:** `Rack` and `NetworkDevice` both set `canonical_detail_view`, so
   `model_detail` *redirects* to the shaped `rack_detail.html` / `device_detail.html` and their
   registry `detail_fields` never render. `Rack.location_slug`, `Rack.owner` and
   `NetworkDevice.owner` are therefore invisible unless added to those hand-written templates —
   and since Stage C moved Viewers out of the admin, invisible there is invisible full stop.
   Also: `RackAddForm.Meta.fields` is an explicit list and will silently drop `location_slug`.
   The UI guards iterate `REGISTRY` slugs, not model fields, so a new *field* breaks no test but the
   new `owner` *entry* carries Department's enumerable cost (query-budget factory, lockout markers,
   writes-nothing routes, partial-grant codenames).

9. **No production backfill.** Phase 17 ships fields, not data. Parsing `mps-wpcsrl-ik42-sub` to
   recover components is inference, and the spreadsheet has no owner or location column — the same
   reason phase 16 declined seeding departments. The sharper reason: phase 18 recomputes hostnames
   from components, so back-deriving components *from* existing names would make that first
   recompute reproduce them by construction, masking the divergences `MORE_MUSINGS.md` already lists
   (rack name always enforced; SD12 names carrying `-control`).

## Also settled

~~`ADR 0018` gets no "amended by" banner for the companion-hostname rule until phase 18 actually
changes the behaviour.~~ **Moot** — ADR 0022 supersedes ADR 0018 outright, and the companion whose
hostname copied its host's verbatim no longer exists. Nothing in phases 17 or 18 needs to amend it.
