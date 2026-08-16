> **Revision 2** — incorporates review notes from `REVIEW-1-PLAN-hostname-ingredients.md`.
> See "Review response" for the mapping. That review was run by a **same-model-family** reviewer
> (a cold Claude subagent), not by `codex` as this project's ritual requires — there was no OpenAI
> budget available. Correlated blind spots are therefore possible in a way the usual chain avoids;
> worth a codex pass over this plan before or alongside implementation.
>
> **Revision 1** turned this file from a decisions record into an implementation plan. The
> decisions it used to hold now live in `docs/adr/0023-hostname-scheme.md`, which also settles
> phase 18's computation and collision rules and corrects three of them against production data.
>
> **Where the old decision numbers went.** Five places cite this file as
> "`PLAN-hostname-ingredients.md` decision *N*" against the superseded numbering
> (`docs/adr/0022-…md`, `docs/plans/PLAN-adr-0022.md` ×2, `inventory/validators.py`,
> `inventory/tests.py`; `PLAN-adr-0022.md:25` refers to "the nine decisions" generally rather than
> to one). They resolve as:
>
> | was | now |
> |---|---|
> | 1 — one ADR covers the whole scheme | ADR 0023, in full |
> | 2 — `Owner` is slug + name | this file, settled decision 4 |
> | 3 — rack-derived owner default in the add forms | this file, settled decision 2 |
> | 4 — `location_slug` nullable-unique, no partial index | this file, settled decision 5 |
> | 5 — `hostname_slug` never auto-filled | ADR 0023, rejected alternatives |
> | 6 — `hostname_slug` is not locked | this file, settled decision 3 |
> | 7 — one shared `validate_dns_label` | shipped in ADR 0022 PR 1; the 63-cap half is ADR 0023 decision 8 |
> | 8 — full read-parity UI | PR 2 below |
> | 9 — no production backfill | ADR 0023 decision 10, with a corrected premise |
>
> The two code-comment citations are refreshed in PR 1; the `.md` ones are historical records and
> are left as written.

# Implement ROADMAP phase 17 — hostname ingredients

## Context

ADR 0023 settles the whole hostname scheme. `ROADMAP.md` phase 17 builds the **fields** and
computes nothing; phase 18 builds the behaviour. ADR 0023 decision 11 draws that seam at fields
versus behaviour, which moves `hostname_purpose` and `hostname_sequence` into this phase from where
the roadmap had them.

Two ingredients already shipped, in ADR 0022 PR 1, and this plan only imports them:
`NetworkDeviceTypePort.hostname_suffix` and `validate_dns_label` (`inventory/validators.py:23`).

Nothing here computes, assembles, validates uniqueness, or renames anything. If a reviewer finds
hostname assembly in this diff, it is in the wrong phase.

## Decisions this plan settles (ADR 0023 left them to the build)

1. **Three PRs**, split so each leaves the suite green and the app coherent: the fields, the
   read-only UI parity, then the importer seeding. The UI parity for a new registry model carries
   the enumerable cost phase 16's `Department` established and is large enough to review on its
   own. PR 2 and PR 3 both depend only on PR 1, and not on each other.

2. **The rack-derived owner default ships in PR 1, not phase 18.** It is a field default, not
   hostname assembly — `NetworkDeviceAddForm.clean()` and `NetworkSwitchAddForm.clean()` fill a
   blank `owner` from `rack.owner`, near the `rack_slot` fill that already does this (ADR 0019).
   Programmatic `objects.create()` gets nothing, so no existing test changes behaviour.

   **This does not work unless the field is in `Meta.fields` first.** `NetworkDeviceAddForm.Meta.
   fields` and `NetworkDeviceChangeForm.Meta.fields` are both explicit lists
   (`inventory/admin.py:434-436`, `:630-632`) that omit the new fields, and
   `ModelForm._post_clean()` calls `construct_instance(…, opts.fields, …)`, which skips any field
   not in that list — so `cleaned_data["owner"] = rack.owner` would be computed and then discarded.
   `NetworkSwitchAddForm` is unaffected: its `Meta` uses `exclude: list[str] = []`
   (`admin.py:659-661`) and picks new fields up automatically. That asymmetry is the trap.

