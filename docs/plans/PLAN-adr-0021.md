> **Revision 2** — incorporates review notes from `REVIEW-1-PLAN-adr-0021.md`.
> See "Review response" for the mapping.

# Implement ADR 0021 — VLAN descriptive metadata (Department)

## Context

`docs/adr/0021-vlan-descriptive-metadata.md` reverses `CONTEXT.md`'s flat-VLAN position and
ships one of its two axes.

**Builds:** a `Department` table, an optional `PROTECT` FK from `VLAN`, admin CRUD, a VLAN
changelist filter, and read-parity in the read-only UI.

**Does not build:** `VLAN.role`. The ADR settles its shape (`TextChoices`, unique per department,
requires a department) so phase 21 does not take the wrong uniqueness scope. No column, no
`TextChoices` class, no constraint lands in this plan.

No addressing behaviour changes anywhere. Nothing here touches a suggester, a materializer, an
offset, or a stored address — if a diff in this plan reaches `suggestions.py` or any `_suggest_*`
method, something has gone wrong.

## Decisions this plan settles (ADR 0021 left them open)

Resolved with Mike, 2026-08-07.

1. **`description` is a `TextField(blank=True)`**, not a `CharField`. A department description is
   free prose with no identity role, so no length ceiling is meaningful. It is excluded from
   `search_fields` for the same reason.
2. **`related_name="vlans"`** on the FK, so the read-only Department detail page's inline accessor
   is `"vlans"` and reads naturally. Not `"+"` — this relation is genuinely traversed.
3. **The blank-name check is a `CheckConstraint`, not just `blank=False`.** Matches
   `racktemplate_name_not_blank`; `blank=False` is form-layer only and does not survive
   `objects.create(name="")`.
4. **Ordering is `["name"]`** on the model `Meta` — alphabetical, since department names have no
   inherent order the way `vlan_id` does.

## Model — `inventory/models.py`

Insert `Department` between `AuditedModel` (`:473`) and `VLAN` (`:494`), so `VLAN.department` can
reference the class directly rather than by string.

```python
class Department(AuditedModel):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~models.Q(name=""), name="department_name_not_blank"),
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name
```

**`__str__` is not optional** (review note 1). `_linked_text_for` (`views.py:1502`) and
`model_detail.html` both call `str()`, and the admin's `list_filter` and FK widget do too — without
it every one of those surfaces renders `Department object (1)`.

