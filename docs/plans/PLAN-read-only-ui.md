> **Revision 3** — Stage A is implemented and merged (`f70790b`, #51). This revision
> specifies **Stage B** to the same depth, incorporating review notes from
> `REVIEW-1-PLAN-read-only-ui-stage-b.md`. See "Review response — revision 3" for the
> mapping. Stage A's sections are left as built, except for two corrected citations.
>
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

Added in revision 3 (Stage B):

13. **The shaped views are canonical for Rack and NetworkDevice; their generic parity URLs
    redirect there.** The alternative — a second, complete generic detail page alongside the
    shaped one — would make two pages the source of truth for the same object. Instead the
    shaped page absorbs the fields it was missing. See "Canonical detail".
14. **A runtime permission decorator, not `@permission_required`.** One view serves eight
    models, and Stage A's decorator captures its codename list at import time. See "Access
    control on the generic views".
15. **The audit view renders `LogEntry.changes` and nothing else** — no object snapshots, no
    applying today's tracking config to yesterday's rows, and its own renderer rather than
    `changes_display_dict()`. See "Audit view".
16. **Standard `Paginator`, page size 50, cost accepted.** Keyset pagination is the better
    shape for an unbounded append-only table, but this is a 21-rack installation and the `no
    JavaScript` constraint makes prev/next links the interaction either way. Recorded as a
    deliberate trade, with the escape hatch named.

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

## Review response — revision 3 (Stage B)

Findings from `REVIEW-1-PLAN-read-only-ui-stage-b.md` (codex, `gpt-5.6`, reasoning effort
high), reviewing **Stage B only** against the merged Stage A. No P0 findings; five P1, two
P2. All eight folded in; none rejected. Every citation was re-checked against the code
before folding — all eight were accurate, including the `changes_display_dict()` crash,
which was confirmed by reading the installed package.

| Note | Resolution | Section |
|---|---|---|
| 1 [P1] Registry contract too vague — accessors, formatting, canonical URLs, query loading, pagination and permission derivation all undecided | **Folded in.** The registry is now specified as three frozen dataclasses (`FieldSpec`, `InlineSpec`, `ModelSpec`) with every key named, plus the render vocabulary, the null/choice/m2m rules, and the 404 behaviour | Stage B → The registry |
| 2 [P1] Parity inventory doesn't match the admin — there are six inlines, not the five listed; "allowed VLANs" is a form field not an inline; RackTemplate's VLAN m2m is omitted entirely; four computed columns unaccounted for | **Folded in.** Verified every citation: `admin.py:166/691/716/738/815` are the six inlines, `:252` and `:322` are the two `ModelMultipleChoiceField`s, and `allowed_vlans_display` (`:881`), `vlans_display` (`:918`), `profile_summary` (`:766`) and `default_gateway` (`models.py:4640`) are all real. The registry now enumerates all six inlines, both m2m memberships and all four computed values explicitly | Stage B → Parity inventory |
| 3 [P1] "Links across" to the shaped Rack/Device pages breaks parity — the device page omits port number, slot offset and the switch-port relation that the admin shows | **Folded in**, taking the recommended option as decision 13. Verified against `device_detail.html:37-64`: the port table has Description / VLAN / Type / Address / Gateway / Switch and no port number or numeric offset. The shaped page absorbs the missing fields and becomes canonical; the generic URL 301s to it | Decisions, Stage B → Canonical detail |
| 4 [P1] A runtime per-slug permission set cannot be expressed with Stage A's `@permission_required([...])` — the list is captured at import time | **Folded in** as decision 14. Verified at `views.py:548-572`: the decorator takes a literal list. A `registry_permission_required("list"\|"detail")` decorator is now specified, innermost, with `@login_required` still outermost | Stage B → Access control on the generic views |
| 5 [P1] Audit design neither honest nor robust — 16 tracked entries not 8, mixed `include_fields`/`exclude_fields`/`m2m_fields`, and `changes_display_dict()` crashes on a stale content type | **Folded in** as decision 15. Confirmed by reading the installed `django-auditlog` 3.4.1: `changes_display_dict` does `model = self.content_type.model_class()` then `model._meta.model` with no null guard, so it raises `AttributeError` for an uninstalled model *despite its own docstring claiming it handles that*. Scalar-vs-m2m shapes, the `SET_NULL` actor, `actor_email` and the field-scoping disclosure are all now specified | Stage B → Audit view |
| 6 [P2] "Paginated, filterable" leaves page size, ordering, filter params, invalid-value behaviour and query loading open | **Folded in** as decision 16. Standard `Paginator` at 50/page with the `COUNT(*)` cost accepted and keyset named as the escape hatch; ordering, the three filters, invalid-value handling and `select_related` all pinned | Stage B → Audit view |
| 7 [P1] Tests and verification insufficient for Stage B | **Folded in.** The test section is rewritten against distinctive-fixture assertions, per-codename 403s, the six audit edge cases, pagination boundaries and query budgets; `## Verification` gains a Stage B block that requires *running* the suite and reporting counts rather than inferring them — the `ADR 0015` Follow-up lesson | Stage B → Tests, Verification |
| 8 [P2] Stage B omits navigation and its own roadmap update; spare-pool switch links still point into the admin | **Folded in.** Verified `base.html:13-20` (Index / Spare Pool / Admin only) and `spare_pool.html:21` (switch links go to `admin:inventory_networkswitch_change`). Nav gains a conditional Audit link, the index gains an "All records" panel, and the switch link is retargeted | Stage B → Navigation |

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
read-only property at `models.py:4640` — do not recompute), and the connected switch port via
`NetworkDevicePort.switch` (`models.py:4634`). Companion tether rendered if either side of the
pair is set. Deep link to `admin:inventory_networkdevice_change`.