3. **`hostname_slug` does not join the Type lock.** `NetworkDeviceTypeAdmin.get_readonly_fields()`
   (`inventory/admin.py:1180-1189`) and the matching model-level lock list
   `manufacturer`/`model`/`name`/`port_count`/`is_add_in_card`. `hostname_slug` stays out of both:
   ADR 0023 requires a typo'd abbreviation to remain fixable without creating a new named profile,
   and phase 18 stores hostnames, so nothing materialized can drift from it.

4. **`Owner.slug` and `Owner.name` are both required and unique**, `slug` DNS-validated. `__str__`
   returns `"Mike Snow (mps)"` — composite like `NetworkSwitchType`'s rather than bare like
   `Department`'s. No `description`; `name` carries what one would.

5. **No conditional `UniqueConstraint` for `Rack.location_slug`.** This backend reports
   `supports_partial_indexes = False`, and Django 6.0.7's `_create_unique_sql()` returns `None` in
   that case — the migration would emit no SQL and the model would claim a constraint the database
   does not have. `null=True` + `unique=True` is DB-enforced because MySQL permits unlimited NULLs
   in a unique index. `""` is normalised to `None` in both `clean()` and `save()`.

6. **Normalisation happens in `clean()` *and* `save()` for every new field.** `Model.save()` never
   calls `clean()`, so a direct `objects.create(slug="MPS ")` would otherwise persist an unstripped,
   uncased value `validate_dns_label` would have rejected. The pattern `Department.save()`
   (`inventory/models.py:554`) already uses.

7. **`hostname` is untouched by this phase.** It stays `CharField(255)` with no validator and no
   normalisation; ADR 0023 decision 8 shrinks and lowercases it in phase 18.

8. **Rack location slugs are a rule plus exceptions, not an enumeration.** Slugify the rack name;
   consult a small `RACK_LOCATION_SLUG_EXCEPTIONS` constant first; `CONSOLES` maps to `None`. Error
   only when a name is neither in the constant nor slugs to a legal DNS label.

   A hard "unmapped name is a `RuntimeError`" — which rev 1 specified — **would have failed the
   entire existing prod-import suite.** `inventory/test_prod_import.py:78-84` drives the real
   importer against synthetic CSVs whose racks include the invented names `AMPRACK1` and
   `W8LMTEST`, which cannot appear in a production constant. The rule form keeps those working
   (`AMPRACK1` → `amprack1`) while still refusing something like `FOH Drive #3`.

9. **Every imported rack is owned by `mps`.** Of 52 Dante rows, 51 are `MPS` and the one `BEJ` row
   is an unracked console. `bej` is seeded as vocabulary an operator will need, referenced by no
   rack. Stated explicitly because "every rack gets an owner" is otherwise untestable.

## PR 1 — the fields

### Model — `inventory/models.py`

**New `Owner(AuditedModel)`**, placed beside `Department` (`:524`):

- `slug` — `CharField(max_length=63, unique=True, validators=[validate_dns_label])`, with
  `CheckConstraint(condition=~Q(slug=""), name="owner_slug_not_blank")`. Note `condition=`:
  `CheckConstraint.__init__` is keyword-only on Django 6 and `check=` is gone; `Department`'s own
  constraint (`models.py:544-546`) is the local precedent. Stripped **and lowercased** in `clean()`
  and `save()`, so `"MPS "` stores `"mps"` rather than erroring — a departure from
  `Department.name`, which strips but deliberately does not casefold, because an uppercase slug
  would be concatenated verbatim into a hostname.
- `name` — `CharField(max_length=100, unique=True)`,
  `CheckConstraint(condition=~Q(name=""), name="owner_name_not_blank")`, stripped only.
- `Meta.ordering = ["name"]`; `__str__` returns `f"{self.name} ({self.slug})"`.

**`Owner` FKs**, all `ForeignKey(Owner, on_delete=models.PROTECT, null=True, blank=True)`:

- `Rack.owner`, `related_name="racks"`
- `NetworkSwitch.owner`, `related_name="switches"`
- `NetworkDevice.owner`, `related_name="devices"`

