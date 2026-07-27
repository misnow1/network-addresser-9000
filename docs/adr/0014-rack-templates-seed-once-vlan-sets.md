# Rack Templates: seed-once VLAN sets, not a live-referenced profile

Many racks are the same *kind* of rack — an "Audio Rack" always gets VLANs 200/201/202, a
"Video Rack" always gets the video VLANs — but today every rack's VLAN ranges are built by
hand, one `RackVlanRange` at a time. This closes the VLAN-only scope of #23: a **Rack
Template** is a named, reusable set of VLANs (plus an optional default slot count) that, when
applied at rack creation, allocates a `RackVlanRange` for each listed VLAN in one step.

A larger version of the same discussion — templates that also lay out *which equipment* goes
in which slot and materialize real switches/devices, plus auto-generated hostnames for that
equipment — is deliberately **out of scope here** and split into #30 and #31. See
`DESIGN.md`'s "Deferred: Populated Rack Templates and Hostname Templating" for why: a
populated template needs FKs to `NetworkSwitchType`/`NetworkDeviceType`, which makes it a new
kind of Type dependent (ADR 0007) — a materially different shape from the VLAN-only case this
ADR covers.

## The constraint that shapes the design: seed-once, not live-referenced

This codebase now contains two different patterns for "an instance copies configuration from a
named, reusable thing":

- **ADR 0010's materialization pattern** — a Type's port templates are copied *exactly once*
  into real instance ports when the instance is first created, then never re-synced. Editing
  the Type afterward (largely blocked anyway, since it locks once in use) has no effect on
  existing instances.
- **ADR 0012's live-reference pattern** — a `SwitchPortVlanProfile` is referenced by ongoing
  identity, not copied. Editing a profile's allowed VLANs changes every port using it
  immediately, including ports on switches that already exist. This was a deliberate departure
  from the materialization pattern, made because a trunk's whole reason to exist is picking up
  newly-allowed VLANs after it's deployed.

Rack Templates follow **ADR 0010's pattern, not ADR 0012's**. Applying a template to a new rack
copies its VLAN list into real `RackVlanRange` rows at that moment; editing a template's VLAN
list afterward — adding or removing which VLANs it references — has **no effect on any rack
already created from it**. (This is scoped to the template's *membership list* only: a
`RackVlanRange` keeps a live FK to its VLAN regardless of templates, as it always has, so
renaming or otherwise editing a VLAN object itself remains immediately visible on every existing
range that references it — templates don't change that and don't snapshot VLAN properties.)
This is deliberate, not an oversight: unlike a switch port profile, a rack's address ranges are
the kind of fact this tool exists to keep stable and auditable (ADR 0004). A template that
silently reshaped existing racks' addressing every time it was edited would be exactly the kind
of untraceable mutation the audit trail exists to prevent.

## Decisions

1. **A Rack Template has a unique `name`, an optional `slot_count`, and an explicit list of
   VLANs** — mirroring `SwitchPortVlanProfile.name`'s uniqueness (`inventory/models.py:1080`).
   Uniqueness relies on the database's own collation for case-folding — this project's MariaDB
   database uses `utf8mb4_uca1400_ai_ci` (accent- and case-insensitive, confirmed against the
   dev database), so `"Audio Rack"` and `"audio rack"` already collide as a DB-level duplicate —
   but the DB does **not** trim whitespace, so the implementation must `.strip()` the name before
   validating uniqueness, or `"Audio Rack"` and `"Audio Rack "` would coexist as confusingly
   near-duplicate templates. There is no dynamic "all VLANs" flag and no VLAN category/tag
   concept; a template that wants "all the infra VLANs" lists them explicitly. Seed-once already
   means a dynamic flag would only ever affect racks created *after* a VLAN is added to the
   flagged set — the convenience is smaller than it looks, and an explicit list keeps "what will
   this template do?" answerable by reading the template. **No cap is placed on how many VLANs a
   template may list** — the "Infra Rack" example above (every currently-defined VLAN) is
   deliberately open-ended. This directly widens the exposure window of the Known-gap
   concurrency issue below and the per-VLAN allocation cost decision 7 describes; both are
   accepted for this tool's current scale (see Known gap, and the scaling note in Follow-up)
   rather than bounded here.

