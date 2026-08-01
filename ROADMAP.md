# Roadmap

High-level phases only — day-to-day task tracking belongs in GitHub Issues once there's code to file issues against. This file exists so it's obvious what phase the project is in and what's next, even after a fresh start.

**Current phase: 12 — designed, not yet built.**

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

## 6. Process hardening

- [x] Pre-commit hooks (formatting/linting)
- [ ] GitHub Actions CI (tests, lint)
- [ ] Branch protection on `main` — require PRs, block direct pushes

## 7. Container publishing

- [ ] GitHub Actions workflow: build and publish the Docker image to GHCR on `main` merges

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

## 12. Production data validation — designed, not yet built

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
- [x] Implement ADR 0017 — `slot_offset`, derived offset-port addressing, span-aware rack-slot occupancy, and the narrowed same-VLAN pre-flight; see `PLAN-adr-0017.md`
- [ ] Production import — see `PLAN-prod-import.md` (revision 2). 9 of 10 blockers cleared; unblocked and ready to build. Two known gaps, neither blocking: the Netgear model (defers 3 of 23 switches to a second pass) and the per-device-type wiring rule for patch-panel-fed devices. Four items filed as `deferred`: #41, #42, #43, #44

## Later / not yet designed

- Purpose-built frontend beyond Django admin (rack visualizations, address-utilization views)
- Device-replacement workflow (swapping a spare into an already-addressed slot) — flagged in ADR 0003, design deferred
- Addressing modeled per `(device, VLAN)` instead of per port — the actual fix for Switched Mode's bridged-jack limitation (ADR 0010, ADR 0013) — see #27
- Slot moves don't re-suggest an already-static device port's address (armed by default now that static materializes by default, ADR 0013; follows from ADR 0003's "stored, not immutable") — see #28
- Multicast configuration: port-level filtering plus switch-level IGMP snooping — see #22
- Populated rack templates: slot layouts that materialize equipment (needs Type `PROTECT`, unlike the VLAN-only feature) — see #30
- Hostname templating for materialized equipment — see #31
- Device-type **addressing modes**: replace hand-assembled port lists with a short enumeration of real hardware shapes — expressible as `Control: yes/no` × `Dante: none / redundant / switched / single` — plus an "advanced" mode where users add arbitrary interfaces. Motivated directly by `PROD-DATA-ANALYSIS.md` §5.4: **34 of production's 229 distinct addresses (15%) are assigned to interfaces that don't physically exist**, in three separate classes, and every one of them would have been unrepresentable under a mode enumeration. Four things to resolve before this becomes an ADR:
  - It needs an explicit **VLAN role/purpose concept** ("which VLAN is Control here"), which `CONTEXT.md` deliberately doesn't have today. Not a contradiction of ADR 0014 decision 1 — that declined a *dynamic membership flag* for rack templates, a different thing — but it must be reconciled explicitly, and roles need a site-level uniqueness invariant.
  - **Two of the modes can't be created.** "Control + switched Dante" and "switched Dante" both put two ports on one VLAN sharing one address — the #27 shape ADR 0013 refuses. Offering them in a picker advertises them as supported, so either #27 lands first or they're visibly unavailable for static devices.
  - **Seed-once or live-resolve?** Generating type ports from the mode at type-creation time matches ADR 0010's existing pattern and makes "advanced" simply mean editing the generated list. Storing the mode and resolving live requires the role mapping to stay stable forever. Seed-once is the cheaper fork.
  - **"Advanced" is load-bearing, not a rare escape hatch** — `DESIGN.md`'s Shure Split Mode needs a fourth role ("Shure Control"), and the DiGiCo SD12 needs ADR 0017's derived offsets. Modes are a convenience layer over a port model that must stay fully expressive.

  Also worth noting: this would make device types genuinely portable. `NetworkDeviceTypePort.vlan` is a `PROTECT` FK to a *specific* VLAN today, so "Martin Audio IK-42 — with Dante Card" is welded to this site's VLAN numbering; a role indirection would let a Type describe hardware rather than a site, which is what ADR 0010 says Types are for.
- Rack *slot* occupancy has no DB-level overlap guarantee once a device spans several ordinals (ADR 0017's known gap). `RackSlotAssignmentMixin`'s docstring defers this to phase 3's "Overlap validation", but that item shipped covering rack-range-vs-range and DHCP overlap only — the deferral has no live home and the pointer is stale
- **Aligned rack allocation** — allocate a rack's offset *once*, as the lowest offset free on every VLAN it is getting a range on, instead of running independent first-fit per `(rack, VLAN)`. Closes the cross-VLAN alignment gap in `PROD-DATA-ANALYSIS.md` §6.1 by removing the mechanism that causes divergence rather than policing the outcome. Tested against the production racks: reproduces all 19 automatically-allocated offsets, and — the point — **gives the same answer when the VLANs have different DHCP geometry**, which is precisely the case where today's per-VLAN first-fit diverges. Strictly more robust than what we have, not merely tidier.
  - **Suggest, don't enforce.** A hard constraint would be the first place this system forbids something an operator may legitimately need, cutting against ADR 0001's suggest-with-override and ADR 0003's stored-not-derived stance. Real cases exist: a VLAN whose subnet is too small for the aligned offset, a rack joining a VLAN whose aligned offset is already taken, or importing a site that is already misaligned. Aligned-by-default achieves the outcome; a constraint mainly produces a wall at the worst moment. If divergence should be *visible*, a report or admin column is far cheaper and strands nobody.
  - **The invariant is the offset from the VLAN's network address, not the third octet.** 16 of the 21 production racks don't start on a `/24` boundary. An offset-based rule survives a VLAN that isn't a `/21`; a third-octet rule doesn't (§6.1).
  - **Static addresses only.** DHCP interfaces are outside the guarantee — the only promises there are the VLAN subnet and the server's pool. A DHCP port stores no address at all, so this needs no special handling, but any misalignment report must ignore DHCP ports or it will flag every mixed device (§6.1).
  - **Department/group scoping declined for now.** It would need a VLAN grouping field, and: ADR 0014 decision 1 already declined the nearest thing deliberately; all 21 production racks carry only audio VLANs, so department-scoped and global alignment are identical on today's data; and the spreadsheet's own model is one offset per rack applied to *every* VLAN base, so scoping would depart from current practice rather than formalise it.
- **Address regions** — named, declared partitions of the host-offset space that racks are allocated *from*, so that "amp racks live here, wireless lives there" is a property the system knows rather than something achieved by picking offsets by hand. Motivated by `PROD-DATA-ANALYSIS.md` §7.2: production's offset gaps are **mnemonic, not technical** — they exist so an address can be identified by eye in the field, the same instinct as VLAN-ID-in-the-second-octet, and they double as error detection. Two facts make this worth building rather than leaving as a convention:
  - **Automation destroys the convention.** First-fit takes the lowest free offset, so the next rack created lands at offset 864 — inside the region reserved by eye for wireless. Aligned allocation doesn't change this. There is no "reserved but unallocated" concept, and today the only thing preventing it is that offsets are chosen manually.
  - **Choosing offsets manually is itself error-prone**, and accidentally overlapping rack ranges is exactly what this tool exists to prevent. The convention and the automation are in direct tension; regions are what would let both hold.
  - **Declare regions once at site level, not per VLAN.** The obvious framing is "several declared address ranges per VLAN", but per-VLAN regions would have to be kept aligned across VLANs by hand — the original problem, one level up. A single site-level partition of the *offset* space, applied to every VLAN, composes correctly with aligned allocation and needs no per-VLAN bookkeeping.
  - **Likely additive, not a rewrite.** `RackVlanRange` is unchanged; a region is a new table (name + offset window, validated non-overlapping), a rack is created in a region, and the suggester restricts its search to that window. That is a constraint on which offsets are *considered*, not a change to how ranges are stored or validated — so the "this breaks the current model" concern is probably narrower than it looks. Worth confirming against a real design pass before relying on that.
  - Open: what happens when a VLAN's subnet is too small to contain every region, and whether a rack may ever sit outside all regions.
- **Watch item — VLAN metadata.** Two parked features now want a field on VLAN: addressing modes want a **role** (Control / Dante Primary / Dante Secondary), aligned allocation's declined department-scoping wants a **department** (Audio / Lighting / Video). These are orthogonal axes, and neither justifies the change alone. If a third arrives, that is the signal to revisit `CONTEXT.md`'s flat-VLAN position as one deliberate decision rather than three incremental ones.
- `.255` avoidance is structural only for blocks of `/24` or smaller; a rack of 255+ slots gets a block with assignable interior `.255` addresses, and `slot_count` has no upper bound — see ADR 0015
