# Roadmap

High-level phases only — day-to-day task tracking belongs in GitHub Issues once there's code to file issues against. This file exists so it's obvious what phase the project is in and what's next, even after a fresh start.

**Current phase: 11 — designed, not yet built.**

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

## 11. Rack Templates — designed, not yet built

- [x] Design decided: named, reusable VLAN sets seeding `RackVlanRange` rows at rack creation, seed-once (not live-referenced) — see ADR 0014, closes the VLAN-only scope of #23
- [ ] `RackTemplate` model + `PROTECT`-FK through model for VLAN membership + migration
- [ ] Admin: template CRUD, and a creation-only template picker on the Rack add view
- [ ] Domain-level apply operation (construct-blank → `full_clean()` → `save()` per VLAN, all-or-nothing, reachable from programmatic creation — not admin-only)
- [ ] Tests: successful suggestion via the template path; rollback when one of several VLANs can't be allocated

## Later / not yet designed

- Purpose-built frontend beyond Django admin (rack visualizations, address-utilization views)
- Device-replacement workflow (swapping a spare into an already-addressed slot) — flagged in ADR 0003, design deferred
- Addressing modeled per `(device, VLAN)` instead of per port — the actual fix for Switched Mode's bridged-jack limitation (ADR 0010, ADR 0013) — see #27
- Slot moves don't re-suggest an already-static device port's address (armed by default now that static materializes by default, ADR 0013; follows from ADR 0003's "stored, not immutable") — see #28
- Multicast configuration: port-level filtering plus switch-level IGMP snooping — see #22
- Populated rack templates: slot layouts that materialize equipment (needs Type `PROTECT`, unlike the VLAN-only feature) — see #30
- Hostname templating for materialized equipment — see #31