2. **VLAN membership uses an explicit through model with a `PROTECT` foreign key to VLAN**, not
   a plain `ManyToManyField` — the same reason `SwitchPortVlanProfileAllowedVlan`
   (`inventory/models.py:1363`) and `NetworkDeviceTypePort.vlan`'s M2M-avoidance exist: a VLAN
   referenced only through an auto-generated M2M join table can't be protected from removal at
   all, since Django's deletion collector doesn't traverse plain M2M relations (ADR 0007). The
   through model also carries a `unique(template, vlan)` constraint — the same reason
   `RackVlanRange` carries `unique(rack, vlan)` (see Known gap below) — so the same VLAN can't be
   listed twice on one template. **Deleting a VLAN that a template lists is therefore blocked**
   until it's removed from every template that references it — consistent with every other VLAN
   FK in this codebase.

3. **A subnet-less (L2-only, ADR 0012) VLAN cannot be added to a Rack Template, and blanking a
   VLAN's subnet is blocked while a template lists it.** A `RackVlanRange` can never target an
   L2-only VLAN (`RackVlanRange.clean()` already rejects this), so listing one in a template is
   meaningless — better to reject the mistake where it's made (editing the template or the
   VLAN) than to discover it later, either by silently skipping the VLAN at rack-creation time
   or failing there instead.

4. **A Rack Template is itself freely deletable.** Its VLAN-membership rows cascade with it.
   This is the direct answer to the question #23 actually asked, and it is the *inverse* of
   decision 2: a template protects its member VLANs from deletion, but nothing protects the
   template itself, because — decision 5 — nothing refers back to it once a rack exists.

5. **A Rack keeps no reference to the template it was created from.** No FK, no stored
   provenance. This is what keeps "a Rack has no purpose field" (`CONTEXT.md`) true even though
   template names (Audio Rack, Video Rack, …) are themselves purposes — the *template* carries
   the purpose, the *rack* it produces carries only ordinary `RackVlanRange` rows
   indistinguishable from ones entered by hand. It also makes seed-once structural rather than
   merely documented: there is no FK for a future "re-sync from template" feature to hang off,
   and it's what makes decision 4 possible without a `PROTECT`/dependent-check story.

6. **Applying a template at rack creation is optional; there is no system-seeded "Default"
   template.** ADR 0012 needed a system-seeded default profile because
   `NetworkSwitchTypePort.profile` is a required, non-null field — there was a hole that had to
   be filled on every row. No equivalent hole exists here: a rack with zero `RackVlanRange` rows
   is already legal today, and stays legal — and by the same logic, **a Rack Template with zero
   listed VLANs is legal too** (it just seeds nothing when applied, equivalent to not applying a
   template at all).

7. **Applying a template constructs each `RackVlanRange` with a blank `address_range`, calls
   `full_clean()` on it, and only then saves it — all inside the rack-creation transaction.**
   `Model.save()`/`objects.create()` never call `clean()`/`full_clean()`, and `RackVlanRange`
   has no `save()` override, so constructing a row and saving it directly would persist
   `address_range=""` on a NOT NULL column rather than triggering the existing suggestion
   logic. This is the same construct-blank → `full_clean()` → `save()` sequence ADR 0013
   already specifies for `_materialize_ports()`'s static device-port addresses, applied here to
   `RackVlanRange` instead. ADR 0001 is untouched by this: its decision was that ranges are
   "not derived from a formula tied to rack number," and the suggestion path this reuses
   (`suggest_rack_vlan_range`, called from `RackVlanRange.clean()`) is already the system's
   normal way of proposing a range — a template just triggers it for several VLANs in one
   request instead of one.

   `slot_count` must be resolved onto the Rack *before* its `RackVlanRange` rows are
   constructed, since `suggest_rack_vlan_range()` sizes the suggested block from
   `rack.slot_count`.

8. **"All-or-nothing" means one request never leaves a partial rack — nothing stronger.** If
   any listed VLAN's range can't be allocated (no block of the required size is free), the
   entire rack-creation request rolls back: no rack, no ranges, an error naming the VLAN that
   failed. This matches ADR 0010's atomic parent-plus-children materialization and ADR 0013's
   atomic refusal of Switched-Mode devices. See "Known gap" below for what this promise does
   **not** cover.

