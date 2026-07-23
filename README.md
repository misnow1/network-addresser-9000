# Network Addresser 9000

A backend service and web frontend for tracking IP addresses assigned to network equipment: VLANs, IPv4 subnets, switches, network devices, and the racks they're grouped into.

## Status

Design phase — no application code yet. The domain model and key architectural decisions are settled; see the documentation below.

## Documentation

- [`DESIGN.md`](./DESIGN.md) — requirements and design narrative
- [`CONTEXT.md`](./CONTEXT.md) — domain glossary (canonical terminology)
- [`docs/adr/`](./docs/adr/) — architecture decision records

## Planned stack

- **Backend**: Python / Django
- **Database**: MariaDB
- **Frontend**: Django admin initially, purpose-built UI later
- **Deployment**: Docker / docker-compose, self-hosted on-prem
