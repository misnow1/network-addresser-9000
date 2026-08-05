# A read-only purpose-built UI at `/`; the admin retains every mutation

`DESIGN.md` deferred a purpose-built frontend "until real usage shows which views (rack
layout, address-utilization dashboards, etc.) are worth building by hand." The production
import supplied that evidence: 183 addresses across 21 racks, and a set of questions the
admin changelists answer badly or not at all — what does this rack look like, where does the
next rack land, which offsets are free, which equipment is unracked.

`inventory/views.py` is one line and `config/urls.py` routes only `admin/`, so this is
greenfield. This ADR fixes the scope of the first version.

## Decision

1. **The UI is mounted at `/`; the admin stays at `/admin/`.** Nothing about the admin's URL
   or configuration changes.

2. **Version 1 is strictly read-only.** `GET` views and templates. No forms, no `POST`
   handlers, no validation, no audit-trail plumbing of its own.

3. **Every mutation is a deep link into the admin**, via `reverse("admin:inventory_<model>_change", args=[pk])`
   and its `_add` counterpart, prefilled through query parameters where the context supplies
   them — `admin:inventory_networkdevice_add?rack=3&rack_slot=6` from a rack's empty slot, for
   instance, which picks up ADR 0019's ordinal suggestion for free.

4. **Roles need no new machinery.** `sync_roles.py` builds the Viewer/Editor/Admin groups out
   of standard Django `view_`/`add_`/`change_`/`delete_` permissions, which are not
   admin-specific. The UI checks the same codenames through `user.has_perm("inventory.…")`.
   `sync_roles.py` is unchanged.

5. **Version 1 reaches read-parity with what a Viewer can see today** — all eight registered
   models (`VLAN`, `SwitchPortVlanProfile`, `RackTemplate`, `Rack`, `NetworkSwitchType`,
   `NetworkSwitch`, `NetworkDeviceType`, `NetworkDevice`), their inlines (rack VLAN ranges,
   type ports, instance ports, switch addresses, allowed VLANs), and the audit trail.

6. **The audit trail gets a read-only view** over `auditlog`'s `LogEntry`, both as a site-wide
   history and per-object on the rack, switch and device pages.

7. **Viewers provision as `is_staff=False`** and are locked out of the admin entirely. Editors
   and Admins keep `is_staff=True`, because deep links are how they mutate.

8. **The admin link renders only when `request.user.is_staff`**, so a Viewer is never offered
   a door that will refuse them.

## Read-only is what makes this affordable

The expensive part of a second frontend is never the reading. It is forms, validation,
transactional correctness, the removal-confirmation flows (ADR 0007), and keeping the audit
actor correct on every write path. All of that already exists once, in the admin, tested and
reviewed.

A read-only UI duplicates none of it. It also cannot introduce the failure this project cares
most about — a wrong or overlapping address written without validation — because it writes
nothing at all. The admin remains the single place where the domain's invariants are enforced,
which is where ADR 0007's removal semantics, ADR 0010's type locking, and ADR 0013's
materialization choice already live.

The cost is a worse editing experience than a bespoke UI would give: placing equipment means
following a link into a Django admin form rather than clicking a slot. That is accepted for
v1, and the deep link with prefilled query parameters narrows the gap considerably — the
operator lands on the right form with the rack and ordinal already filled in.

## Viewers leave the admin, and that sets v1's real size

`AdminSite.has_permission()` gates on `is_active and is_staff`, so `is_staff=False` is a
complete lockout — not a permission narrowing but an inability to load any admin page. That
is the strongest available answer to "can we block users from the admin," and it needs no
custom `AdminSite` subclass and no reverse-proxy rule.

It also means the UI becomes the entire product for a Viewer, which is why decision 5 is
read-*parity* rather than the handful of shaped screens the sketch draws. A Viewer who cannot
reach the admin and cannot see switch port VLAN profiles in the new UI has simply lost access
to data `CONTEXT.md` promises them ("Can see all data"). Parity is what keeps that definition
true, and decision 6 exists for the same reason: `sync_roles.py` grants Viewers
`auditlog.view_logentry` deliberately, on the stated reasoning that "who-changed-what is part
of" seeing all data.

Most of that parity is cheap. The five sketched views — rack elevation, address map, device,
spare pool — are the shaped ones that justify the project; the rest are plain tables and
detail pages over models that are small and mostly static.

## Prerequisites

Three things are missing from the current configuration and must land with the first view:

- **`LOGIN_URL` and `LOGIN_REDIRECT_URL` are unset**, so Django falls back to
  `/accounts/login/`, which `config/urls.py` does not route. Any `@login_required` view would
  redirect a logged-out user to a 404. Either mount `django.contrib.auth.urls` or point
  `LOGIN_URL` at the admin login — the latter is fewer moving parts, but means a non-staff
  Viewer logs in through a page branded "Django administration", so mounting the auth URLs is
  preferred.
- **`TEMPLATES["DIRS"]` is `[]`** with `APP_DIRS: True`. Either add a project-level template
  directory or keep every template under `inventory/templates/`, alongside the existing
  admin overrides.
- **The audit trail must be reachable without the admin**, per decision 6, before any Viewer
  is flipped to `is_staff=False`. Flipping first would silently narrow the Viewer role.

## Rejected alternatives

**Read plus targeted writes** — placing equipment, moving between racks, flipping
DHCP/static. Rejected for v1 because those are precisely the operations with the most
validation behind them, and duplicating that validation is how the two frontends start
disagreeing. The deep link delivers the same outcome without a second write path. This is the
obvious v2, and nothing in v1 forecloses it.

**Read-only v1 with a v2 write set specified now.** Rejected as designing twice: a deferred
phase nobody schedules is indistinguishable from not having one, plus the overhead of having
decided its contents in advance of the usage that should inform them.

**Replacing the admin outright.** Rejected — it would mean rebuilding removal confirmations,
type-locking, materialization choices, formsets and the audit actor plumbing, for models
whose editing needs are genuinely well served by admin forms.

**Keeping Viewers in the admin.** Rejected because it makes the UI a convenience rather than a
boundary, and because Viewers are the one role with nothing to lose by leaving: they mutate
nothing, so every deep link in the UI is one they could not have followed anyway.

**A custom `AdminSite` subclass overriding `has_permission()`.** Considered as a finer-grained
lockout than the `is_staff` flag, and unnecessary: role-shaped access is exactly what
`is_staff` expresses here, and the subclass would be a second place for admin access rules to
live.

## Consequences

- **Django admin remains a dependency of the product, not just of development.** Editors and
  Admins use it for every mutation, so its customizations stay first-class and the "Frontend:
  Django admin (customized) for now" line in `DESIGN.md` becomes "Django admin for mutation,
  purpose-built UI for reading" rather than being retired.

- **`ROADMAP.md` gains a phase** for the read-only UI, replacing the "Purpose-built frontend
  beyond Django admin" bullet in "Later / not yet designed."

- **A Viewer's login lands somewhere new.** Once `is_staff=False`, `LOGIN_REDIRECT_URL` must
  point at `/` rather than the admin index, or a Viewer logs in and is immediately refused.

- **Permission checks are duplicated in intent, not in code.** The UI's `has_perm` calls read
  the same codenames the admin's own checks read, so a change to `sync_roles.py` reaches both
  without edits. Nothing enforces that the two agree beyond their sharing one permission
  table, which is the point.

- **Deep links couple the UI to admin URL names.** `admin:inventory_<model>_change` is stable
  Django API, but it does tie a template to the admin remaining mounted at all. If the admin
  is ever removed, every link breaks — which is the honest signal that removing it means
  building the write path first.