*(Revision 3 citation fix — both line numbers were off by roughly 35. Stage B extends this
table further; see "Canonical detail".)*

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

**Parity is measured against the admin, not against the models.** The gap the review found is
that `admin.py` shows several values that are not fields — computed columns, two m2m
memberships rendered as form widgets rather than inlines — and a registry built by walking
`_meta.fields` would silently drop every one of them. The inventory below is the checklist.

## Files

| File | Change |
|---|---|
| `inventory/urls.py` | three routes: `model_list`, `model_detail`, `audit` |
| `inventory/views.py` | the registry dataclasses, the registry itself, `registry_permission_required`, the two generic views, the audit view, the audit-panel helper |
| `inventory/templates/inventory/model_list.html` | **new** |
| `inventory/templates/inventory/model_detail.html` | **new** |
| `inventory/templates/inventory/audit.html` | **new** |
| `inventory/templates/inventory/_audit_panel.html` | **new** — the per-object include |
| `inventory/templates/inventory/_changes.html` | **new** — renders one `LogEntry.changes` |
| `inventory/templates/inventory/base.html` | nav gains a conditional Audit link |
| `inventory/templates/inventory/index.html` | gains the "All records" panel |
| `inventory/templates/inventory/spare_pool.html` | switch link retargeted off the admin |
| `inventory/templates/inventory/device_detail.html` | the three missing port columns; audit panel |
| `inventory/templates/inventory/rack_detail.html` | audit panel |
| `inventory/static/inventory/na9k.css` | styles for the new pages, same token set |
| `inventory/test_ui.py` | extended |
| `ROADMAP.md` | phase 15 checkboxes at lines 142-143 |

No model changes, no migration, no new dependency — same as Stage A.

## Routes

```
/models/<slug>/           inventory:model_list
/models/<slug>/<int:pk>/  inventory:model_detail
/audit/                   inventory:audit
```

`<slug>` is a `str` converter matched against the registry; anything not in it is a 404, not a
500. Detail pks stay integers (decision 9).

## The registry

Module-level in `views.py` (decision 7 — no package split), built from three frozen
dataclasses. This is the whole contract; nothing about a model's page is decided in a
template.