9. **`Rack.slot_count` stays a required field on `Rack` itself.** A template's `slot_count`, if
   set, supplies only an **overridable initial value** on the rack-creation form; a template
   with no `slot_count` leaves the field exactly as unfilled as it is today. Neither
   `Rack.slot_count` nor a template's `slot_count` is bounded below or above today —
   `Rack.slot_count` is a plain `PositiveIntegerField` (`inventory/models.py:710`) with no
   `MinValueValidator`, so `0` is already legal on `Rack` itself, independent of this feature.
   This is a pre-existing gap, not one this ADR introduces, but since a template's `slot_count`
   feeds directly into `suggest_rack_vlan_range()`'s block-size computation, a future
   implementation plan should decide whether to finally bound it (e.g. `>= 1`) rather than
   silently inheriting the ambiguity.

10. **Template application is a domain operation, not an admin-only convenience.** It must be
    reachable from programmatic rack creation, not only through the admin add view — the same
    rule ADR 0013 states for `port_addressing`: "a domain rule, not a UI quirk that only the UI
    knows about." The choice of which template (if any) to apply is transient at creation time,
    the same way ADR 0013's `port_addressing` is: no stored field records which template a rack
    used, consistent with decision 5.

11. **A template may be combined with manually-entered `RackVlanRange` inline rows on the same
    rack-creation submission.** Template rows materialize first; if a manual row names a VLAN
    the template already covers, that is a **validation error naming the conflicting VLAN** —
    not silent precedence in either direction, and not a raw `unique_rack_vlan_range`
    `IntegrityError` surfacing after materialization has already partially run.

12. **`RackTemplate` and its VLAN-membership rows are ordinary `AuditedModel`s, and their edits
    are in scope for ADR 0004's mutation logging, not just creation.** A template's VLAN list
    determines every future rack seeded from it — editing that list (even though, per the
    seed-once rule above, it can never reshape an already-created rack) is exactly the kind of
    operationally meaningful change ADR 0004's edit/removal logging exists to make traceable,
    the same as a rack/slot reassignment. It is not exempted just because it's seed-once.

## Known gap, unchanged: concurrent range allocation is not locked

`RackVlanRange.Meta.constraints` enforces only `unique(rack, vlan)` — there is no database
constraint on `address_range` itself, and `suggest_rack_vlan_range()`'s read of sibling ranges
on a VLAN is unlocked. Two concurrent rack-creation requests (whether template-driven or
manual) can each observe the same free block as available and both commit it, producing
overlapping ranges. This is **pre-existing and accepted**, in the same terms ADR 0013 documents
for the cross-table switch/device address race (#5): a Rack Template exercises this gap more
often, by turning what used to be N separate manual range-creation actions into one, but it
does not introduce the gap or worsen it in kind. Decision 8's "all-or-nothing" promise is scoped
to mean *one request is atomic with itself*, not that concurrent requests are serialized against
each other.

Closing this properly would mean a shared allocation-lock protocol — locking the affected
VLAN rows in a stable order before suggesting any range, the same pattern `_lock_profile_rows()`
/`_lock_type_rows()` already use for their own TOCTOU races — used by *both* template
application and today's plain manual range creation, since the race exists on the manual path
regardless of whether this feature ships. That protocol would not be free: a template holding N
VLAN row locks for the duration of one transaction (vs. today's one lock per one-VLAN manual
add) multiplies both lock footprint and hold-time by template size, raising contention risk
against any other rack creation touching an overlapping VLAN set — worth weighing against the
gap it closes, not just adopting reflexively. That is a larger change than VLAN-only Rack
Templates and belongs in its own future ADR if it's ever prioritized.

## Follow-up

Building `RackTemplate` (model, through table, migration, admin, and the domain-level apply
operation described in decisions 7–11) is intentionally **not** part of this ADR. It gets its
own implementation plan, independently reviewed per this project's plan-review convention,
before it's built. That plan must include test coverage for both halves of decision 7: a
successful suggestion through the construct-blank → `full_clean()` → `save()` path, and a
rollback when `full_clean()` cannot suggest a range for one VLAN among several. It must also
cover decision 2's `unique(template, vlan)` constraint and decision 1's name-normalization rule
with tests of their own.

**Scaling note, not a blocker:** decision 7's per-VLAN `full_clean()`+`save()` sequence is
several sequential DB round-trips per VLAN (FK-existence checks, the sibling-range read, the
uniqueness check, the insert) — negligible for a handful of VLANs, but a template with dozens or
more (this ADR's own "every currently-defined VLAN" example is the worst case) turns that into a
long single transaction. Not a concern at this tool's current scale; worth a look if template
size or site VLAN count grows substantially.

Populated slot layouts (equipment materialization from a template) and hostname templating for
that materialized equipment are tracked separately as #30 and #31.