`PROTECT` matching `VLAN.department` (`models.py:576`, ADR 0021) — the same "a descriptive label
with rows pointing at it must not vanish silently" case.

**`Rack.location_slug`** — `CharField(max_length=63, null=True, blank=True, unique=True,
validators=[validate_dns_label])`, stripped and lowercased, then `""` → `None` in `clean()` and
`save()` so `"  "` also becomes `None`.

**`hostname_slug`** on `NetworkSwitchType` and `NetworkDeviceType` — `CharField(max_length=63,
blank=True, validators=[validate_dns_label])`, stripped and lowercased, deliberately **not**
unique. Two profiles of one model both carry `ik42` and are matched by hand; a Type's identity is
`(manufacturer, model, name)` (ADR 0010). `help_text` carries the example *and* the trap:
`slugify("IK-42")` gives `ik-42` where the name in use is `ik42`, which is why this is never
auto-filled.

**`hostname_purpose`** on `NetworkSwitch` and `NetworkDevice` — `CharField(max_length=63,
blank=True, validators=[validate_dns_label])`, stripped and lowercased. `help_text` gives
`midhi-01-04` and `sub`, and says a non-numeric component like `01-04` belongs here, not in the
sequence.

**`hostname_sequence`** on `NetworkSwitch` and `NetworkDevice` —
`PositiveIntegerField(null=True, blank=True)`.

### Migration `0016_hostname_ingredients`

`CreateModel` for `Owner` with both check constraints, then ten `AddField`s: `Rack.owner`,
`Rack.location_slug`, `NetworkSwitch.owner`, `NetworkSwitch.hostname_purpose`,
`NetworkSwitch.hostname_sequence`, `NetworkDevice.owner`, `NetworkDevice.hostname_purpose`,
`NetworkDevice.hostname_sequence`, `NetworkSwitchType.hostname_slug`,
`NetworkDeviceType.hostname_slug`. Every field is nullable or blank-able, so no data migration and
no default, and existing rows are untouched.

### Audit — `config/settings.py`

**Not automatic, contrary to what ADR 0023's Consequences first claimed.** `NetworkSwitch` is
registered with `include_fields=["rack", "rack_slot", "created_at"]` (`:289`) and `NetworkDevice`
with `include_fields=["rack", "rack_slot", "host", "created_at"]` (`:292`); a whitelist does not
pick up new fields.

- Add `owner`, `hostname_purpose`, `hostname_sequence` to both `include_fields` lists.
- Add `"inventory.Owner"` to `AUDITLOG_INCLUDE_TRACKING_MODELS` — otherwise `OwnerAdmin`'s
  `show_auditlog_history_link` renders a permanently empty history.
- `Rack` and the two Type models are registered bare and need no change.

### Admin — `inventory/admin.py`

- `OwnerAdmin` beside `DepartmentAdmin` (`:969`): `list_display = ["name", "slug"]`,
  `search_fields = ["name", "slug"]`, `AuditedModelAdminMixin`, `AuditlogHistoryAdminMixin`,
  `show_auditlog_history_link = True`.
- **`owner`, `hostname_purpose` and `hostname_sequence` added to `NetworkDeviceAddForm.Meta.fields`
  and `NetworkDeviceChangeForm.Meta.fields`** (settled decision 2). Without this the fields render
  on neither device form and the owner default silently no-ops.
- `owner` added to `RackAdmin`, `NetworkSwitchAdmin`, `NetworkDeviceAdmin` `list_display` and
  `list_filter`; `location_slug` to `RackAdmin.list_display` and `search_fields`;
  `hostname_purpose`/`hostname_sequence` to both equipment admins' `list_display`; `hostname_slug`
  to both Type admins' `list_display`.
- `list_select_related` covering `owner` on all three — `NetworkDeviceAdmin` has one already
  (`:1200`) and needs extending; `RackAdmin` and `NetworkSwitchAdmin` have none. `VLANAdmin`'s
  comment (`:989-991`) records that this project declares the attribute deliberately rather than
  relying on Django's auto-`select_related`.