```python
@dataclass(frozen=True)
class FieldSpec:
    label: str
    accessor: str | Callable[[Any], Any]   # dotted path ("profile.native_vlan.vlan_id")
                                           # or a pure function of the object
    render: Literal["text", "choice", "boolean", "relation", "m2m"] = "text"

@dataclass(frozen=True)
class InlineSpec:
    label: str                             # panel heading, e.g. "Ports"
    accessor: str                          # reverse relation name on the parent
    columns: tuple[FieldSpec, ...]
    ordering: tuple[str, ...]
    permissions: tuple[str, ...]           # the inline model's own view_ codename(s)

@dataclass(frozen=True)
class ModelSpec:
    slug: str
    model: type[Model]
    label: str
    label_plural: str
    list_columns: tuple[FieldSpec, ...]
    detail_fields: tuple[FieldSpec, ...]
    inlines: tuple[InlineSpec, ...] = ()
    canonical_detail_view: str | None = None    # e.g. "inventory:rack"
    ordering: tuple[str, ...] = ()
    list_select_related: tuple[str, ...] = ()
    list_prefetch_related: tuple[str, ...] = ()
    detail_select_related: tuple[str, ...] = ()
    detail_prefetch_related: tuple[str, ...] = ()
    list_permissions: tuple[str, ...] = ()
    detail_permissions: tuple[str, ...] = ()
```

`REGISTRY: dict[str, ModelSpec]` keyed by slug, plus a derived
`_SPEC_BY_MODEL: dict[type[Model], ModelSpec]` so a `relation` value can find the target's
canonical URL. Build the second from the first at import time — do not hand-maintain two maps.

**Render vocabulary**, fixed so templates hold no per-model logic:

| `render` | Behaviour |
|---|---|
| `text` | `str(value)`; `None` or `""` renders `—` |
| `choice` | the model's `get_<field>_display()`; `—` when unset |
| `boolean` | `Yes` / `No` — never a bare `True`/`False`, and never a blank for `False` |
| `relation` | link to the target's canonical detail URL if its model is in the registry *and* the user holds that model's view codename; otherwise plain `str(value)`. `None` renders `—` |
| `m2m` | the related objects sorted by their natural ordering, each rendered as `relation` would, comma-joined; empty renders `—` |

`accessor` resolution walks dotted attributes and returns `None` the moment any hop is
`None` — the same defensive posture as `safe_slot_address` (Stage A): a read-only page must
never 500 on data the write path allowed. A callable accessor is called with the object and
must not touch the database beyond what the spec's `select_related`/`prefetch_related`
declares.

`canonical_detail_view` is the name of a shaped view; see "Canonical detail".

### Parity inventory — what each entry must reproduce

Checked against `admin.py` (review note 2). **Six** inlines exist, not five, and two m2m
memberships are rendered as `ModelMultipleChoiceField`s rather than inlines, so a naive
"inlines" sweep misses both.

| Registry entry | Must include |
|---|---|
| `VLAN` | its own fields; `subnet` blank is legitimate (L2-only), render `—` not a crash |
| `SwitchPortVlanProfile` | `port_mode` as `choice`; `native_vlan` as `relation`; `all_vlans_allowed` as `boolean`; **`allowed_vlans` as `m2m`** — the admin shows this via `allowed_vlans_display` (`admin.py:881`) fed by a form field at `admin.py:252`, *not* an inline |
| `RackTemplate` | `slot_count`; **`vlans` as `m2m`** — `vlans_display` (`admin.py:918`), form field at `admin.py:322`. The revision-2 text omitted this entirely |
| `Rack` | inline **Rack VLAN ranges** (`admin.py:166`): vlan, `address_range`. `canonical_detail_view = "inventory:rack"` |
| `NetworkSwitchType` | inline **Type ports** (`admin.py:691`), columns exactly `port_number`, `description`, `port_type`, `profile` (`:694`) |
| `NetworkSwitch` | inline **Addresses** (`admin.py:172`); inline **Ports** (`admin.py:738`) with columns `port_number`, `port_type`, `description`, `profile`, and **`profile_summary`** — the computed "mode, native VLAN, allowed VLANs" string at `admin.py:766`. Reuse the admin's method or extract it; do not reimplement the formatting twice |
| `NetworkDeviceType` | inline **Type ports** (`admin.py:716`), columns exactly `port_number`, `description`, `port_type`, `vlan`, `slot_offset` (`:720`) |
| `NetworkDevice` | inline **Ports** (`admin.py:815`) with the full declared column list at `:828` including `slot_offset` and **`default_gateway`** (`models.py:4640`). `canonical_detail_view = "inventory:device"` |

