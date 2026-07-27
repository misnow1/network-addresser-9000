# Roadmap

High-level phases only — day-to-day task tracking belongs in GitHub Issues once there's code to file issues against. This file exists so it's obvious what phase the project is in and what's next, even after a fresh start.

**Current phase: 9 — in progress.**

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

## 9. Switch port VLAN profiles

- [x] `SwitchPortVlanProfile`: reusable, *live-referenced* Port Mode/Native VLAN/Allowed VLANs/Allow All VLANs bundle — see ADR 0012
- [x] `NetworkSwitchTypePort`/`NetworkSwitchPort` point at a profile instead of carrying their own VLAN config
- [x] Port Mode/Native VLAN lock once a profile has any real switch port; Allowed VLANs/Allow All VLANs stay editable
- [x] System-seeded "Default" profile and subnet-less "Default VLAN" (VLAN 1); `VLAN.subnet` is now optional (L2-only VLANs)
- [x] `seed_defaults` management command re-seeds the system rows if removed outside a migration

## Later / not yet designed

- Purpose-built frontend beyond Django admin (rack visualizations, address-utilization views)
- Device-replacement workflow (swapping a spare into an already-addressed slot) — flagged in ADR 0003, design deferred
- Bridged multi-port logical interfaces (e.g. Shure ULXD4Q/D "Switched" mode) — flagged in ADR 0010, design deferred
- Multicast configuration: port-level filtering plus switch-level IGMP snooping — see #22
- Rack templates: a named set of VLANs applied once at rack creation (seed-once, per ADR 0010) — see #23
- Device creation offering static addressing by default instead of always materializing ports as DHCP (revises ADR 0010) — see #24
