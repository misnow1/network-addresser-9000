> **Revision 2** — incorporates review notes from `REVIEW-1-PLAN-read-only-ui.md`.
> See "Review response" for the mapping.

# Implement ADR 0020 — the read-only purpose-built UI

## Context

`docs/adr/0020-read-only-purpose-built-ui.md` is committed (`a6f56ab`) and unimplemented.
`inventory/views.py` is a single comment line and `config/urls.py` routes only `admin/`, so
this is greenfield.

The production import (183 addresses, 21 racks) supplied the usage evidence `DESIGN.md` was
waiting on, and with it a set of questions the admin changelists answer badly or not at all:
*what does this rack look like, where does the next rack land, which offsets are free, which
equipment is unracked.* An admin changelist is a flat table of rows; every one of those
questions is about the shape of a **grid** — ordinals down, VLANs across.

Outcome: a read-only UI at `/`, four shaped views plus read-parity over the eight registered
models and the audit trail, with every mutation a deep link into the admin. Nothing in this
plan writes to the database. No model changes, no migration.

**Roadmap phase 15.** Phase 14 (ADR 0019's ordinal-suggestion helper) lands first, as its own
separate piece of work — see "Sequencing" below.

## Decisions this plan settles (ADR 0020 left them open)

Resolved with Mike, 2026-08-05.

1. **Three staged PRs off one plan document.** Stage A = prerequisites + base layout + the four
   shaped views. Stage B = read-parity + the audit view. Stage C = the Viewer `is_staff` flip.
   `/plan-cycle` runs once per stage. The roadmap phase closes when C merges.
2. **Full sketch visual language**, encodings *and* styling — mono uppercase panel headings,
   amber accent on a cool near-black ground, VLAN identity as a mono chip with a 2px hue
   underline. One hand-written CSS file, no build step, no framework.
3. **Phase 14 lands first, separately.** It is small (a suggestion helper, two admin add-form
   hooks, tests, no migration) and it is what makes ADR 0020 decision 3's "picks up ADR 0019's
   ordinal suggestion for free" actually true rather than aspirational.
4. **The Viewer flip is documentation plus a test, not automation.** `sync_roles.py` stays a
   pure group/permission command and is **unchanged**, exactly as ADR 0020 decision 4 says.
   Existing Viewers are flipped by hand in the admin; there are few of them.

Decisions taken without asking, for the record:

5. **No JavaScript, including no theme toggle.** `admin.py:539-543` (`RackAddForm`'s docstring)
   already records "this project has no JavaScript" as a live constraint that shapes
   server-side design. v1 honours `prefers-color-scheme` only. The CSS is written with
   `:root[data-theme="dark"]` / `:root[data-theme="light"]` selectors already present and
   unused, so adding a toggle later is a script tag and nothing else.
6. **`TEMPLATES["DIRS"]` stays `[]`.** ADR 0020's prerequisite list assumes a project-level
   template dir is needed; it isn't. `APP_DIRS: True` plus `inventory/templates/` already
   works — that is where the existing admin overrides live. The one thing that genuinely needs
   `DIRS` is overriding `registration/logged_out.html`, which `django.contrib.admin` ships and
   which wins over ours because it precedes `inventory` in `INSTALLED_APPS`. `LOGOUT_REDIRECT_URL
   = "/"` sidesteps that template entirely, so the prerequisite dissolves for less than it
   proposed. **One ADR prerequisite is therefore closed by not doing it** — call this out in the
   PR body. *(Review note 1 confirmed this loader-ordering argument is factually sound.)*
7. **One `inventory/views.py` module and one `inventory/urls.py`.** Matches the repo's shape
   (`models.py` at 4613 lines, `admin.py` at 1057) rather than introducing a package layout
   this codebase has no precedent for.
8. **Parity is a declarative registry, not sixteen hand-written views.** Two generic views
   driven by a per-model spec (see stage B).
9. **UI tests live in a new `inventory/test_ui.py`**, following `test_prod_import.py`'s
   precedent rather than growing `tests.py` past 6000 lines.

Added in revision 2:

10. **Only `LoginView` and `LogoutView` are mounted — not `include("django.contrib.auth.urls")`.**
    That module also routes password change and password reset, whose `POST` handlers save
    passwords and send email. ADR 0020's stated concern was that a non-staff Viewer should not
    log in through a page branded "Django administration"; two explicit routes satisfy that
    while exposing strictly less surface. See "Settings" for the Stage C consequence.
11. **An elevation cell holds a collection of addresses, not one**, and ordinals covered by a
    span are rendered as continuations rather than empty. See "Rack elevation".
12. **Every view is `@require_GET`**, and the test suite proves the UI writes nothing rather
    than asserting it in prose.

## Review response

Findings from `REVIEW-1-PLAN-read-only-ui.md` (codex, `gpt-5.6`, reasoning effort high).
No P0 findings. All ten folded in; none rejected.

| Note | Resolution | Section |
|---|---|---|
| 1 [P1] `{ordinal: occupant}` insufficient — continuation ordinals look empty and get a spurious add-link; a cell can legitimately need several addresses | **Folded in.** Verified: `NetworkDevicePort`'s constraints are `(device, description)`, `(device, ordinal)`, `(vlan, address)` — nothing forbids two ports on one `(device, vlan, slot_offset)`, so a cell must hold a list. Occupancy map now expands across spans with an explicit continuation marker | Rack elevation |
| 2 [P1] Prefetch cannot meet the query budget — `NetworkDeviceType.slot_span` runs `aggregate(Max(...))` on every access | **Folded in.** Verified at `models.py:2689`. Spans are now bulk-resolved in one grouped query; the property is not called per device. Test asserts *equal* query counts for a 2-device and a 50-device rack, which an after-the-fact number could not | Rack elevation → Query budget |
| 3 [P1] Legal inputs 500 the page — L2-only VLANs have `subnet=""`, and stored range text may be malformed | **Folded in.** Verified: `subnet` is `blank=True` (`models.py:467`) and the seeded VLAN 1 is subnet-less. A view-layer helper now mirrors `_suggest_rack_slot_address`'s validate-and-catch (`models.py:289`); the index does not link subnet-less VLANs to a map | Rack elevation, Address map, Index |
| 4 [P1] `django.contrib.auth.urls` opens password change/reset write paths | **Folded in** as decision 10 — mount `LoginView`/`LogoutView` only. Does not contradict ADR 0020, whose concern was login-page branding, and exposes strictly less | Settings |
| 5 [P1] One codename per view is wrong for multi-model pages; `login_required` must be outermost | **Folded in.** Each view now declares the full set of codenames it actually reads. Decorator order fixed — `permission_required(raise_exception=True)` 403s anonymous users too if it runs first | Access control |
| 6 [P1] Nothing proves the UI writes nothing — a function view accepts POST unless it says otherwise | **Folded in.** `@require_GET` on every view, plus tests asserting 405 on POST and zero new rows / zero new `LogEntry`s across a full GET sweep | Access control, Tests |
| 7 [P2] Encoding tests can pass with encodings in the wrong cells | **Folded in.** Assertions now name cell coordinates and include negative controls | Tests |
| 8 [P2] Verification not executable from a fresh DB; examples use labels where routes take int pks | **Folded in.** Verification now runs `import_prod_data` first (it refuses a DB that already has a Rack) and resolves pks rather than hard-coding labels | Verification |
| 9 [P2] Deep-link test only proves the URL resolves | **Folded in.** Test now GETs the admin add page and asserts the bound form's initial values | Tests |
| 10 [P3] Two citations wrong — no-JS is `admin.py:539-543` not `:429-432`; audit perm is `sync_roles.py:40-45` not `:44-47` | **Folded in.** Both corrected in place (decision 5 above, Stage B audit section) | Decisions, Stage B |

## Sequencing

| | Work | Depends on |
|---|---|---|
| **Phase 14** | ADR 0019 ordinal suggestion helper + admin add-form wiring | — |
| **Stage A** | Prereqs, base layout, four shaped views | nothing (phase 14 improves it, doesn't gate it) |
| **Stage B** | Read-parity + audit view | A |
| **Stage C** | Viewer `is_staff=False` | B (roadmap gates the flip on parity + audit landing) |

Phase 14 is *not* a hard dependency: the UI supplies the ordinal itself from the empty slot the
operator clicked, so `?rack=3&rack_slot=6` is correct with or without it. Doing it first means
the admin-first creation path agrees with the UI-first one.

**On the incomplete-UI window** (review note context): Stage A ships a UI that does not yet
reach read-parity, but no Viewer is locked out of the admin until Stage C, so nobody loses
access to data in the meantime. That ordering is deliberate and is what ADR 0020's prerequisite
list already requires.

---

# Stage A — prerequisites, layout, four shaped views

## Files

| File | Change |
|---|---|
| `config/settings.py` | `LOGIN_URL`, `LOGIN_REDIRECT_URL`, `LOGOUT_REDIRECT_URL` |
| `config/urls.py` | `LoginView`/`LogoutView` routes, `path("", include("inventory.urls"))` — admin path untouched |
| `inventory/urls.py` | **new** — `app_name = "inventory"`, five routes |
| `inventory/views.py` | **new content** — index + four shaped views + the span/address helpers |
| `inventory/templates/inventory/*.html` | **new** — base, index, rack elevation, address map, device, spare pool |
| `inventory/templates/registration/login.html` | **new** |
| `inventory/static/inventory/na9k.css` | **new** — the whole stylesheet |
| `inventory/test_ui.py` | **new** |

## Settings and URLs

```python
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"
```

Route `LoginView` and `LogoutView` **explicitly** (decision 10), not the whole auth module:

```python
path("accounts/login/", LoginView.as_view(), name="login"),
path("accounts/logout/", LogoutView.as_view(), name="logout"),
```

The route names must stay `login`/`logout` — `LOGIN_URL` and Django's own
`{% url 'login' %}` resolution depend on them.

`LogoutView` is POST-only in Django 6.0.7 (confirmed by review note 1), so the nav's sign-out
is a one-button `<form method="post">` with `{% csrf_token %}`. That is Django's own auth view,
not a write path of ours — ADR 0020 decision 2's "no `POST` handlers" is about our views.

**Stage C consequence to carry forward:** once Viewers are `is_staff=False` they lose the
admin's password-change form and, with password reset unmounted, have no self-service way to
change a password. Stage C decides whether to mount `PasswordChangeView` for them or to leave
password changes an admin-performed operation. Not Stage A's problem, but it must not be
discovered in Stage C by surprise.

## Access control

Every view carries, in this order:

```python
@login_required                                    # outermost — must run first
@require_GET
@permission_required([...codenames...], raise_exception=True)
```

**Order is load-bearing** (review note 5): `permission_required(raise_exception=True)` raises
403 for *anonymous* users too, so if it runs before `login_required` a logged-out visitor gets
a 403 instead of the login redirect the plan promises.

Each view declares the full set of codenames it actually reads, not one token model
(review note 5):

| View | Codenames |
|---|---|
| index | `view_rack`, `view_vlan`, `view_networkswitch`, `view_networkdevice` |
| rack elevation | `view_rack`, `view_vlan`, `view_networkswitch`, `view_networkdevice` |
| address map | `view_vlan`, `view_rack`, `view_networkswitch`, `view_networkdevice` |
| device detail | `view_networkdevice`, `view_vlan` |
| spare pool | `view_networkswitch`, `view_networkdevice` |

All three roles hold every `view_` codename via `sync_roles.py`, so in practice this is a
floor, not a narrowing — but a hand-made user with partial grants must not see data it lacks
the codename for. ADR 0020 decision 4: same codenames the admin reads, no new machinery.

`@require_GET` is what makes ADR 0020's "writes nothing" claim structural rather than
aspirational (review note 6) — an ordinary function view accepts POST unless it says
otherwise.

The admin link in the nav renders only under `{% if request.user.is_staff %}` (decision 8).

## Routes

```
/                     inventory:index          dashboard
/racks/<int:pk>/      inventory:rack           rack elevation
/vlans/<int:pk>/      inventory:vlan_map       address map
/devices/<int:pk>/    inventory:device         device detail
/spares/              inventory:spares         spare pool
```

Integer primary keys, not business labels (review note 8) — `WPC1SRU` is a rack *name*, and
VLAN 201's pk is not 201.

## Shared helpers (write these first)

Both live in `views.py` and are what keeps the views themselves thin:

**`safe_slot_address(range_cidr, ordinal)`** — mirrors `_suggest_rack_slot_address`'s existing
validate-then-catch discipline (`models.py:289-320`): validate the CIDR, call
`suggestions.suggest_slot_address`, and return `None` on `ValidationError`/`ValueError` rather
than propagating. Stored range text can be malformed (a bare `save()` bypasses `clean()`, which
the model layer already anticipates) and an ordinal past the block's end raises. A read-only
page must never 500 on data the write path allowed (review note 3).

**`resolve_slot_spans(devices)`** — one grouped query resolving `max(slot_offset) + 1` per
`NetworkDeviceType` across every device on the page, returned as `{device_type_id: span}`.
`NetworkDeviceType.slot_span` runs `aggregate(Max(...))` on **every** access (`models.py:2689`),
so prefetching `device_type` does not cache it and calling the property per device is an N+1
(review note 2). The property stays the single source of the *rule*; this is a bulk evaluation
of the same rule, and a test asserts the two agree.

## The four shaped views

### Rack elevation — `rack_detail`

The centrepiece. **Rows are ordinals `1..rack.slot_count`, columns are the rack's VLANs**
(`rack.vlan_ranges` ordered by `vlan__vlan_id`). Indexed by ordinal, not by device — that is
what gives a spanning device somewhere to put its second address (ADR 0017), which a
one-row-per-device grid cannot express.

**Occupancy map (revised per review note 1).** Build `{ordinal: cell}` covering *every* ordinal
a device claims, not just its starting `rack_slot`:

- an occupant's `rack_slot` gets a `start` cell;
- ordinals `rack_slot + 1 … rack_slot + span - 1` get `continuation` cells pointing at the same
  occupant. **A continuation is not empty** and must not render the add-device deep link — the
  slot is taken.
- everything else is a genuine `empty` cell.

**A cell's address slot holds a list, not a value.** `NetworkDevicePort` is unique on
`(device, description)`, `(device, ordinal)` and `(vlan, address)` — nothing forbids two ports
on the same `(device, vlan, slot_offset)`, which is exactly the `#27` bridged-jack shape the
roadmap still carries as unsolved. Render every address in the cell rather than silently
picking one. Ports map to a row by `device.rack_slot + port.slot_offset`, and to a column by
`port.vlan_id`.

Encodings, each of which exists to prevent a specific documented failure:

- **Solid bracket spanning ordinals** — a device whose span > 1 (ADR 0017 derived addressing).
  Its offset ports carry a `derived` tag marking the address read-only. Spans come from
  `resolve_slot_spans`, not from per-device property access.
- **Dashed tether** — an ADR 0018 companion pair (`device.host` / its reverse). Deliberately
  *unlike* the bracket: a companion's address is independent and unpredictable from its host's.
  Production proves it — the DM7C's interface sits an address *below* its host, the DM3's
  *above*. An operator must never read one as the other. (Review note 1 confirmed supported
  creation and moves keep both halves in the same rack, so the tether is always intra-rack.)
- **Em-dash in an address cell** — this occupant has no port on this VLAN. Absence, not missing
  data. `PROD-DATA-ANALYSIS.md` §5.4 found 34 of 229 production addresses assigned to
  interfaces that don't physically exist; rendering absence explicitly is how that stops
  recurring.
- **Empty ordinal renders the address it *would* get**, greyed, via `safe_slot_address` — the
  useful fact about a free slot is what it hands out. Blank, not a crash, when that returns
  `None`. Each empty ordinal deep-links to
  `admin:inventory_networkdevice_add?rack=<pk>&rack_slot=<ordinal>` (ADR 0020 decision 3).

**Query budget.** Prefetch `vlan_ranges__vlan`, `switches__addresses__vlan`,
`devices__ports__vlan`, `devices__device_type`, and the companion relation; resolve spans in
one grouped query. The test asserts **equal** query counts for a 2-device rack and a 50-device
rack, which is what actually catches an N+1 — a single number recorded after the fact would
just bless whatever the implementation does (review note 2).

### Address map — `vlan_map`

For one VLAN, the shape of its subnet:

- **Hatched fill** = the bottom-`/24` DHCP convention (allocated to nothing).
- **Coloured fill** = another rack holds this block, labelled with the rack's name and linked
  to its elevation.
- Hatched ≠ coloured on purpose: two different kinds of unavailable.
- **Where the next rack lands** — `suggest_rack_vlan_range(vlan.subnet, slot_count, used, dhcp)`
  against ADR 0015's `/27` floor as the reference size, shown as a banner. ADR 0019 made offset
  space reservable with an empty Rack, so this is now a backstop rather than the only guard —
  word it that way.
- Below the map, addresses in use on this VLAN (switch addresses + device ports), sorted
  numerically by `ipaddress.IPv4Address`, each linking to its owner.

**L2-only VLANs (review note 3).** `VLAN.subnet` is `blank=True` (`models.py:467`) and the
seeded VLAN 1 is subnet-less; `suggest_rack_vlan_range("")` constructs `IPv4Network("")` and
raises. This view renders an explicit "L2-only — no tracked addressing" state for such a VLAN
and never reaches the suggester. The index does not offer a map link for them at all.

### Device detail — `device_detail`

Type, rack + ordinal (linked to the elevation), and the port table: description, VLAN chip,
port type, address or `DHCP`, `derived` tag where `slot_offset > 0`, `default_gateway` (the
read-only property at `models.py:4604` — do not recompute), and the connected switch port via
`NetworkDevicePort.switch` (`models.py:4599`). Companion tether rendered if either side of the
pair is set. Deep link to `admin:inventory_networkdevice_change`.

### Spare pool — `spare_pool`

`NetworkSwitch.objects.filter(rack__isnull=True)` and the same for `NetworkDevice` — serial
number, hostname, type, and a deep link to each one's admin change form. CONTEXT.md's Spare
Pool entry is the framing: factory-DHCP equipment tracked by little more than serial and
hostname until it is racked.

### Index

Racks with occupancy counts, VLANs with utilisation, spare-pool counts. Every tile links into
one of the four views above — except subnet-less VLANs, which are listed without a map link.

## Styling

One file, `inventory/static/inventory/na9k.css`. Tokens in `:root`; `@media (prefers-color-scheme: dark)`
as the default signal with `:root[data-theme=…]` selectors present but unexercised (decision 5).
Mono uppercase panel-label headings against a quiet system sans body; amber signal accent on a
cool near-black ground; VLAN identity as a mono chip with a 2px hue underline **rather than a
coloured surface**, so VLAN hues never compete with the accent. The elevation grid must scroll
inside its own container on narrow screens — the page body never scrolls horizontally.

WhiteNoise + `CompressedManifestStaticFilesStorage` already serve this in the container
(`settings.py` handles the DEBUG fallback), so no static plumbing is needed.

## Tests — `inventory/test_ui.py`

**Access and method:**
- Every route returns 200 for Viewer, Editor and Admin; 403 for an authenticated user in no
  group; redirects to `/accounts/login/` when logged out (this is what catches a wrong
  decorator order — review note 5).
- A user holding only `view_rack` is refused the index and the rack view.
- POST/PUT/PATCH/DELETE to every route returns **405** (review note 6).
- **The UI writes nothing:** sweep every route with GET as an Admin, and assert the row counts
  of every inventory model *and* `auditlog.LogEntry` are unchanged from before the sweep. This
  is the test for ADR 0020's central claim.
- The admin link is absent for `is_staff=False`, present for `is_staff=True`.

**Elevation encodings — coordinates, not just presence** (review note 7):
- A spanning device's bracket covers exactly `rack_slot` through `rack_slot + span - 1`;
  the ordinal *after* its span is a normal empty slot. Negative control: the ordinal *before*
  `rack_slot` is not bracketed.
- A continuation ordinal renders no add-device link. Negative control: a genuinely empty
  ordinal does.
- The tether joins the actual host/companion pair, not merely adjacent devices — assert against
  the pks, and include a decoy device between them.
- The em-dash is at the specific `(ordinal, VLAN)` intersection where the occupant has no port,
  and *not* at intersections where it does.
- An empty ordinal shows the address `suggest_slot_address` returns for it.
- `resolve_slot_spans` agrees with `NetworkDeviceType.slot_span` for every type in a fixture.

**Robustness** (review note 3):
- A rack with a malformed `RackVlanRange.address_range` (saved via `save()`, bypassing
  `clean()`) renders 200 with blank cells, not a 500.
- An L2-only VLAN's map renders 200 in its "no tracked addressing" state.
- An ordinal beyond the range's capacity renders blank, not a 500.

**Query budget** (review note 2): equal query counts for a 2-device and a 50-device rack, on
both the elevation and the address map.

**Deep links** (review note 9): GET `admin:inventory_networkdevice_add?rack=…&rack_slot=…` as
an Editor and assert the returned form's `initial` carries both values — not merely that the
URL resolves.

---

# Stage B — read-parity and the audit trail

ADR 0020 decision 5 and the "Viewers leave the admin" section: a Viewer who cannot reach the
admin and cannot see, say, switch port VLAN profiles in the new UI has lost access to data
CONTEXT.md promises them. Parity is what keeps "Can see all data" true.

## Parity, declaratively

A module-level registry in `views.py` — model → `{list columns, detail fields, inlines,
detail-view name}` — driven by two generic views:

```
/models/<slug>/           inventory:model_list
/models/<slug>/<int:pk>/  inventory:model_detail
```

Eight entries: `VLAN`, `SwitchPortVlanProfile`, `RackTemplate`, `Rack`, `NetworkSwitchType`,
`NetworkSwitch`, `NetworkDeviceType`, `NetworkDevice`, with their inlines (rack VLAN ranges,
type ports, instance ports, switch addresses, allowed VLANs). Each entry declares **every**
codename its page reads — including the inlines' models, which is the same partial-privilege
gap review note 5 raised for Stage A. Two templates total.

Where a model already has a shaped view (Rack, NetworkDevice), the parity detail page links
across to it rather than duplicating it.

## Audit view

`/audit/` — `auditlog.LogEntry`, newest first, paginated, filterable by actor, action and
content type. Gated on `auditlog.view_logentry`, which `sync_roles.py` already grants Viewers
deliberately (`sync_roles.py:40-45` — "who-changed-what is part of" seeing all data).

Per-object history is the same query narrowed by content type + object id, included as a panel
on the rack, switch and device pages (ADR 0020 decision 6).

Read `AUDITLOG_INCLUDE_TRACKING_MODELS` in `settings.py` before writing this — tracking is
scoped per model, so some models log only a few fields and the view must not imply it is
showing more than was recorded.

## Tests

Every registry entry's list and detail return 200 for a Viewer and 403 without the codename;
each inline renders; the audit view shows a known mutation and its actor; per-object history
filters correctly. `@require_GET` and the writes-nothing sweep extend to the new routes.

---

# Stage C — Viewers leave the admin

No code that flips anything (decision 4). Deliverables:

- `CONTEXT.md` Roles section and `README` provisioning notes: Viewers are created with
  `is_staff=False`; Editors and Admins keep `is_staff=True`, because deep links are how they
  mutate.
- A test asserting a `is_staff=False` Viewer reaches **every** UI route (shaped, parity and
  audit) and is refused by `/admin/`. This is the real gate — `AdminSite.has_permission()`
  checks `is_active and is_staff`, so the flip is a total lockout, and the test is what proves
  the UI covers what the lockout removes.
- **Decide the password-change question** carried forward from Stage A's Settings section: a
  non-staff Viewer has no admin password form and no mounted reset flow. Either mount
  `PasswordChangeView` or record that password changes are admin-performed.
- `ROADMAP.md` phase 15 checkboxes closed.
- `DESIGN.md` needs no edit — line 13 was already updated when ADR 0020 landed.

---

## Verification

Per `CLAUDE.md`, source the env and never inline variables:

```bash
set -a; source .env; set +a
python manage.py test inventory          # full suite, including the new test_ui.py
ruff check . && ruff format --check .
mypy --config-file=pyproject.toml config inventory manage.py
```

Then run it against production data — which the shaped views need in order to be verifiable at
all, and which a fresh dev database does **not** have (review note 8). `import_prod_data`
refuses a database that already contains a Rack, so this runs against an empty one:

```bash
set -a; source .env; set +a
python manage.py migrate
python manage.py seed_defaults
python manage.py sync_roles
python manage.py import_prod_data          # refuses if any Rack already exists
python manage.py verify_prod_import
python manage.py runserver
```

The routes take integer pks, so resolve them rather than typing labels into the URL bar — from
the index page's links, or:

```bash
python manage.py shell -c "from inventory.models import Rack, VLAN; \
print([(r.pk, r.name) for r in Rack.objects.all()]); \
print([(v.pk, v.vlan_id, v.name) for v in VLAN.objects.all()])"
```

Then check:

- The `WPC1SRU` rack — a straightforward elevation; addresses match the admin's.
- **The `CONSOLES` rack** — the one that exercises both ADR 0017 offset pairs (bracket) and ADR
  0018 companions (tether) on one screen. If those two render identically, the encoding has
  failed and the view is wrong. Confirm the continuation ordinal under a bracketed device shows
  no add-device link.
- VLAN 201's map — the offset-864 collision the sketch surfaced; the next-free banner should
  point at it.
- The seeded subnet-less "Default VLAN" (VLAN 1) — listed on the index without a map link, and
  its map route renders the L2-only state rather than 500ing.
- A device with a port on only some of its rack's VLANs — em-dashes in the other columns, not
  blanks.
- Log in as a Viewer with `is_staff=False`: every Stage A view reachable, no admin link
  rendered, `/admin/` refuses.

Cross-check a handful of rendered addresses against `manage.py verify_prod_import` output —
the UI must agree with the importer's own verification, since both read the same rows.

## Definition of done (Stage A)

- Full suite green, no new failures against the recorded baseline.
- `pre-commit run --all-files` clean.
- The writes-nothing sweep and the equal-query-count tests both present and passing.
- `ROADMAP.md` phase 15's "Prerequisites" and "The four shaped views" checkboxes ticked; the
  remaining three left open for Stages B and C.
- PR body notes that ADR 0020's `TEMPLATES["DIRS"]` prerequisite was closed by not doing it,
  with decision 6's reasoning.
