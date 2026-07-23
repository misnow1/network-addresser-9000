# Roadmap

High-level phases only — day-to-day task tracking belongs in GitHub Issues once there's code to file issues against. This file exists so it's obvious what phase the project is in and what's next, even after a fresh start.

**Current phase: 2 — not started.**

## 1. Foundation — done

- [x] Design narrative (`DESIGN.md`)
- [x] Domain glossary (`CONTEXT.md`)
- [x] Architecture decisions (`docs/adr/`)
- [x] README

## 2. Django scaffolding — next up

- [ ] Project setup
- [ ] Models matching `CONTEXT.md` (VLAN, Rack, Network Switch/Device + Types, Ports)
- [ ] Initial migration
- [ ] Admin registration for all models

## 3. Core domain logic

- [ ] Address-range suggestion (rack ranges, VLAN gateway/DHCP range) — see ADR 0001, ADR 0002
- [ ] Overlap validation (rack ranges vs. each other and the DHCP block)
- [ ] Device address default-and-override behavior — see ADR 0003
- [ ] Removal semantics: block non-empty containers, unassign on leaf removal — see ADR 0007

## 4. Access and accountability

- [ ] Local auth, three roles (Viewer / Editor / Admin)
- [ ] Mutation audit trail — see ADR 0004
- [ ] "Big scary prompt" confirmation flows for removal

## 5. Deployment

- [ ] Dockerfile
- [ ] docker-compose (app + MariaDB)

## 6. Process hardening

- [ ] Pre-commit hooks (formatting/linting)
- [ ] GitHub Actions CI (tests, lint)
- [ ] Branch protection on `main` — require PRs, block direct pushes

## Later / not yet designed

- Purpose-built frontend beyond Django admin (rack visualizations, address-utilization views)
- Device-replacement workflow (swapping a spare into an already-addressed slot) — flagged in ADR 0003, design deferred
