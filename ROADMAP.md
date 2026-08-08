# Roadmap

High-level phases only — day-to-day task tracking belongs in GitHub Issues once there's code to file issues against. This file exists so it's obvious what phase the project is in and what's next, even after a fresh start.

**Current phase: 17 — scoped, ADR not yet written. Phases 1–6 and 8–16 done; phase 7 scoped but skipped.**

## 1. Foundation — done

- [x] Design narrative (`DESIGN.md`)
- [x] Domain glossary (`CONTEXT.md`)
- [x] Architecture decisions (`docs/adr/`)
- [x] README

## 2. Django scaffolding — done

- [x] Project setup
- [x] Models matching `CONTEXT.md` (VLAN, Rack, Network Switch/Device + Types, Ports)
- [x] Initial migration
- [x] Admin registration for all models

## 3. Core domain logic — done

- [x] Address-range suggestion (rack ranges, VLAN gateway) — see ADR 0001, ADR 0002. VLAN DHCP-range suggestion was later removed — see ADR 0011.
- [x] Overlap validation (rack ranges vs. each other and the DHCP start/end range)
- [x] Device address default-and-override behavior — see ADR 0003
- [x] Removal semantics: block non-empty containers, unassign on leaf removal — see ADR 0007

## 4. Access and accountability — done

- [x] Local auth, three roles (Viewer / Editor / Admin)
- [x] Mutation audit trail — see ADR 0004, ADR 0008
- [x] "Big scary prompt" confirmation flows for removal

## 5. Deployment — done

- [x] Dockerfile
- [x] docker-compose (app + MariaDB)

## 6. Process hardening — done

- [x] Pre-commit hooks (formatting/linting)
- [x] GitHub Actions CI (tests, lint)
- [x] Branch protection on `main` — require PRs, block direct pushes

## 7. Container publishing

- [ ] GitHub Actions workflow: build and publish the Docker image to GHCR on `main` merges

Deliberately left unchecked and out of order rather than renumbered — it was scoped, then
skipped, and hiding that would only make it harder to notice.

## 8. Port profiles & materialization — done

- [x] Network Switch/Device Type is a purpose profile: `(manufacturer, model, name)` identity — see ADR 0010
- [x] `NetworkSwitchTypePort`/`NetworkDeviceTypePort` templates; materialized once into instance ports on creation
- [x] Type immutable after creation; a profile's type ports lock once it has any instance
- [x] `allowed_vlans` moved to an explicit `PROTECT`-FK through model (closes a VLAN-removal gap)
- [x] Device port `default_gateway` is a derived read-only property, not a stored field

## 9. Switch port VLAN profiles — done

- [x] `SwitchPortVlanProfile`: reusable, *live-referenced* Port Mode/Native VLAN/Allowed VLANs/Allow All VLANs bundle — see ADR 0012
- [x] `NetworkSwitchTypePort`/`NetworkSwitchPort` point at a profile instead of carrying their own VLAN config
- [x] Port Mode/Native VLAN lock once a profile has any real switch port; Allowed VLANs/Allow All VLANs stay editable
- [x] System-seeded "Default" profile and subnet-less "Default VLAN" (VLAN 1); `VLAN.subnet` is now optional (L2-only VLANs)
- [x] `seed_defaults` management command re-seeds the system rows if removed outside a migration

## 10. Static-by-default device port addressing — done

