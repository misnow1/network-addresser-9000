# Network Addresser 9000

A backend service and web frontend for tracking IP addresses assigned to network equipment: VLANs, IPv4 subnets, switches, network devices, and the racks they're grouped into.

## Status

Django scaffolding, core domain logic (address suggestion, overlap validation, removal semantics), and access/accountability (RBAC, mutation audit trail, removal confirmation) are in place; phase 5 (deployment) is next. See [`ROADMAP.md`](./ROADMAP.md) for what that covers. The domain model and key architectural decisions are settled; see the documentation below.

## Documentation

- [`DESIGN.md`](./DESIGN.md) — requirements and design narrative
- [`CONTEXT.md`](./CONTEXT.md) — domain glossary (canonical terminology)
- [`docs/adr/`](./docs/adr/) — architecture decision records
- [`ROADMAP.md`](./ROADMAP.md) — current phase and what's next

## Setting up accounts

Create a user via `/admin/auth/user/add/` with **Staff status** checked (required for any Django-admin access, including the read-only Viewer role), then assign them to one of the three groups — Viewer, Editor, or Admin (see CONTEXT.md's "Roles") — from the user's own admin page. Those groups must exist first: run `python manage.py sync_roles` once after `migrate` (safe to re-run any time, e.g. after adding a model). Wiring `sync_roles` into the deploy entrypoint is deferred to phase 5 (Docker).

## Planned stack

- **Backend**: Python / Django
- **Database**: MariaDB
- **Frontend**: Django admin initially, purpose-built UI later
- **Deployment**: Docker / docker-compose, self-hosted on-prem