- `hostname_slug` **not** added to either Type admin's `get_readonly_fields()` lock list.
- The rack-derived owner default in both add forms' `clean()`. Add-only.

### Docs

- **`CONTEXT.md` gains an `Owner` entry**, beside `Department` (`:19`) and in its shape: a table
  rather than free text because #10 documents what free-text identity fields do; nothing branches
  on it; the importer does not seed it per-equipment. `_Avoid_`: confusing an owner with a
  department (one owns equipment, the other owns a VLAN), and reading `Rack.owner` as inheritance
  rather than a creation-time default.
- **`CONTEXT.md`'s `Rack` entry** (`:23`) gains `location_slug` — optional, unique where set, and
  explicitly *not* a purpose field, since that entry already says a Rack has none. Note there that
  `AVIO` and `SPARE` do carry location names while `CONSOLES` does not, so nobody re-derives
  "virtual racks are location-free" from the three names that entry lists.
- **`ROADMAP.md`** — already corrected on this branch: the phase 17 bullet said `hostname_slug` was
  "prefilled by slugifying the model as a convenience", which ADR 0023 rejects outright.
- **`DESIGN.md`** gains the new fields; no behaviour section changes.
- **Refresh the two code-comment citations** of the superseded decision numbering —
  `inventory/validators.py:26` and `inventory/tests.py:6415` — to point at ADR 0023.

## PR 2 — read-only UI parity

### Registry — `inventory/views.py`

An `"owner"` `ModelSpec` modelled on `"department"` (`:1171`): `list_columns` and `detail_fields`
of Name and Slug, `ordering=("name",)`, and three `InlineSpec`s — Racks, Network Switches, Network
Devices — with `detail_prefetch_related` to match.

**`detail_permissions` must fold in every inline's codename:**
`("inventory.view_owner", "inventory.view_rack", "inventory.view_networkswitch",
"inventory.view_networkdevice")`. `InlineSpec.permissions` is declared (`views.py:1105`) but **read
nowhere** — `_render_inline()` (`:1656-1666`) does no permission check and `model_detail()` renders
every inline unconditionally. Enforcement in this registry is `registry_permission_required`
against the spec's own `detail_permissions`, which is exactly why `"department"` folds its inline's
`view_vlan` in (`:1201`). Without this, a Viewer holding only `view_owner` sees every device in the
estate.

**On the three existing specs**, add `FieldSpec("Owner", "owner", render="relation")` to
`list_columns` *and* `detail_fields`, `location_slug` to `rack`, and extend `list_select_related`.
The codename goes in **`list_permissions`** — the established pattern, since
`networkdevice.list_permissions` already carries `view_networkdevicetype`/`view_rack` for its
relation columns.

**`detail_permissions` gets `view_owner` for `networkswitch` only.** `rack` (`:1286-1290`) and
`networkdevice` (`:1504-1506`) both carry a comment stating their detail codenames are *"minimal on
purpose"* because `model_detail` redirects before rendering, and that requiring the larger set
"would 403 the redirect itself for a user `rack_detail` would otherwise happily serve, and would
report the wrong codename in a 403." Their codename belongs on the shaped views instead.

Also add `hostname_slug` to both Type specs' `list_columns`/`detail_fields`, and
`hostname_purpose`/`hostname_sequence` to `networkswitch`'s. Since Stage C provisions Viewers with
`is_staff=False` (`test_ui.py:418-422`), a field absent from the registry is invisible to a Viewer
full stop — the same argument the plan makes for `location_slug`.

### The `canonical_detail_view` trap — three parts, not one

`Rack` and `NetworkDevice` both set `canonical_detail_view` (`views.py:1282`, `:1496`), so
`model_detail` **redirects** to the hand-written `rack_detail.html` / `device_detail.html` and
their registry `detail_fields` never render.

1. Both templates gain the new rows by hand (`owner`, and `location_slug` on the rack); plus
   `hostname_purpose`/`hostname_sequence` on `device_detail.html`.
2. **Both shaped views gain `inventory.view_owner` in their literal `@permission_required` list**
   (`views.py:573-596`, `:881-904`). Each carries a comment recording that a previous Codex review
   caught them under-declaring, and the rule is that each view declares the full set of codenames
   it actually reads.