- [x] Device creation offers a DHCP-or-static choice, defaulting to static, computed rack-range-base + rack-slot per port VLAN — see ADR 0013 (closes #24)
- [x] Transient, never-stored `NetworkDevice.port_addressing` — a writable property, not a field or plain class attribute
- [x] Unracked devices and L2-only-VLAN ports always materialize DHCP regardless of the choice
- [x] Static materialization refuses Switched-Mode-shaped devices (duplicate VLAN across ports) atomically, with a clear error
- [x] Admin add form exposes the choice (creation-only); change form omits it

## 11. Rack Templates — done

- [x] Design decided: named, reusable VLAN sets seeding `RackVlanRange` rows at rack creation, seed-once (not live-referenced) — see ADR 0014, closes the VLAN-only scope of #23
- [x] `RackTemplate` model + `PROTECT`-FK through model for VLAN membership + migration
- [x] Admin: template CRUD, and a creation-only template picker on the Rack add view
- [x] Domain-level apply operation (construct-blank → `full_clean()` → `save()` per VLAN, all-or-nothing, reachable from programmatic creation — not admin-only)
- [x] Tests: successful suggestion via the template path; rollback when one of several VLANs can't be allocated

## 12. Production data validation — done

Three CSVs exported from the production spreadsheet were validated against the code's own
arithmetic. The formula matches exactly (259/259 address assignments), which turned the
exercise into a search for missing *rules* rather than missing features — see
`PROD-DATA-ANALYSIS.md`.

- [x] Validation: 259/259 assignments match `base + slot`; all 21 rack bases match `VLAN base + offset` — see `PROD-DATA-ANALYSIS.md`
- [x] Rack address blocks are never smaller than a `/27` — see ADR 0015 (reproduces 19 of 21 production rack bases automatically, vs. 1 today)
- [x] Switches materialize VLAN addresses at creation from the rack's ranges — see ADR 0016
- [x] Derived same-VLAN addresses via per-port slot offsets, for DiGiCo control+engine consoles — see ADR 0017 (partially supersedes ADR 0003)
- [x] Implement ADR 0015 — one-line change to `required_block_size`, three existing tests to update
- [x] Implement ADR 0016
- [x] Implement ADR 0017 — `slot_offset`, derived offset-port addressing, span-aware rack-slot occupancy, and the narrowed same-VLAN pre-flight; see `docs/plans/PLAN-adr-0017.md`
- [x] Production import — see `docs/plans/PLAN-prod-import.md` (revision 3), implemented as `manage.py import_prod_data` plus an independent `manage.py verify_prod_import`. Run against the real export: **183 addresses placed, 161 byte-identical, 2 differing by design (the DMI-DANTE pair), 20 correct-but-unrecorded switch addresses** — the plan's prediction exactly, 19 of 19 automatically-allocated rack bases reproduced, and every other `## Verification` check green. Two known gaps carried forward, neither blocking: the Netgear model (defers 3 of 23 switches to a second pass) and the per-device-type wiring rule for patch-panel-fed devices. Four items filed as `deferred`: #41, #42, #43, #44

## 13. Device companions — done

Hardware that comes in two independently-addressed pieces, one of which cannot exist without
the other — a Yamaha DM7C/DM3 console and its Device Control Interface. The production import
created both as unlinked devices because the model has no way to say one requires the other;
`slot_offset` is deliberately not that way (ADR 0017's scope boundary).

- [x] Design decided: a type declares a `companion_type`, the host materializes its companion at
      creation, deletion cascades from the host and is refused from the companion, and the pair
      moves as a unit — addresses stay independent throughout. See ADR 0018 (extends ADR 0007's
      removal rules and ADR 0010's materialization; does **not** close #42)
- [x] Implement ADR 0018 — schema + backfill migration, companion materialization in
      `NetworkDevice.save()`, the delete guard and its queryset twin, host-managed companion
      placement in the admin, and the importer/verifier pairing pass; #42 stays open — see below

## 14. Rack is the address pool; the ordinal is suggested — done

`docs/RACK-MUSINGS.md` asked whether arbitrary "address pools" should replace racks. They
shouldn't — the misalignment that motivated the question comes from dense per-VLAN
allocation, not from racks, and the existing ordinal already guarantees a device's addresses
line up across every VLAN its rack carries.

- [x] Design decided: no new grouping concept, no rename; suggest the ordinal instead of
      requiring it typed, and reserve offset space with empty racks — see ADR 0019
- [x] Suggestion helper: lowest free run of `slot_span` consecutive ordinals in a rack
- [x] Wired into the `NetworkSwitch` and `NetworkDevice` admin add forms as an initial value
      (a default, not a lock — ADR 0001, ADR 0003)
- [x] Tests: a plain device takes the lowest free ordinal; a spanning device (ADR 0017) skips
      a run that would overlap; an operator-typed ordinal still wins

No migration and no model change — nothing stored is altered.

## 15. Read-only purpose-built UI — done

The frontend `DESIGN.md` deferred until real usage showed which views were worth building.
Read-only, mounted at `/`, with every mutation deep-linked into the admin — see ADR 0020.

- [x] Design decided: strictly read-only v1, admin retains all mutation, Viewers leave the
      admin — see ADR 0020
- [x] Prerequisites: `LOGIN_URL`/`LOGIN_REDIRECT_URL` (Django's default is unrouted),
      `TEMPLATES["DIRS"]`, admin link gated on `is_staff` — the `TEMPLATES["DIRS"]` item is
      closed by *not* doing it: `LOGOUT_REDIRECT_URL = "/"` sidesteps
      `django.contrib.admin`'s `registration/logged_out.html` entirely rather than needing a
      project-level dir to out-order it (`docs/plans/PLAN-read-only-ui.md` decision 6)
- [x] The four shaped views: rack elevation, address map, device, spare pool
- [x] Read-parity: plain views for the remaining registered models and their inlines
- [x] Audit-trail view over `auditlog` — site-wide and per-object
- [x] Flip Viewer provisioning to `is_staff=False` (gated on parity + the audit view landing)

---

Phases 16–21 were scoped from `docs/MORE_MUSINGS.md` in a design pass on 2026-08-06. That pass
made one framing decision that shapes all of them: **model work comes before any further UI
work.** Every UI item in `MORE_MUSINGS.md` — live JavaScript, the rack creation wizard, the
password change form — is parked in Later behind a single gate, so the data model settles before
a second frontend is built on top of it.

## 16. VLAN metadata: Department — done

`CONTEXT.md` describes VLANs as flat — an 802.1Q ID plus its IPv4 addressing, nothing else. Two
parked features have each wanted a field on VLAN, and `MORE_MUSINGS.md` now asks for department
a second time from a different direction: as an organizational label operators already use
(Audio 200–207, Lighting 100–101, Video 220–221), not as allocation scoping. That is the signal
the old "watch item" was waiting for — so the flat-VLAN position gets reversed **once**,
deliberately, rather than three times by accident.

- [x] ADR: VLAN carries descriptive metadata. Settles **both** axes on paper — department as an
      *operator vocabulary*, role as a *code vocabulary* — and ships only the first (ADR 0021)
- [x] `Department` model: unique, non-blank name (mind #10's trimming/case-folding gap) plus an
      optional description. A real table rather than `TextChoices`, because no code branches on
      the value and adding a department must not require a migration and a redeploy — a dead end
      for the non-networking users `MORE_MUSINGS.md` opens by naming
- [x] `VLAN.department`: optional `PROTECT` FK. Optional because the system-seeded VLAN 1
      ("Default VLAN") has no department and every existing row needs a valid backfill; `PROTECT`
      to match ADR 0007's removal semantics and the codebase's 14-`PROTECT`-to-9-`CASCADE` habit
      for operator-managed entities
- [x] No system-seeded rows — unlike the "Default" Switch Port VLAN Profile, no department is
      meaningfully a default
- [x] Admin CRUD and an admin list filter on the VLAN changelist, plus a Department column
      (not a grouping) on the read-only VLAN list/detail pages and the address-map view, and a
      new `/models/department/` read-only page. **Not** a grouping of the index page's VLAN
      tiles — ADR 0021 decision 6 declined that as a layout change to a shaped view, distinct
      from adding a field
- [x] Tests: department is optional; `PROTECT` refuses to delete a department that has VLANs

**Role is designed here but not built.** It ships with phase 21, as `TextChoices` — addressing
modes branch on "which VLAN is Control here", exactly as the code branches on `PortMode` — and
its uniqueness invariant is **per-department, not site-level**, because Audio, Lighting and Video
each have their own Control VLAN. Getting that scope right is the whole reason department ships
first: role designed in isolation would have taken the site-level scope and needed a second ADR
to correct. This ADR is also where the reconciliation with ADR 0014 decision 1 is written down —
that decision declined a *dynamic membership flag for rack templates*, a different thing.

**Department does not scope allocation.** Shipping it dissolves only the first of the four
reasons phase 19 gives for declining department-scoped alignment. The other three survive
untouched, and the two that carry the weight are: all 21 production racks carry only audio VLANs,
so scoped and global alignment are identical on real data; and the spreadsheet's own model is one
offset per rack applied to *every* VLAN base. The ADR says so explicitly, so nobody later assumes
Department implied it.

## 17. Hostname ingredients

`MORE_MUSINGS.md` specifies a computed hostname as five dash-joined components: owner, location,
device type, an optional free-form purpose, and an optional sequence. Three of the five have no
representation in the data model at all. This phase adds them as ordinary optional fields and
computes nothing — each is independently meaningful, and no naming behaviour changes until
phase 18 turns it on.

**This is not issue #31 as filed.** #31 imagined `mps-{{ rack_name }}-{{ device_name }}-{{ slot_no }}`,
where `device_name` is a per-slot label only a populated rack template could supply — hence its
stated dependency on #30. The component scheme needs nothing from #30: component 3 is the device
*type*, which already exists, and component 4 is operator input. #30 becomes an *enhancer* of
hostnames (a populated template could prefill the purpose), never a blocker. **#31 has been
rewritten** accordingly — dependency dropped, template-engine framing withdrawn, `deferred` label
removed — and is the tracking issue for both this phase and phase 18.

The scheme covers switches as well as devices — component 3's own example, `sg300-10mp`, is a
Cisco switch — so every field decision here lands on both hierarchies.

- [ ] `Owner` model (short slug + full name) and optional `PROTECT` FKs on `Rack`,
      `NetworkSwitch` and `NetworkDevice`. A racked item's owner *defaults* from its rack at
      creation and stays overridable — ADR 0019's suggest-don't-lock pattern, not inheritance. A
      table rather than free text because #10 already documents what free-text identity fields do
- [ ] Owner lives on equipment, not only on Rack: component 1 is never skipped, and spare-pool
      equipment has no rack at all (`CONTEXT.md`, "Spare Pool")
- [ ] `Rack.location_slug`: optional, DNS-safe, unique **where non-blank**. Blank contributes no
      location component — which is how `AVIO`/`CONSOLES`/`SHURE` get `MORE_MUSINGS.md`'s
      virtual-rack behaviour **without** a purpose field or a pool concept. Neither
      `CONTEXT.md`'s "a Rack has no purpose field" nor ADR 0019 is amended; the question asked is
      "does this rack have a location name?", not "is this rack virtual?"
- [ ] Uniqueness lands on the new slug, not on `Rack.name`. `Rack.name` is **not** unique today
      (`inventory/models.py`), so `MORE_MUSINGS.md`'s premise that "rack name uniqueness is
      enforced" is false as written — and a brand-new field has no live rows to dedup or re-slug
- [ ] `hostname_slug` on `NetworkSwitchType` and `NetworkDeviceType`: operator-set, prefilled by
      slugifying the model as a convenience, DNS-validated, deliberately **not** unique. Not
      derived, because one rule gives two answers — `slugify("IK-42")` is `ik-42` where the name
      in use is `ik42`, while `slugify("SG300-10MP")` happens to be right
- [ ] Blank `hostname_slug` means that Type offers no computed hostnames, so existing Types need
      no backfill and creating a Type isn't blocked on choosing an abbreviation
- [ ] `hostname_suffix` on `NetworkDeviceTypePort`, beside `slot_offset`, materialized onto
      `NetworkDevicePort` per ADR 0010. Device-side only — `slot_offset` is a device-side concept
- [ ] Tests: the rack-derived owner default and its override; `location_slug` uniqueness ignores
      blanks; the suffix materializes with the port

A Type's identity is `(manufacturer, model, name)` where `name` is the *profile* label (ADR
0010), so two profiles of one model — "Martin Audio IK-42 — Default" and "— with Dante Card" —
both carry `ik42` and must be set to match by hand. That small duplication is accepted rather
than inventing a bare hardware-model entity ADR 0010 deliberately doesn't have. Same-model
collisions are routine, and phase 18 is what resolves them.

## 18. Hostname computation

Assembles phase 17's components at creation, enforces uniqueness, and resolves collisions.
Computed at materialization and stored — **not** immutable, and never re-derived automatically.
That is the same rule ADR 0003 gives static addresses, and what #31 already committed to.

- [ ] ADR: hostname computation. It **amends ADR 0018 decision 3**, whose companion hostname
      copies the host's verbatim on the default path — `inventory/models.py`: *"duplicate
      hostnames are already legal in this model"*. Under uniqueness that default becomes an
      integrity error on every Yamaha companion creation. `MORE_MUSINGS.md` supplies the
      replacement: the companion derives its name from the host plus a suffix
      (`-device-control`). The verbatim copy was a placeholder for the absence of a naming
      scheme; there is now a naming scheme
- [ ] `hostname_purpose` and `hostname_sequence` stored on `NetworkSwitch`/`NetworkDevice`
      alongside the existing editable `hostname`. Stored rather than transient like ADR 0013's
      `port_addressing`, because recomputation is impossible if the parts can't be recovered from
      the assembled string
- [ ] Cross-table uniqueness across `NetworkSwitch` + `NetworkDevice` together, validated in
      `full_clean()` with a plain-language error, blank exempt. Same shape as the existing
      cross-table static-address check, inheriting its known race (#5) rather than introducing a
      second, stricter mechanism for names; blank-exempt means the spare pool and every existing
      row need no backfill
- [ ] A `NetworkDevicePort` hostname as a **derived, read-only property** —
      `<device.hostname>-<hostname_suffix>` — exactly mirroring how ADR 0017 already treats an
      offset port's *address*. This is where `…-sd12-engine` lives. Ports have no hostname field
      and gain none
- [ ] Collisions bump `hostname_sequence` until the name is free, in physical and virtual racks
      alike, and never block a save. Two advisory messages ride along: recommend assigning `1` to
      a twin that has no sequence, and where the rack has a `location_slug`, note that a purpose
      reads better than a number
- [ ] *"In a physical rack, the 4th field should be used to avoid collisions"* is guidance, not
      machinery — the purpose is free-form operator input (`midhi-01-04`, `sub`) that the system
      cannot invent. Likewise *"recommend the existing device be assigned 1"* is a message about
      an already-saved row, not an action
- [ ] An explicit "recompute hostname" admin action. Moves never rename automatically
- [ ] Consider #54, filed as the sibling of #28: a rack move leaves a stale `location_slug` baked
      into a hostname, the same staleness class as an already-static address surviving a slot
      move. A staleness *indicator* is the cheap answer and would cover both
- [ ] Tests: assembly with and without each optional component; blank hostnames don't collide
      with each other; a switch-vs-device collision is caught; sequence auto-bump; a companion
      pair materializes two legal names

Because computation always yields a free name, the `full_clean()` uniqueness error only ever
fires on a **hand-typed** duplicate — which is exactly when it should.

## 19. Aligned rack allocation

Allocate a rack's offset **once**, as the lowest offset free on every VLAN it is getting a range
on, instead of running independent first-fit per `(rack, VLAN)`. Closes the cross-VLAN alignment
gap in `PROD-DATA-ANALYSIS.md` §6.1 by removing the mechanism that causes divergence rather than
policing the outcome. Easy to conflate with ADR 0019 and isn't: this is about where a rack's
*block* sits, whereas ADR 0019 is about the *ordinal inside* the block — the ordinal is already
aligned across VLANs by construction; the block base is not.

Tested against the production racks: reproduces all 19 automatically-allocated offsets, and — the
point — **gives the same answer when the VLANs have different DHCP geometry**, which is precisely
the case where today's per-VLAN first-fit diverges. Strictly more robust than what we have, not
merely tidier. No schema change; this is a suggester change plus a report.

- [ ] **Suggest, don't enforce.** A hard constraint would be the first place this system forbids
      something an operator may legitimately need, cutting against ADR 0001's suggest-with-override
      and ADR 0003's stored-not-derived stance. Real cases exist: a VLAN whose subnet is too small
      for the aligned offset, a rack joining a VLAN whose aligned offset is already taken, or
      importing a site that is already misaligned. Aligned-by-default achieves the outcome; a
      constraint mainly produces a wall at the worst moment
- [ ] **The invariant is the offset from the VLAN's network address, not the third octet.** 16 of
      the 21 production racks don't start on a `/24` boundary. An offset-based rule survives a VLAN
      that isn't a `/21`; a third-octet rule doesn't (§6.1)
- [ ] **Static addresses only.** DHCP interfaces are outside the guarantee — the only promises
      there are the VLAN subnet and the server's pool. A DHCP port stores no address at all, so
      this needs no special handling, but any misalignment report must ignore DHCP ports or it
      will flag every mixed device (§6.1)
- [ ] **Department scoping stays declined**, even though phase 16 now supplies the grouping field
      that was the first of four reasons for declining it. The other three stand: ADR 0014
      decision 1 declined the nearest thing deliberately; all 21 production racks carry only audio
      VLANs, so department-scoped and global alignment are identical on today's data; and the
      spreadsheet's own model is one offset per rack applied to *every* VLAN base, so scoping
      would depart from current practice rather than formalise it
- [ ] A divergence report (or an admin column) so misalignment is *visible* — far cheaper than a
      constraint, and it strands nobody

## 20. Addressing per `(device, VLAN)` instead of per port — #27

The actual fix for Switched Mode's bridged-jack limitation (ADR 0010, ADR 0013): two physical
jacks bridged onto one VLAN share one address, a shape the per-port model can't express and that
ADR 0013's static materialization refuses outright. Scoped, not yet designed — it needs its own
ADR and will partially supersede ADR 0003 and ADR 0013.

It is scheduled here because it is a **prerequisite for phase 21**, where two of the four
addressing modes are otherwise unofferable, and because it is the nearest neighbour of #42.

## 21. Device-type addressing modes

Replace hand-assembled port lists with a short enumeration of real hardware shapes — expressible
as `Control: yes/no` × `Dante: none / redundant / switched / single` — plus an "advanced" mode
where users add arbitrary interfaces. Motivated directly by `PROD-DATA-ANALYSIS.md` §5.4:
**34 of production's 229 distinct addresses (15%) are assigned to interfaces that don't
physically exist**, in three separate classes, and every one of them would have been
unrepresentable under a mode enumeration.

This also makes device types genuinely portable. `NetworkDeviceTypePort.vlan` is a `PROTECT` FK
to a *specific* VLAN today, so "Martin Audio IK-42 — with Dante Card" is welded to this site's
VLAN numbering; a role indirection lets a Type describe hardware rather than a site, which is
what ADR 0010 says Types are for.

- [ ] Ships phase 16's **VLAN role** as `TextChoices`, unique **per department**. Phase 16's ADR
      already pinned the shape and the scope, and reconciled it with ADR 0014 decision 1
- [ ] **Depends on phase 20.** "Control + switched Dante" and "switched Dante" both put two ports
      on one VLAN sharing one address — the #27 shape ADR 0013 refuses. Offering them in a picker
      advertises them as supported, so either #27 lands first or they are visibly unavailable for
      static devices
- [ ] **Seed-once or live-resolve?** Generating type ports from the mode at type-creation time
      matches ADR 0010's existing pattern and makes "advanced" simply mean editing the generated
      list. Storing the mode and resolving live requires the role mapping to stay stable forever.
      Seed-once is the cheaper fork
- [ ] **"Advanced" is load-bearing, not a rare escape hatch** — `DESIGN.md`'s Shure Split Mode
      needs a fourth role ("Shure Control"), and the DiGiCo SD12 needs ADR 0017's derived offsets.
      Modes are a convenience layer over a port model that must stay fully expressive

## Later / not yet designed

### Blocked on one gate: ADR 0020 decision 2

ADR 0020 decision 2 is *"no forms, no `POST` handlers, no validation, no audit-trail plumbing of
its own"*. Three `MORE_MUSINGS.md` items sit behind it, and none can move until a deliberate
ADR 0020 v2 phase is scheduled. The 2026-08-06 design pass parked all three in favour of model
work.

- **Live JavaScript UI.** Worth recording that this is really *two* features, not one. JS as
  **progressive enhancement of the read-only views** — filtering, expanding a rack elevation,
  hovering a slot — needs no `POST` and doesn't touch ADR 0020 at all. JS as a **write surface**
  does, and carries the full cost ADR 0020 says read-only is what avoids: forms, validation,
  transactional correctness, ADR 0007's removal confirmations, and keeping the audit actor
  correct on every write path. Whichever lands first, they are separable decisions
- **Rack creation wizard** — the most expensive single item in `MORE_MUSINGS.md`: a multi-step
  write flow *and* one that materializes equipment, so it needs #30 (populated rack templates,
  still undesigned) underneath it as well as a write surface above it
- **Password change form.** A non-staff Viewer cannot change their own password (no admin
  password form, and password reset is deliberately unrouted — see README.md "Setting up
  accounts"); password changes stay admin-performed until a read/write UI provides a `POST`
  surface. Mounting `PasswordChangeView` now would contradict ADR 0020 decision 2 directly.
  `MORE_MUSINGS.md` widens this to account-setup and reset emails, which additionally needs
  Django SMTP configuration documented in the setup/deployment instructions
- **User documentation** — deliberately deferred until the write UI exists, so screenshots are
  shot once against a UI that has stopped changing shape. The accepted cost: phase 15 moved every
  Viewer to `is_staff=False` and into a purpose-built UI they have never used, and they stay on an
  undocumented interface until then

### Blocked on external input

- **Other CSV import** — other departments are working on rack layouts. Blocked on obtaining
  sample exports from the lighting/video departments; there is no shape to design against yet
- **Switch config generator** — export a text file importable into a switch's administrative
  interface. Not a phase: it needs port-level and switch-level configuration storage the model
  doesn't have, and overlaps multicast (#22). The next step is a data-model investigation, not an
  implementation plan

### Design deferred

- Device-replacement workflow (swapping a spare into an already-addressed slot) — flagged in ADR 0003, design deferred
- Two *independent* static addresses on one VLAN (a Yamaha console's "For Device Control" interface) — see #42. ADR 0018 covers the same hardware but solves a different problem (existence and lifecycle, not addressing) and leaves this open: if it ever lands, those consoles collapse to one device and their companion links fall away. Nearest neighbour of phase 20
- Slot moves don't re-suggest an already-static device port's address (armed by default now that static materializes by default, ADR 0013; follows from ADR 0003's "stored, not immutable") — see #28, and its hostname sibling #54 (a rack move leaves a stale location baked into a computed hostname). Both are the same problem on two fields, and a single staleness *indicator* would cover both — report, don't enforce, as in phase 19
- Multicast configuration: port-level filtering plus switch-level IGMP snooping — see #22
- Populated rack templates: slot layouts that materialize equipment (needs Type `PROTECT`, unlike the VLAN-only feature) — see #30. **No longer blocks hostnames** (phase 17); it would *enhance* them by prefilling the purpose component, and it does gate the rack creation wizard
- Rack *slot* occupancy has no DB-level overlap guarantee once a device spans several ordinals (ADR 0017's known gap) — see #40. `RackSlotAssignmentMixin`'s docstring defers this to phase 3's "Overlap validation", but that item shipped covering rack-range-vs-range and DHCP overlap only — the deferral still has no live home and the pointer is still stale
- **Address regions** — named, declared partitions of the host-offset space that racks are allocated *from*, so that "amp racks live here, wireless lives there" is a property the system knows rather than something achieved by picking offsets by hand. Motivated by `PROD-DATA-ANALYSIS.md` §7.2: production's offset gaps are **mnemonic, not technical** — they exist so an address can be identified by eye in the field, the same instinct as VLAN-ID-in-the-second-octet, and they double as error detection. Composes with phase 19 but is not scheduled with it
  - **Downgraded by ADR 0019, not closed.** Reserving offset space no longer needs this feature: an empty Rack with hand-placed ranges holds a block against the first-fit suggester today, using machinery that already exists. What regions would still buy is (a) *automatic* enforcement — the suggester restricting its search to a window, so nobody has to remember to create the reservation racks first — and (b) one named region instead of a CIDR decomposition, since a region boundary need not be CIDR-aligned and a rack's range must be (offsets 864–1279 take three reservation racks: a `/27`, a `/25` and a `/24`). Both are real, and both are much smaller than "there is no way to reserve offset space", which is what this item used to claim
  - **Choosing offsets manually is itself error-prone**, and accidentally overlapping rack ranges is exactly what this tool exists to prevent. Reservation racks reduce this but don't remove it — they still have to be placed by hand
  - **Declare regions once at site level, not per VLAN.** The obvious framing is "several declared address ranges per VLAN", but per-VLAN regions would have to be kept aligned across VLANs by hand — the original problem, one level up. A single site-level partition of the *offset* space, applied to every VLAN, composes correctly with aligned allocation and needs no per-VLAN bookkeeping
  - **Likely additive, not a rewrite.** `RackVlanRange` is unchanged; a region is a new table (name + offset window, validated non-overlapping), a rack is created in a region, and the suggester restricts its search to that window. That is a constraint on which offsets are *considered*, not a change to how ranges are stored or validated — so the "this breaks the current model" concern is probably narrower than it looks. Worth confirming against a real design pass before relying on that
  - Open: what happens when a VLAN's subnet is too small to contain every region, and whether a rack may ever sit outside all regions
- `.255` avoidance is structural only for blocks of `/24` or smaller; a rack of 255+ slots gets a block with assignable interior `.255` addresses, and `slot_count` has no upper bound — see ADR 0015

### Resolved

- **Watch item — VLAN metadata** (*closed by ADR 0021, implemented in phase 16*). The item asked
  for a third axis before reversing `CONTEXT.md`'s flat-VLAN position. Department arriving a
  second time from an independent direction — an operator-facing organizational label rather than
  allocation scoping — was taken as that signal, and ADR 0021 makes the reversal as one deliberate
  decision covering both known axes (department, and role for phase 21) rather than three
  incremental ones
- **Hostname templating** (*promoted to phases 17 and 18*). #31 was filed as blocked on #30; the
  `MORE_MUSINGS.md` component scheme dissolves that dependency, and the issue has been rewritten
  to describe the component scheme instead of the template language it used to propose
