# Docker deployment shape: single app container + WhiteNoise, MariaDB as a Compose service

DESIGN.md calls for a containerized deployment, ideally app + MariaDB via docker-compose, self-hosted and VPN-only with no public exposure — so TLS termination and a reverse proxy are out of scope for now. Given that, the app container serves its own static files via WhiteNoise (`CompressedManifestStaticFilesStorage`) rather than adding an nginx service: nginx would earn its place once a reverse proxy is needed for external exposure anyway (at which point it can take over static serving too), but until then it's a second container and a shared-volume problem in exchange for nothing this deployment target needs. Static assets are collected at Docker *build* time, not container start — that keeps the image immutable and means `/app` never needs to be writable at runtime, at the cost of build-only placeholder values for `SECRET_KEY`/`DB_PASSWORD` (settings.py requires them to be non-empty outside `DJANGO_DEBUG=true` purely to import cleanly; `collectstatic` never touches the database).

gunicorn is the WSGI server, given an explicit bind address and worker count rather than defaults, since silent defaults on a server process are the kind of thing that's invisible until it's a production incident. The entrypoint runs `migrate` and `sync_roles` (see the `sync_roles` management command's own docstring for why it must run after, not during, `migrate`) before handing off to gunicorn via `exec`, so gunicorn becomes PID 1 and receives shutdown signals directly. Doing this at every container start — rather than as a separate manual or CI step — is only safe because this is a single-instance deployment; a multi-replica setup would need to move schema migration out of the shared startup path to avoid a migration race, but that's not this system's shape.

MariaDB runs as its own Compose service, pinned to a specific release rather than a floating tag, with its data on a named volume — which is persistence, not a backup; there is no automated backup story yet, only a documented manual `mariadb-dump` procedure. Publishing the app's port does not itself constitute the "VPN-only" boundary DESIGN.md assumes: that's a host firewall/VPN policy question, orthogonal to Compose, so the published port's host-bind address is left configurable rather than hardcoded to all interfaces.

## Postscript (2026-08-21, phase 7 — container publishing)

The image is now built on every pull request and `main` push (`.github/workflows/ci.yml`'s `docker` job, validation only) and built, multi-arch, and pushed to GHCR on `v*` release tags (`.github/workflows/publish.yml`). This doesn't change the shape decided above — the app container, WhiteNoise, gunicorn, and MariaDB-as-a-Compose-service all stand as written. What's new is *where the image comes from* at deploy time: a deploy may either build from source, exactly as before, or pull a pinned, published image via a new override file, `docker-compose.release.yml`:

```
docker compose -f docker-compose.yml -f docker-compose.release.yml up -d
```

`docker-compose.yml` itself is unchanged — `build: context: .` is still its documented default — so nothing about existing deployments breaks or requires action. No new ADR was written for this, because no new architectural decision was taken: publishing an image that this ADR's own shape already produces isn't a shape change, just a distribution mechanism for it.

Two things worth recording because they're easy to get wrong silently:

- The published package is public, so pulling it needs no registry credentials — but visibility has to be flipped from GHCR's private default by hand after the first push; it doesn't happen automatically.
- `.dockerignore` was tightened alongside this (excluding `prod/`, `.env.deployment`, `docker-compose.env`) — a **local** `docker compose up --build` previously had no guard against baking real production network data or deployment secrets into an image. A CI-built image was never at risk (Actions only checks out tracked files), but a local build was, and a public registry makes that mistake expensive rather than merely embarrassing.
