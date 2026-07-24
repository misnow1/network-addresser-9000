# Network Addresser 9000

A backend service and web frontend for tracking IP addresses assigned to network equipment: VLANs, IPv4 subnets, switches, network devices, and the racks they're grouped into.

## Status

Django scaffolding, core domain logic (address suggestion, overlap validation, removal semantics), access/accountability (RBAC, mutation audit trail, removal confirmation), and deployment (Docker / docker-compose) are in place; phase 6 (process hardening) is next. See [`ROADMAP.md`](./ROADMAP.md) for what that covers. The domain model and key architectural decisions are settled; see the documentation below.

## Documentation

- [`DESIGN.md`](./DESIGN.md) — requirements and design narrative
- [`CONTEXT.md`](./CONTEXT.md) — domain glossary (canonical terminology)
- [`docs/adr/`](./docs/adr/) — architecture decision records
- [`ROADMAP.md`](./ROADMAP.md) — current phase and what's next

## Running with Docker

```
cp .env.compose.example .env   # then fill in SECRET_KEY, DJANGO_ALLOWED_HOSTS, DB_PASSWORD, MARIADB_ROOT_PASSWORD
docker compose up
```

This brings up MariaDB and the app together; the app container waits for the database, applies migrations, and runs `sync_roles` automatically on every start (see "Setting up accounts" below) before serving on `http://127.0.0.1:8000/` (loopback-only by default — see `HOST_BIND` in `.env.compose.example`). Create your first user with `docker compose exec app python manage.py createsuperuser`.

A few things worth knowing before relying on this in practice:

- **`DJANGO_ALLOWED_HOSTS` is required.** Outside `DJANGO_DEBUG=true`, Django rejects every request if this is empty — list every hostname/IP the app will be reached by.
- **This is not a VPN boundary by itself.** Per `DESIGN.md`, the deployment target is self-hosted/VPN-only with no public exposure and no TLS — but publishing the app's port only controls which *host* interface it binds to (see `HOST_BIND`), not who can reach that host. The actual VPN/firewall policy is your responsibility, outside Compose. See [ADR 0009](./docs/adr/0009-docker-deployment-shape.md).
- **The named volume is persistence, not a backup.** `docker compose down`/`up` preserves data, but there's no automated backup — take one with `docker compose exec db mariadb-dump -u root -p"$MARIADB_ROOT_PASSWORD" --all-databases > backup.sql` (restore via `docker compose exec -T db mariadb -u root -p"$MARIADB_ROOT_PASSWORD" < backup.sql`).
- **Changing `.env` credentials doesn't change an already-initialized database.** `MARIADB_*` variables only take effect the first time the `db_data` volume is created; rotating `DB_PASSWORD`/`MARIADB_ROOT_PASSWORD` afterward requires changing the password inside MariaDB itself (or removing the volume, which destroys data).
- **Rebuild periodically to pick up base-image security fixes** — the Dockerfile pins specific Python and MariaDB versions for reproducibility, which also means they don't update automatically.

## Setting up accounts

Create a user via `/admin/auth/user/add/` with **Staff status** checked (required for any Django-admin access, including the read-only Viewer role), then assign them to one of the three groups — Viewer, Editor, or Admin (see CONTEXT.md's "Roles") — from the user's own admin page. Under Docker, those groups are created automatically on every container start (see "Running with Docker"). Outside Docker, run `python manage.py sync_roles` once after `migrate` (safe to re-run any time, e.g. after adding a model).

## Planned stack

- **Backend**: Python / Django
- **Database**: MariaDB
- **Frontend**: Django admin initially, purpose-built UI later
- **Deployment**: Docker / docker-compose, self-hosted on-prem