`profile_summary` reads `profile.native_vlan` and `profile.allowed_vlans` per row; the admin
guards that with `select_related("profile__native_vlan").prefetch_related(
"profile__allowed_vlans")` (`admin.py:752-765`). The spec's `detail_prefetch_related` must
carry the same hints or the switch detail page is an N+1 per port.

### Canonical detail

Decision 13. Rack and NetworkDevice already have shaped pages, and revision 2's "the parity
detail page links across to it" left it ambiguous whether the generic page also renders the
full field set. It must not — two pages claiming to be the record of one object is exactly
the duplication decision 5 exists to avoid.

So: when a spec declares `canonical_detail_view`, `model_detail` issues a permanent redirect
to `reverse(canonical_detail_view, args=[pk])` before rendering anything. A redirect is still
a read; `@require_GET` is unaffected.

That makes the shaped page responsible for parity, and `device_detail.html` currently is not
(review note 3, verified against `device_detail.html:37-64`). Its port table shows
Description / VLAN / Type / Address / Gateway / Switch. Add:

- **Port number** — `port.port_number`.
- **Offset** — the numeric `slot_offset`. The existing `derived` badge says *that* an address
  is derived; the admin shows *by how much*, and under ADR 0017 the offset is the whole
  content of the derivation.
- **Switch port**, not just the switch — the admin's inline exposes the specific
  `NetworkDevicePort.switch_port`; the shaped page collapses it to `port.switch`
  (`device_detail.html:60-68`). Show the port, keeping the existing rack link.

The rack elevation is already at parity with `RackAdmin` (`list_display = ["name",
"slot_count"]` plus the VLAN-range inline, all of which the elevation renders), so it needs
no field additions — only the audit panel.

### Access control on the generic views

Decision 14. Stage A's pattern cannot work here: `@permission_required([...])` at
`views.py:548-572` binds a literal list at import time, and one view now serves eight
different permission sets. Specify a decorator:

```python
def registry_permission_required(which: Literal["list", "detail"]):
    """Innermost decorator. Resolves <slug> to its ModelSpec, 404s an unknown
    slug, then checks the spec's own codename set with has_perms()."""
```

raising `PermissionDenied` (→ 403) on failure. Stacking order is unchanged and still
load-bearing:

```python
@login_required                       # outermost — logged-out gets the login redirect
@require_GET
@registry_permission_required("detail")
```

**Both sets follow the same rule: declare every codename the page actually reads.**
`list_permissions` is the spec's own `view_` codename **plus the view codename of every model
reachable through a `relation` or `m2m` column in `list_columns`** — the profile list prints
native-VLAN and allowed-VLAN values, so it reads `VLAN` and must say so. `detail_permissions`
is the same rule over `detail_fields`, plus every inline's `permissions`. Enumerate both sets
explicitly per entry; do not compute them from the field specs at import time, because a
computed set is invisible in review.

`relation`'s degradation to unlinked text is about **linking, not disclosure** — the value is
still printed either way, so it is not a substitute for holding the target's codename. It
exists so that a page whose *own* permissions are satisfied does not emit a link the user
would only be refused at. This mirrors what Stage A settled at `views.py:867-878`, where the
device page's codename list was widened for exactly this reason.

*(Revision 3 correction, made during the code review: this section originally read
"`list_permissions` is the spec's own `view_` codename", which contradicts the rule stated
everywhere else in this plan and would have let a user without `view_vlan` read VLAN names
off the profile list. The implementation declares the wider set and is correct; this text
was wrong.)*

The audit panel is **not** in `detail_permissions`. It renders inside
`{% if perms.auditlog.view_logentry %}`, so a user without it loses the panel rather than the
page — folding it into the page's required set would turn a missing audit grant into a 403 on
the rack elevation, which is a regression against Stage A.

## Audit view

`/audit/` over `auditlog.LogEntry`, gated on `auditlog.view_logentry` — granted to Viewers
deliberately at `sync_roles.py:40-45` ("who-changed-what is part of" seeing all data).

**Render `LogEntry.changes` and nothing else** (decision 15). Do not reconstruct an object
state, and do not apply today's `AUDITLOG_INCLUDE_TRACKING_MODELS` to an old row — tracking
config changes over time and a row records what was tracked *then*.