3. **Both querysets gain `select_related("owner")`.** `device_detail`'s docstring says the
   registry's hints never apply to this page and that relations must be declared in *that*
   queryset — so `detail_select_related=("owner",)` on those two specs is inert and the pages
   would pay a query per render.

`NetworkSwitch` has no canonical view and needs no template change. On-screen copy names no ADR.

### `RackAddForm`

`RackAddForm.Meta.fields` is an explicit `["name", "slot_count"]` and will **silently drop**
`location_slug` and `owner`. Both added.

### Roles

`sync_roles` must be re-run after migrating: permission rows are created by `post_migrate`, so
until then no role holds `view_owner` and the read-only UI hides Owners from everyone. Editors also
get `add_owner` and `change_owner`.

### The enumerable cost

The UI guards iterate `REGISTRY` slugs, so a new *field* breaks no test but the new `owner` *entry*
carries Department's full parity cost. The prerequisite for most of it is the fixture:
`ParityFixtureMixin.pk_by_slug` (`test_ui.py:424-434`) is a hand-written dict and `_parity_routes()`
indexes it for every registry slug — a missing entry is a `KeyError`, not a readable failure. So:
an `Owner` row created in `setUp` with a value distinctive enough not to be a substring of anything
else on the page (the fixture docstring requires this), its `pk_by_slug` entry, then the
query-budget factory, the lockout markers, the two hand-written entries in the writes-nothing route
list (`test_ui.py:770-793`, not registry-derived), and the partial-grant codenames.

## PR 3 — importer seeding

ADR 0023 decision 10: seed the vocabulary, backfill nothing per-device. There is no join key
between the Dante sheet that carries the components and the addressing sheet the importer reads.

### `inventory/management/commands/import_prod_data.py`

- Two `Owner` rows: `mps` / "MPS" and `bej` / "BEJ". Full names are placeholders an operator edits.
- `Rack.owner = mps` on every imported rack (settled decision 9).
- `RACK_LOCATION_SLUG_EXCEPTIONS`, a module-level constant in the same spirit as the existing
  `PRIMARY_SWITCH_TABLES` (`:118` — rev 1 cited a `SWITCH_PORT_TABLE_MAP`, which does not exist):
  `{"XE300-1": "xe1", "XE300-2": "xe2", "FOH Drive #1": "foh1", "FOH Drive #2": "foh2",
  "CONSOLES": None}`. Everything else slugifies from the rack name. A name that is neither in the
  constant nor slugs to a value `validate_dns_label` accepts raises — but ordinary synthetic names
  like `AMPRACK1` slug cleanly and keep the existing suite green (settled decision 8).

Explicitly **not** seeded: `NetworkDevice.owner`, `NetworkSwitch.owner`, `hostname_purpose`,
`hostname_sequence`, `hostname_slug` on any Type.

### `verify_prod_import.py`

That file's module docstring states `import_prod_data.py` "is imported nowhere in this file, on
purpose" — a check sharing the importer's helper proves nothing. So the verifier **re-derives**
rather than importing the constant: it applies the same slugify rule independently and carries its
own small literal of the four exceptions plus `CONSOLES`, exactly as it already duplicates
`ADDRESSING_CSV_NAME` and friends.

Checks added: every rack has the expected `location_slug` (`CONSOLES` null); both `Owner` rows
exist; every rack's `owner` is `mps`. Plus a **negative** check that no `NetworkDevice` or
`NetworkSwitch` carries an `owner`, `hostname_purpose` or `hostname_sequence` — the seeding boundary
is what is most likely to drift, and asserting its absence catches a well-meaning future backfill.

## Tests

**PR 1**

- `Owner.slug` normalises (`"MPS "` → `mps`); `"-mps"` and `"mps-"` are rejected; blank slug and
  blank name each hit their check constraint; `__str__` is `"MPS (mps)"`.
- Deleting an `Owner` with a rack, switch or device attached raises `ProtectedError`.
- `Rack.location_slug`: `""` and `"   "` store `None`; two racks may both be `None`; two may not
  share a non-null slug, asserted as an **`IntegrityError`** at the database level, not just a
  `ValidationError` — settled decision 5 exists precisely because a partial index would have
  silently emitted no SQL.
