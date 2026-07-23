# Network Addresser 9000

A backend service and web frontend for tracking IP addresses assigned to network equipment: VLANs, IPv4 subnets, switches, network devices, and the racks they're grouped into.

## Status

Django scaffolding is in place (models, admin, migrations) and phase 3 (core domain logic) is next; see [`ROADMAP.md`](./ROADMAP.md) for what that covers. The domain model and key architectural decisions are settled; see the documentation below.

## Documentation

- [`DESIGN.md`](./DESIGN.md) — requirements and design narrative
- [`CONTEXT.md`](./CONTEXT.md) — domain glossary (canonical terminology)
- [`docs/adr/`](./docs/adr/) — architecture decision records
- [`ROADMAP.md`](./ROADMAP.md) — current phase and what's next

## Planned stack

- **Backend**: Python / Django
- **Database**: MariaDB
- **Frontend**: Django admin initially, purpose-built UI later
- **Deployment**: Docker / docker-compose, self-hosted on-prem