`AUDITLOG_INCLUDE_TRACKING_MODELS` (`settings.py:263`) has **16 entries, not 8**, mixing four
shapes: whole-model, `include_fields`, `exclude_fields`, and `m2m_fields`. The page must
therefore carry an explicit note that field coverage is per-model and that a field's absence
from an entry means *not tracked*, not *unchanged*. That sentence is the difference between an
honest audit view and a misleading one.

**Do not call `LogEntry.changes_display_dict()`.** Verified against the installed
`django-auditlog` 3.4.1: it does `model = self.content_type.model_class()` and then
`auditlog.contains(model._meta.model)` with no null check, so it raises `AttributeError` for a
content type whose model is no longer installed — despite the comment directly above it
claiming to "gracefully handle the case where the model no longer exists". Write a small
renderer over `changes_dict` instead:

- **scalar** — `{field: [old, new]}` → `old → new`, with `—` for `None`/`""`.
- **m2m** — `{field: {"type": "m2m", "operation": ..., "objects": [...]}}` (`models.py:122-127`
  in the package) → `<operation>: a, b, c`. A renderer that assumes a two-element list
  crashes or renders garbage on every profile/template VLAN change.
- **empty or null `changes`** — "No field changes recorded", not a blank cell.

**Actor**, in order: `actor` if set → `actor_email` if set → "system or deleted actor".
`actor` is `on_delete=SET_NULL` in the package, so a deleted user leaves rows behind and
`actor_email` is the retained fallback — a blank column would read as "nobody did this".

**Query and pagination** (decision 16): ordering `("-timestamp", "-pk")` — `-timestamp` alone
is not a total order and pages would duplicate or skip rows across a tie. `select_related(
"actor", "content_type")`. Django's `Paginator`, 50 per page, `COUNT(*)` cost accepted for an
installation this size; if it ever hurts, keyset prev/next over `(timestamp, pk)` is the
replacement and needs no template change. Budget: **a fixed number of queries per page,
independent of page size** — the same equal-count assertion Stage A uses.

**Filters**, all optional query parameters: `actor` (user pk), `action` (the `LogEntry.Action`
integer), `content_type` (pk). A value that does not parse is ignored and the page renders a
visible "filter ignored" note; a value that parses but matches nothing renders an empty
result, which is a different and true statement. Populate the content-type dropdown from
`LogEntry.objects.values_list("content_type", flat=True).distinct()` rather than from the
registry, so a content type for a model that no longer exists stays selectable and its rows
stay reachable.

**Per-object history** is the same queryset narrowed by content type + object id
(`LogEntry.objects.get_for_object(obj)`), rendered by the shared `_audit_panel.html` include
on the rack elevation, the switch parity detail page and the device detail page (ADR 0020
decision 6). Same renderer, same actor fallback, capped at the most recent 20 with a link
through to `/audit/` filtered to that content type.

## Navigation

Review note 8, verified: `base.html:13-20` offers only Index, Spare Pool and Admin, and
`spare_pool.html:21` still links switches into `admin:inventory_networkswitch_change` — which
a Stage C Viewer cannot open.

- Nav gains **Audit**, inside `{% if perms.auditlog.view_logentry %}`.
- `index.html` gains an **All records** panel listing the eight model lists, each link shown
  only if the user holds that spec's `list_permissions`. This is the discovery surface for
  `/models/<slug>/`; it avoids a ninth route and keeps the nav bar short.
- `spare_pool.html`'s switch link retargets to `inventory:model_detail` for
  `networkswitch`. The device link already goes to `inventory:device` and stays.

## Tests

Extending `inventory/test_ui.py` (decision 9).

**Parity, with distinctive fixtures** (review note 7) — every assertion names a value that
could only come from the right place, never merely `assertContains(response, "VLAN")`:

- Each of the eight list pages renders every declared `list_column` for a fixture row.
- Each of the six detail pages renders every declared `detail_field` and every inline column.
- The two m2m memberships specifically: a `SwitchPortVlanProfile` with two allowed VLANs, and
  a `RackTemplate` with two VLANs, each rendering both — these are the values a `_meta.fields`
  walk would have dropped.