- `hostname_slug`: normalises; not unique across two profiles of one model; **editable after
  instances exist**, both at the model level and through `get_readonly_fields()`, while
  `manufacturer` stays locked.
- `hostname_purpose` normalises and validates. `hostname_sequence` accepts `None` and rejects a
  negative **via `full_clean()`** — stated at that layer deliberately, since settled decision 6
  exists because `save()` bypasses `clean()`.
- **Form-field presence**, the guard rev 1 lacked: `owner`, `hostname_purpose` and
  `hostname_sequence` each appear in `NetworkDeviceAdmin.get_form(request, obj=None).base_fields`
  *and* `…(request, obj=device).base_fields`, and in the switch equivalents. The `base_fields`
  idiom is already used at `tests.py:2204`/`3298`. Without this the owner-default test below passes
  vacuously on a field the form never had.
- The rack-derived owner default: both add forms fill a blank `owner` from `rack.owner`; an
  explicit choice is not overridden; the **change** form does not derive; `objects.create()` does
  not derive; a rack with no owner leaves it blank.
- **Audit**: changing a device's `owner` produces an UPDATE `LogEntry` naming `owner`, in the shape
  of `DepartmentAuditTests` (`tests.py:4865-4878`), whose docstring exists to prove exactly this.
- Nothing computes: creating a device with every component set leaves `hostname` exactly as
  submitted, blank included. The guard against phase 18 leaking backwards.

**PR 2**

- The registry-exhaustive guards pass with the new `owner` slug.
- A Viewer with `view_owner` sees the Owners list and detail; without it, both 403.
- A Viewer holding `view_owner` but **not** `view_networkdevice` gets a 403 on the Owner detail
  page — the shape `PartialGrantParityAccessTests` (`test_ui.py:2059-2075`) enforces registry-wide.
  Not "the inline is omitted": that machinery does not exist.
- `rack_detail.html` renders `location_slug` and `owner`; `device_detail.html` renders `owner`,
  `hostname_purpose` and `hostname_sequence` — asserted through the canonical redirect, since the
  registry detail view never renders for these two models.
- A Viewer without `view_owner` is 403'd on both shaped views.
- `RackAddForm` exposes `location_slug` and `owner`.

**PR 3**

- **The existing synthetic suite, `inventory/test_prod_import.py`, passes unchanged** — an explicit
  acceptance criterion, not an assumed side effect. It is where settled decision 8's rule form gets
  proven, since `AMPRACK1`/`W8LMTEST` are exactly the names a hard constant would have broken.
- After a rebuild: both `Owner` rows exist; every rack's owner is `mps`; every rack's
  `location_slug` matches the rule; `CONSOLES` is null.
- No device or switch has an `owner`, `hostname_purpose` or `hostname_sequence`.
- A rack name that neither maps nor slugs legally raises rather than importing a null slug.
- Each new verifier check fails when its target is perturbed.

## Verification

```bash
set -a; source .env; set +a
python manage.py test inventory
```

Record the baseline count before touching code. PR 3 additionally rebuilds from the CSVs and runs
`verify_prod_import.py`.

After migrating, re-run `sync_roles` — without it the read-only UI hides Owners from every role,
which looks like a permissions bug and is not.

## Review response