Docstring must carry: descriptive only, nothing branches on it, it does not scope allocation
(ADR 0021's "Department does not scope allocation" section), and no row is ever system-seeded.

**Normalization — copy `RackTemplate`'s pattern exactly** (`inventory/models.py:855–870`),
including the reasoning comment. Strip `name` in **both** `clean()` and `save()`; `Model.save()`
never calls `clean()`, so `Department.objects.create(name="Audio ")` would otherwise persist
whitespace the case-insensitive collation does not fold away. Do not add a `casefold()` — the
collation already handles case, and folding in Python would change what operators see on screen.

**`VLAN.department`**, first field on `VLAN`:

```python
department = models.ForeignKey(
    Department,
    on_delete=models.PROTECT,
    null=True,
    blank=True,
    related_name="vlans",
    help_text="Optional. The department that owns this VLAN, e.g. Audio, Lighting, Video.",
)
```

`help_text` is operator-facing: no ADR reference, per the project's on-screen-copy convention.
Rationale belongs in a code comment if it is needed at all.

## Migration — `inventory/migrations/0012_vlan_department.py`

`CreateModel` for `Department` **before** `AddField` for `VLAN.department`. **No `RunPython`, no
backfill** — every existing row is validly null, and the ADR rejected backfilling by `vlan_id`
range. Open with the destructive-database posture comment the other migrations carry
(`0011_device_companions.py:1–10`), noting that this one genuinely has nothing to backfill.

## Admin — `inventory/admin.py`

New `DepartmentAdmin`, placed immediately before `VLANAdmin` (`:851`) to match model order:

```python
@admin.register(Department)
class DepartmentAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    list_display = ["name", "description"]
    search_fields = ["name"]
    show_auditlog_history_link = True
```

No `get_readonly_fields`, no `has_delete_permission` override — a department has no locked fields
and no system rows, so `RackTemplateAdmin` (`:903`) minus its custom form is the right shape.
Deleting one that is in use is already refused by the `PROTECT` FK; Django names the blocking
VLANs itself and no custom handling is needed.

`VLANAdmin` (`:851`):
- `list_display` gains `"department"` (first, before `name`)
- `list_filter = ["department"]` — it has none today
- **`list_select_related = ["department"]`** — a declarative attribute, *not* a `get_queryset`
  override (review note 7, with a corrected rationale).

  The correction matters and belongs in the code comment. Django's changelist does auto-apply
  `select_related()` when a real FK appears in `list_display`
  (`ChangeList.has_related_field_in_list_display`), which would make an override redundant — **but
  only for non-nullable FKs.** `select_related_descend` (verified in Django 6.0.7) reads
  `if not restricted: return not field.null`, so the *bare* auto-applied call descends **nothing**
  for a nullable FK. `department` is nullable, so the N+1 is real and Django does not solve it.
  Naming the field takes the `restricted` branch, which honours the relation regardless of
  nullability. Do not write a comment claiming Django would otherwise N+1 in general — it is
  specifically nullability that defeats the automatic path.

  `RackTemplateAdmin.get_queryset`'s `prefetch_related` is **not** the precedent to copy here: it
  guards an M2M, which Django never handles automatically.

## Settings — `config/settings.py`

Add `"inventory.Department"` to `AUDITLOG_INCLUDE_TRACKING_MODELS` (`:263`), bare like
`"inventory.Rack"` — all fields tracked. `VLAN` is already registered bare, so its new
`department` field is tracked with no entry change; add a short comment saying so, since every
other entry in that tuple explains itself.

## Read-only UI — `inventory/views.py`, `inventory/templates/`

**New `REGISTRY["department"]` entry** (`views.py:1109`). Mandatory: ADR 0020 read-parity means
every admin-registered model is reachable by an `is_staff=False` Viewer, so registering a model in
the admin without a registry entry reopens the gap phase 15 closed.

```python
"department": ModelSpec(
    slug="department",
    model=Department,
    label="Department",
    label_plural="Departments",
    list_columns=(FieldSpec("Name", "name"), FieldSpec("Description", "description")),
    detail_fields=(FieldSpec("Name", "name"), FieldSpec("Description", "description")),
    inlines=(
        InlineSpec(
            label="VLANs",
            accessor="vlans",
            columns=(
                FieldSpec("Name", "name"),
                FieldSpec("VLAN ID", "vlan_id"),
                FieldSpec("Subnet", "subnet"),
            ),
            ordering=("vlan_id",),
            permissions=("inventory.view_vlan",),
        ),
    ),
    ordering=("name",),
    detail_prefetch_related=("vlans",),
    list_permissions=("inventory.view_department",),
    detail_permissions=("inventory.view_department", "inventory.view_vlan"),
),
```

The inline needs a comment recording that it is the registry's **first inline with no admin
counterpart**, and why ADR 0021 decision 6 permits it — the admin gets a `list_filter`, the
read-only UI has no filtering, so this is capability parity rather than registry drift. Without
that note the next reader will read it as an inconsistency.

**`InlineSpec.permissions` is declarative only** (review note 2, verified): nothing in `views.py`
reads it — `model_detail` (`:1695`) renders every inline unconditionally, and `_render_inline`
(`:1588`) never consults it. Access is gated one level up, by `detail_permissions` on the
`ModelSpec`. Set it for consistency with the other entries, but **do not write a test that assumes
it filters anything.**

**Permission dependencies — three additions** (review note 3). `_linked_text_for` links a relation
cell only when the viewer holds that model's own `view_` codename, so registering the model is not
by itself enough to make the link appear. This follows the convention `switchportvlanprofile` and
`racktemplate` already use (both declare `view_vlan` because they render VLAN relations):

- `REGISTRY["vlan"].list_permissions` gains `"inventory.view_department"`
- `REGISTRY["vlan"].detail_permissions` gains `"inventory.view_department"`
- `vlan_map`'s `@permission_required([...])` list (`views.py:737–748`) gains
  `"inventory.view_department"`, beside the existing comment explaining that the list names every
  model whose data appears on the page

**`REGISTRY["vlan"]`**: add `FieldSpec("Department", "department", render="relation")` to
`list_columns` and `detail_fields`, and set `list_select_related=("department",)` /
`detail_select_related=("department",)`. No template change is needed for the link —
`_linked_text_for` handles it, and returns plain text (not a crash) for a null department.

**`vlan_map`** (`views.py:780`): change `get_object_or_404(VLAN, pk=pk)` to
`get_object_or_404(VLAN.objects.select_related("department"), pk=pk)`, and add a department line to
`vlan_map.html`'s header (beside the `{{ vlan.subnet }}` line at template line ~26), rendered only
when set. This is the "address-map view" half of the roadmap item.

**Not changed:** `index.html`. Its VLAN tiles keep their flat `vlan_id` ordering (ADR 0021
decision 6); the new Departments tile appears in the "All records" panel automatically, since that
panel iterates `REGISTRY`.

## Tests

### Registry-exhaustive guards that will fail until updated

These are not incidental breakage — they are the guards phase 15 installed, firing correctly.
Review note 4 established that the rev-1 list was incomplete; this is the verified full set.

- `test_ui.py:1343` — `assertEqual(set(factories), set(REGISTRY), …)`. Add a `department` factory:
  `lambda i: Department.objects.create(name=f"QB Department {i}")`.
- `ParityFixtureMixin` (`test_ui.py:270`) — add a Department row and its `pk_by_slug` entry
  (`:390`). **The row must be assigned to `vlan_native`**, not left unconnected, or the inline and
  relation-column tests below have nothing to assert. Honour the fixture's substring-collision
  discipline: the name must not be a substring of any other value rendered on the same page, so
  something like `"StageB Grillework"`, never `"Audio"`.
- `AdminLockoutTests` markers (`test_ui.py:2133`) — add `self._list_url("department")` and
  `self._detail_url("department")` entries, or the exhaustive set equality at `:2167`
  (`set(routes) == set(canonical_targets) | set(markers)`) fails.
- `ParityContentTests.test_vlan_list_renders_every_declared_column` (`:1602`) and
  `test_vlan_detail_renders_every_declared_field` (just below) assert the **exact** cell sequence
  — both need the new Department value inserted in the right position.
- `WritesNothingTests` (`test_ui.py:670`, route list at `:721`) — review note 5: its routes are a
  **hand-maintained literal list**, not a `REGISTRY` iteration, so the new pages would go
  unswept while the test stayed green. Add a Department fixture and both routes. Preferably
  derive from `_parity_routes()`/`_all_ui_routes()` so this cannot drift again; if that is too
  invasive, add the two literals and a comment pointing at the enumeration it should have used.

### Docstring counts — read each one, do not sed

Review note 9. Hard-coded counts referring to **registry entries** change; ones referring to
**`urls.py` patterns** do not, and they are worded almost identically.

Change (8 → 9, 16 → 18, 6 → 7 non-redirecting detail pages):
`views.py:990`, `views.py:995`, `views.py:1035`, `views.py:1609`, `test_ui.py:442`,
`test_ui.py:711`, `test_ui.py:760`, `test_ui.py:1305` (the query-budget docstring — `:1305`, not
rev 1's `:1304`), and the "sixteen registry list/detail paths" clause at `test_ui.py:414`.

**Do not change:** the "all eight URL patterns in `urls.py`" clause in that same `test_ui.py:414`
comment. `urls.py` still has exactly eight `path()` entries — a ninth registry entry adds no
route, because `model_list`/`model_detail` are generic. Two different eights, one sentence apart.

### New — `inventory/tests.py`

A `DepartmentModelTests` class, sited near `RackTemplateModelTests` (`:5791`):

- A VLAN saves with no department (the optional-FK case named in the roadmap)
- `PROTECT` refuses to delete a department that has VLANs, and the error names them
- Deleting a department with **no** VLANs succeeds
- `name` is stripped by `full_clean()` **and** by a bare `objects.create()` — two separate
  assertions, since they exercise different code paths
- A case-variant duplicate (`"Audio"` then `"audio"`) is rejected — pins the collation assumption
  the ADR relies on, so it fails loudly if the DB collation ever changes
- `objects.create(name="")` violates `department_name_not_blank`
- `__str__` returns the name (review note 1 — cheap, and everything else renders through it)
- **No department exists on a fresh database, and `seed_defaults` creates none** (review note 8) —
  `Department.objects.count() == 0` after `call_command("seed_defaults")`. This is what makes ADR
  0021 decision 3 a tested claim rather than an untested intention; an accidental migration seed
  would otherwise pass everything else here.

Audit coverage (review note 6 — rev 1 cited the wrong precedent and tested the wrong half):

- `created_by` stamping on an admin-created department follows **`AuditedModelAdminTests`
  (`tests.py:371`)**, not `RackTemplateAuditTests`, which does not demonstrate that.
- A Department edit produces an UPDATE log entry — proves the new `AUDITLOG_INCLUDE_TRACKING_MODELS`
  entry works.
- **Assigning/changing `VLAN.department` produces an UPDATE entry naming `"department"`** — this is
  the one that proves the plan's actual claim, that bare `VLAN` registration picks the new FK up
  with no settings change. The Department-side test does not prove it.

### New — `inventory/test_ui.py`

Extend the existing parity classes rather than adding new ones:

- `ParityContentTests` (`:1583`) — `/models/department/` renders the department's name **and its
  description**, as an exact cell sequence per that class's stated discipline;
  `/models/department/<pk>/` renders the VLANs inline with the right VLAN IDs and **not** a VLAN
  from another department; `/models/vlan/` renders the Department column and links it to
  `/models/department/<pk>/`
- `/vlans/<pk>/` shows the department in its header, and a department-less VLAN's map page still
  renders — no crash, no stray label. (`_linked_text_for` and the template must both tolerate
  null; the codebase's "a read-only page must never 500 on data the write path allowed" posture.)

**Do not write a "detail page without the VLANs inline" test** (review note 2). It is
unreachable: `detail_permissions` includes `view_vlan`, so `registry_permission_required` 403s the
whole page before any inline renders, and inlines are not conditionally filtered anyway. The
existing `PartialGrantParityAccessTests` (`:1565`) already sweeps every declared codename for
every slug and picks the new entry up for free.

The query-budget, access-control and admin-lockout sweeps need no *new* test methods beyond the
fixture and marker updates above — they iterate `REGISTRY`.

## Docs

- **`CONTEXT.md`** — new **Department** glossary entry after **VLAN**. Its `_Avoid_` line must
  cover both traps: confusing it with the undesigned VLAN *role*, and reading it as scoping
  allocation. Amend the **VLAN** entry, which currently defines a VLAN as ID + addressing only.
- **`ROADMAP.md`** — tick every phase 16 box and move the current-phase line to 17. **Reword the
  "column/grouping on the existing read-only VLAN and address-map views" checkbox before ticking
  it** (review note 9): grouping was deliberately declined by ADR 0021 decision 6, so ticking it
  as written would claim work that was consciously not done. Also reword the resolved "Watch item
  — VLAN metadata" so it is closed by the merged ADR rather than by a plan.
- **`docs/MORE_MUSINGS.md`** — update the §"Department Name" annotation to point at ADR 0021.
- **`DESIGN.md`** — no change. It describes address computation; department does not touch it.

## Verification

```bash
set -a; source .env; set +a
python manage.py makemigrations --check --dry-run   # migration committed, nothing pending
python manage.py test inventory
```

Record the baseline test count **before** touching code, so the registry-guard failures above are
distinguishable from real regressions.

Then against a dev database:

```bash
python manage.py migrate
python manage.py sync_roles      # REQUIRED
```

`sync_roles` needs no code change (it enumerates app models dynamically), but permission rows are
created by `post_migrate` *after* `migrate` finishes — until it is re-run, no role holds
`view_department` and the read-only UI hides Departments from **everyone**, including Admins.
**This must appear in the PR description**, not only here.

Admin smoke test: create Audio / Lighting / Video, assign the eight imported VLANs, filter the VLAN
changelist by department, then try to delete Audio and confirm it is refused and names its VLANs.
Confirm the changelist and filter render department **names**, not `Department object (N)`.

Viewer smoke test (`is_staff=False`): `/models/vlan/` shows the Department column and its cells
link; `/models/department/<pk>/` lists that department's VLANs; `/vlans/<pk>/` shows the department
in its header; a department-less VLAN's map page renders cleanly; `/` shows a Departments tile
under "All records".

Finally: `pre-commit run --all-files` (ruff check/format, mypy over `config inventory manage.py`).

## Review response

| Note | Resolution | Section |
|---|---|---|
| 1 — `Department` has no `__str__` | **Accepted.** Verified: `_linked_text_for` and `model_detail.html` both call `str()`, as do the admin's `list_filter` and FK widget. Added to the model sketch, to the tests, and to the admin smoke test. | Model; Tests; Verification |
| 2 — partial-grant inline test is unreachable | **Accepted.** Verified: `model_detail` (`views.py:1695`) renders every inline unconditionally and `_render_inline` never reads `InlineSpec.permissions` — nothing in `views.py` reads it at all. Test dropped; the field's declarative-only status is now stated so nobody builds on it. Did **not** take the alternative (make inlines conditional and drop `view_vlan` from `detail_permissions`) — that is a generic-view redesign and outside this plan's scope. | Read-only UI; Tests |
| 3 — missing permission dependencies | **Accepted.** Verified `_linked_text_for` gates linking on the target model's own codename, and that `switchportvlanprofile`/`racktemplate` already declare `view_vlan` for exactly this reason. Added `view_department` to VLAN's `list_permissions`, `detail_permissions`, and `vlan_map`'s decorator. | Read-only UI |
| 4 — predicted test failures incomplete | **Accepted in full.** Verified each: `AdminLockoutTests` markers (`:2133`) against the set-equality assert (`:2167`); the exact-cell-sequence assertions at `:1602`; and that an unattached fixture row leaves the inline test with nothing to assert. The fixture must attach the department to `vlan_native`. | Tests |
| 5 — write-nothing sweep is a literal list | **Accepted.** Verified `WritesNothingTests` (`:721`) hand-maintains its routes rather than iterating `REGISTRY` — it would have stayed green while the new pages went unswept. Deriving from `_parity_routes()` preferred, literals plus a pointer accepted as fallback. | Tests |
| 6 — audit verification cites wrong pattern, misses the VLAN side | **Accepted.** Verified `AuditedModelAdminTests` (`tests.py:371`) is the `created_by` precedent, not `RackTemplateAuditTests`. The more important half is right too: only a `VLAN.department` change proves bare `VLAN` registration tracks the new FK. Both tests added. | Tests |
| 7 — admin N+1 rationale wrong for Django 6.0.7 | **Recommendation accepted, reasoning corrected.** `list_select_related = ["department"]` is right. But the finding's premise — that the auto-`select_related()` makes an override redundant — does not hold here: `select_related_descend` reads `if not restricted: return not field.null` (verified against the installed Django 6.0.7), so the bare auto-applied call descends **nothing** for a nullable FK. The N+1 is real; naming the field is what fixes it. The plan now says so, and warns against writing the comment either way round. | Admin |
| 8 — nothing tests "no seeded departments" | **Accepted.** A decision nothing tests is an intention. `Department.objects.count() == 0` after `seed_defaults`. | Tests |
| 9 — counts and one citation | **Accepted, with a trap flagged.** Verified every cited location, and corrected `:1304`→`:1305`. Added the warning that `test_ui.py:414`'s "all eight URL patterns in `urls.py`" must **not** be changed — `urls.py` still has eight `path()` entries, and it sits one clause away from a "sixteen" that must. Also accepted the `ROADMAP.md` "column/grouping" rewording: grouping was declined, so the checkbox cannot be ticked as written. | Tests; Docs |

No finding hit the escalation gate — none contradicts a committed ADR, changes a deliverable, or
attacks a settled decision.

## Out of scope

- `VLAN.role` — designed in ADR 0021, built in phase 21
- Department on `Rack`, `NetworkSwitch`, `NetworkDevice`
- Department-scoped rack allocation (phase 19, still declined)
- Any change to `import_prod_data` or `verify_prod_import` (ADR 0021 rejected it)
- Grouping the index page's VLAN tiles by department
- Making registry inlines permission-conditional (review note 2's alternative)
- Closing #10 for Type profiles and device ports