- `profile_summary` renders mode, native VLAN and allowed VLANs for a switch port, and matches
  what `NetworkSwitchPortInline.profile_summary` returns for the same object.
- `default_gateway` renders on a device port, and renders `—` for a DHCP port.
- Rack and NetworkDevice `model_detail` URLs **redirect** to their shaped views (assert the
  redirect target, not just a 3xx).
- The shaped device page renders port number, numeric `slot_offset` and the connected switch
  *port* — the three fields review note 3 found missing.
- A `relation` to a model the user lacks the codename for renders as text with no `<a href>`.

**Access:**

- Every new route: 200 for Viewer, Editor, Admin; redirect to `/accounts/login/` when logged
  out (the decorator-order test); 403 for an authenticated user in no group.
- **Per codename**: for each spec, a user granted every codename in `detail_permissions`
  *except one* gets 403 on that detail page. This is the test that proves the sets are real
  rather than decorative.
- A user without `auditlog.view_logentry` gets 403 on `/audit/` but **200** on the rack
  elevation, with no audit panel rendered — the regression guard for the decision above.
- Unknown slug → 404. Unknown pk → 404. Neither is a 500.
- POST/PUT/PATCH/DELETE to every new route → 405.
- The Stage A **writes-nothing sweep is extended to every new route**: row counts of every
  inventory model *and* `auditlog.LogEntry` unchanged across a full GET sweep as an Admin.

**Audit edge cases** — one test each, because each is a live crash path:

- a scalar change; an m2m change; a `LogEntry` with `changes` null/empty;
- an entry whose `actor` was deleted (assert the `actor_email` fallback, then the "system or
  deleted actor" string when both are absent);
- an entry whose `content_type` points at a model not in the registry — construct it directly
  and assert 200, which is the `changes_display_dict()` crash this view exists to avoid;
- a field-scoped model (`NetworkSwitch`, `include_fields`) shows only its tracked fields and
  the page carries the coverage note.

**Pagination and filters:**

- 51 entries across two pages: no row appears twice and none is missing, asserted by pk set
  union — this is what the `-pk` tiebreak is for.
- Each filter narrows correctly; an unparseable `action=banana` renders 200 with the note.
- Per-object history returns only that object's entries, with a decoy object of the same
  content type carrying its own entries.

**Query budgets:** equal query counts for a 2-row and a 50-row list page, for each of the
eight; equal counts for a detail page whose inline has 2 rows versus 50; equal counts for the
audit page at 10 versus 50 entries.

## Verification (Stage B)

Run the commands in the shared `## Verification` section below (it follows Stage C), then — as
an authenticated Editor in the admin —
**make a real mutation** (rename a rack, add a VLAN to a rack template) and confirm it appears
both in `/audit/` and in the per-object panel on that object's page, with the right actor.
Nothing else proves the audit trail is wired to the thing it claims to record.

Then walk all eight `/models/<slug>/` lists and one detail page each against the corresponding
admin changelist open in another tab. The two must not disagree. Check specifically:

- the `SwitchPortVlanProfile` list's allowed-VLAN column against `allowed_vlans_display`;
- a switch's port inline against the admin's `profile_summary` column;
- `/models/rack/1/` and `/models/networkdevice/1/` land on the shaped pages.

**Report the actual numbers.** Run the suite and lint and report what they printed; do not
predict them from the diff. `docs/adr/0015-minimum-rack-block-size.md:107` keeps a wrong
five-failing-test prediction on the page permanently, on purpose, as the record of what that
mistake costs.

## Definition of done (Stage B)

- Full suite green against the recorded baseline; `pre-commit run --all-files` clean.
- All six inlines, both m2m memberships and all four computed values from the parity
  inventory rendered and asserted.
- The per-codename 403 tests, the audit edge-case tests and the query budgets present and
  passing.
- `ROADMAP.md:142-143` ticked — read-parity and the audit view. **Line 144 (the Viewer flip)
  stays open**; it is Stage C's, and ticking it here would claim the phase is finished.
- The PR body names decision 13 (shaped pages made canonical, generic detail redirects) as a
  change to how Stage A's device page renders, so it is not discovered in review.

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