| Note | Resolution | Section |
|---|---|---|
| 1 (P0) | Accepted — verified `test_prod_import.py:78-84` invents `AMPRACK1`/`W8LMTEST`, so rev 1's hard `RuntimeError` would have failed every `call_command("import_prod_data")` test. Adopted the reviewer's option (a): slugify-with-exceptions. This is the sharpest catch in the review; the plan claimed each PR leaves the suite green and PR 3 did not. | Settled decision 8; PR 3 |
| 2 (P0) | Accepted — verified both `NetworkDevice` form `Meta.fields` lists (`admin.py:434`, `:630`) omit the new fields, and that `construct_instance` skips anything not listed, so the owner default would have been computed and discarded. `NetworkSwitchAddForm` uses `exclude=[]` and is unaffected. Added the fields to both lists and a `base_fields` test. | Settled decision 2; PR 1 Admin; Tests |
| 3 (P1) | Accepted — verified the "minimal on purpose" comments at `views.py:1286-1290` and `:1504-1506` forbid exactly what rev 1 proposed. `view_owner` moves to `list_permissions` (with an Owner column actually added), and to `detail_permissions` for `networkswitch` only. | PR 2 Registry |
| 4 (P1) | Accepted — verified `InlineSpec.permissions` is read nowhere in `views.py`; rev 1's test asserted behaviour the machinery does not have. All four codenames fold into the Owner spec's `detail_permissions`, and the test becomes the 403 shape. | PR 2 Registry; Tests |
| 5 (P1) | Accepted. The trap was half-worked: the shaped views also need `view_owner` in their literal permission lists and `select_related("owner")` in their own querysets, since the registry hints never reach them. | PR 2 "canonical_detail_view trap" |
| 6 (P1) | Accepted — verified `settings.py:289`/`:292` use `include_fields` whitelists, so the new fields would be silently untracked, and `inventory.Owner` was registered nowhere while `OwnerAdmin` advertised a history link. **This corrects ADR 0023's Consequences**, which claimed the existing registrations covered them; the ADR is amended. | PR 1 Audit; ADR 0023 Consequences; Tests |
| 7 (P1) | Accepted. Rev 1 put five fields on admin `list_display` and only two in the registry, while claiming full read-parity — and Viewers are `is_staff=False`, so admin-only is invisible. `hostname_slug`, `hostname_purpose` and `hostname_sequence` added to the registry. | PR 2 Registry |
| 8 (P2) | Accepted — `SWITCH_PORT_TABLE_MAP` does not exist anywhere; the real symbol is `PRIMARY_SWITCH_TABLES` (`import_prod_data.py:118`). Fabricated citation, fixed here and in ADR 0023. | PR 3; ADR 0023 decision 10 |
| 9 (P2) | Accepted — `verify_prod_import.py`'s docstring forbids importing the importer, so checking against its constant would be a tautology. The verifier re-derives the rule and carries its own literal of the exceptions. | PR 3 verifier |
| 10 (P2) | Accepted, both halves. The owner-per-rack rule is now stated (`mps`; `bej` is vocabulary only). The reviewer also caught that ADR 0023 overclaimed `bej-dm3d` as reproducible — blank `CONSOLES` fixes the *location* component, but the first recompute yields `mps-dm3d` until an operator sets the device's owner. ADR amended. | Settled decision 9; ADR 0023 rejected alternatives |
| 11 (P2) | Accepted — 21 racks are created but only 14 appear in the Dante sheet, so a full enumeration would mean inventing five evidence-free slugs. Resolved by the same rule-plus-exceptions shape as note 1. | Settled decision 8; PR 3 |
| 12 (P2) | Accepted — `ROADMAP.md:271` still said `hostname_slug` is "prefilled by slugifying the model", which ADR 0023 rejects in bold. Corrected on this branch. | PR 1 Docs; ROADMAP.md |
| 13 (P2) | Accepted. The cost list named four items and omitted the fixture the other three depend on; `pk_by_slug` is hand-written and a missing entry is a `KeyError`. Named the fixture row, the `pk_by_slug` entry and the hand-written route list. | PR 2 "enumerable cost" |
| 14 (P3) | Accepted — `Rack` declares no FKs at all, so the justifying sentence was simply wrong. Now cites `VLAN.department` (ADR 0021). | PR 1 Model |
| 15 (P3) | Accepted. `list_select_related` covering `owner` on all three admins; `VLANAdmin`'s comment records that this project declares it deliberately. | PR 1 Admin |
| 16 (P3) | Accepted — `CheckConstraint.__init__` is keyword-only on Django 6 and `check=` is gone. Both constraints now written `condition=…, name=…`. | PR 1 Model |
| 17 (P3) | Accepted, both halves. The header census is five, not six (`PLAN-adr-0022.md:25` is a general reference, not a numbered citation). The `hostname_sequence` negative test now names `full_clean()` as its layer. | Header; Tests |
