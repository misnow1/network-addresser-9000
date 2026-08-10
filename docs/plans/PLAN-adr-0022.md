> **Revision 2** — incorporates review notes from `REVIEW-1-PLAN-adr-0022.md`.
> See "Review response" for the mapping.

# Implement ADR 0022 — Add-in cards and operator-set ports

## Context

`docs/adr/0022-add-in-cards-and-operator-set-ports.md` sorts dependent hardware into three tiers by
*optional* × *removable*, closes issue #42, and supersedes ADR 0018.

**Builds:** `NetworkDeviceTypePort.address_source` (a second static address on one VLAN, operator-set
— issue #42); `NetworkDeviceTypePort.hostname_suffix` and a derived read-only port hostname;
`NetworkDevice.host` plus `NetworkDeviceType.is_add_in_card` with a fit/pull flow.

**Deletes:** every part of ADR 0018. `companion_type`, the materialization machinery, the paired-move
machinery, the tether UI, and the companion fields on `NetworkDeviceAddForm`. The Yamaha Device
Control interfaces become ports on their consoles.

**Does not build:** hostname assembly, collision handling, uniqueness or length validation — those
are phase 18's ADR. This plan ships one *ingredient* (`hostname_suffix`) because folding the Device
Control into its console would otherwise silently lose the name `dm7c-1-device-control` that
production carries today. No `Owner`, no `location_slug`, no `hostname_slug`; `ROADMAP.md` phase 17
resumes after this with the nine decisions already recorded in `PLAN-hostname-ingredients.md`.

**Also does not build:** `VLAN.role` (phase 21), switch-side equivalents of anything here, nested
cards, or a `fits_host_types` compatibility matrix (ADR 0022 decision 5 chose a boolean instead).

**What this plan does not change** (rev 2 — the earlier wording overstated this, review note 15).
No address *value* moves except the two Device Control addresses changing which row owns them; no
address *arithmetic* changes; no suggester, allocator, `slot_span` or `occupied_rack_slot_ranges()`
change; no database constraint is added, dropped or altered. If a diff reaches `suggestions.py`,
`_suggest_*`, `occupied_rack_slot_ranges()` or `unique_device_rack_slot`, something has gone wrong.

It *does* deliberately change three validation behaviours, named here so they are not mistaken for
regressions: `_check_static_materialization_possible()` stops refusing a second port on a VLAN when
that port is operator-addressed; `_validate_device_type_port_profile()` starts refusing
`OPERATOR` combined with `slot_offset > 0`; and `verify_prod_import.py`'s cross-VLAN alignment check
stops applying to operator-addressed ports (review note 6 — without this a correct import *fails*
verification, because a DM7C at slot 5 carries its Device Control at `.4`).

## Decisions this plan settles (ADR 0022 left them open)

1. **Three PRs, not two.** The grill settled on two, but `companion` appears 290 times in
   `models.py` and 420 times in `tests.py` — bundling additive work into that deletion would produce
   a diff nobody can review. PR 1 is purely additive and leaves ADR 0018 running untouched; PR 2 is
   the deletion; PR 3 is the cards. Each leaves the suite green and the app coherent.
2. **Operator addresses reach `_materialize_ports()` through a transient property**, exactly
   mirroring ADR 0013's `port_addressing` (`models.py:3113-3300`): a class-level `_operator_addresses`
   default plus a property, so `objects.create(operator_addresses={...})` works and nothing is
   stored. Not a new mechanism — the same one, for the same reason.
3. **`address_source=OPERATOR` and `slot_offset > 0` are mutually exclusive**, rejected in
   `_validate_device_type_port_profile()`. An address the operator sets cannot also be one the
   hardware derives; permitting the pair would leave `_derive_offset_siblings()` overwriting a value
   the operator just typed.
4. **`validate_dns_label` ships here**, in `validators.py` beside `validate_ipv4_cidr`, to
   `PLAN-hostname-ingredients.md` decision 7's spec exactly — `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`, max
   63, stripped and lowercased in `clean()` and `save()`. Phase 17 reuses it rather than writing a
   second one. Suffixes are stored bare (`engine`, not `-engine`).
5. **The derived port hostname is a property returning `None`, not `""`,** when the device has no
   hostname or the suffix is blank. Templates test truthiness; an empty string that renders as a
   stray `-` is the failure mode worth designing out.
6. **PR 2 deletes `DeviceCompanionMigrationTests` rather than adapting it.** It asserts that
   `0011_device_companions.link_production_companions()` backfilled a relationship that no longer
   exists. Historical migrations stay on disk and unedited; the test that guards their forward
   meaning goes.
7. **PR 3's importer links the DMI-DANTE to the console whose row carried the marker.** The
   detection at `import_prod_data.py:862-867` already has that row in hand (`SD12-96-1-Control`), so
   the link is read from the data rather than guessed. *(Rev 2, review note 10: the earlier claim
   that "the existing `CommandError` still fires first" on multiple markers was **false** —
   `dmi_dante_racks` is a set of rack names, so two marker-bearing consoles in `CONSOLES` collapse to
   one entry and pass the guard. The detection now collects the marker **rows**, and requires exactly
   one.)*
8. **The migration in PR 2 moves the existing port row; it does not copy and delete** (rev 2, review
   note 1). `unique_device_port_vlan_address_value` is a `UniqueConstraint(vlan, address)`
   (`models.py:4373`), so a host port created while the companion's port still holds that address
   violates it. Re-pointing the row is also the only version that preserves the port's audit history.
9. **The migration refuses companion shapes it was not written for** (rev 2, review note 4). ADR 0018
   permitted arbitrary companion types, DHCP companions, multi-port companions and names other than
   `-device-control`. Collapsing all of those into one Device Control port would be a guess. The
   migration defines the admissible shape and raises on anything else rather than silently deleting
   inventory.

---

## PR 1 — Operator-set ports and port labels

Purely additive. Nothing existing changes behaviour; ADR 0018 still runs at the end of this PR.

### `inventory/validators.py`

`validate_dns_label(value)` per settled decision 4.

### Model — `inventory/models.py`

Beside the existing `PortMode` / `PortAddressing` enums:

```python
class PortAddressSource(models.TextChoices):
    SLOT = "slot", "From the device's rack slot"
    OPERATOR = "operator", "Set by the operator"
```

`NetworkDeviceTypePort` gains two fields:

```python
address_source = models.CharField(
    max_length=10, choices=PortAddressSource.choices, default=PortAddressSource.SLOT,
    help_text=(
        "Where this port's address comes from. Leave at the default unless this port is a "
        "second address on a VLAN this device already uses — a Yamaha console's Device "
        "Control interface — which the system cannot compute and the operator must supply."
    ),
)
hostname_suffix = models.CharField(
    max_length=63, blank=True, validators=[validate_dns_label],
    help_text=(
        'Names this port in address lists as "<device hostname>-<suffix>", e.g. "engine" '
        'for a console\'s audio engine. Store it bare, with no leading dash. Leave blank '
        "for a port that shares the device's own name."
    ),
)
```

Behaviour changes, each small and named:

- **`_check_static_materialization_possible()` (`:3379-3390`)** — build `by_vlan_offset` from
  `SLOT`-sourced ports only. `OPERATOR` ports are exempt from the one-per-`(vlan, offset)` refusal;
  that exemption *is* the fix for #42.
- **`_materialize_ports()` (`:3428`)** — an `OPERATOR` port on a device that materializes statically
  takes its address from `self.operator_addresses[type_port.description]`; a missing key is a
  `ValidationError` naming the port. On an unracked/DHCP device it materializes DHCP like any other
  port and the mapping is ignored.
- **`_check_static_materialization_possible()`** also gains the pre-flight for the above, so the
  `objects.create()` path (which never calls `clean()`) fails before writing anything.
- **`_validate_device_type_port_profile()` (`:253-286`)** — reject `OPERATOR` with `slot_offset > 0`
  (settled decision 3). Its existing rule that a VLAN with a non-zero-offset port must also carry an
  offset-0 port on that VLAN is unchanged and still counts `OPERATOR` ports, since the Device
  Control's VLAN genuinely does have one.
- **`NetworkDeviceTypePort.clean()` / `save()` / `delete()` (`:2960-2994`)** — the profile lock
  gains an exemption so a write touching only `hostname_suffix` is allowed on a type with instances
  (ADR 0022 decision 4). Compare against the persisted row; every other field still refuses.
- **`NetworkDevicePort.hostname`** — a property, `f"{self.device.hostname}-{suffix}"` where both
  parts are non-empty, else `None`. Read-only, stored nowhere, per settled decision 5.
  **Casing is not normalised** (review note 12): production stores `DM7C-1`, so the property yields
  `DM7C-1-device-control` where the sheet spells it `dm7c-1-device-control`. The suffix is
  lowercased; the device half is whatever the operator typed. Phase 18 owns hostname casing, and no
  test or verifier assertion in this plan compares a derived port hostname case-sensitively.
- **`NetworkDevice._operator_addresses` / `.operator_addresses`** — the transient property from
  settled decision 2.
- **`NetworkDevicePort._locked_fields()` (`:4534`)** — `address` on an `OPERATOR` port must stay
  editable. It already is (the lock only fires for `slot_offset > 0`), but this needs a test, because
  it is the exact property that distinguishes #42's shape from ADR 0017's.

### Migration `0013_operator_set_ports`

`AddField` ×2. No data migration — `SLOT` and `""` are the defaults and reproduce today's behaviour
for every existing row.

### Admin — `inventory/admin.py`

- `NetworkDeviceTypePortInline` (`:718-737`) surfaces both new fields, **and its
  `has_change_permission()` is relaxed** (review note 9). It currently returns `False` outright once
  the type has instances (`:729-732`), which would make the model-level `hostname_suffix` exemption
  unreachable through the UI. It must permit *change* while rendering every other field read-only;
  add and delete stay refused.
- `NetworkDeviceAddForm.__init__` (`:374`) adds one `GenericIPAddressField` per `OPERATOR` type port
  on the chosen type, labelled from the port's `description`. `clean()` assembles them into
  `self.instance.operator_addresses`, beside the existing `port_addressing` assignment at `:508`.
  The fields are creation-only, like every other field on that form.
- The change form never offers them — the ports exist by then, and their addresses are edited on the
  port rows.

### Read-only UI

The device detail template renders a port's derived hostname where it has one.

**There is no `REGISTRY["networkdevicetypeport"]` entry** (review note 13) — type ports surface only
as an inline of `networkdevicetype` (`views.py:1394-1420`), so both new fields are added to *that*
inline's `FieldSpec` list, not to a top-level entry. Rendering `NetworkDevicePort.hostname` on the
device detail page additionally requires `source_type_port` in that view's `prefetch_related` (or it
is an N+1 across every port) and `inventory.view_networkdevicetypeport` in the page's permission set
(or it reads a model the viewer has not been granted).

---

## PR 2 — Consoles absorb their Device Control; ADR 0018 is deleted

The large one. It is a deletion plus an importer rewrite, and it changes which row owns two
production addresses.

### Model

**Removed** (rev 2 — expanded per review note 2; the earlier list was incomplete and PR 2 would not
have been coherent):

- `NetworkDeviceType.companion_type`, `_validate_companion_type()`, and `companion_type` from
  `NetworkDeviceType`'s locked-field snapshot (`:2762-2821`).
- `NetworkDevice.host` (the `OneToOneField` — see the note below), `_materialize_companion()`,
  `_companion_rack_slot`, `_companion_hostname`, `_check_companion_creation_possible()`,
  `_check_companion_type_compatibility()`, `_plan_companion_move()`, `_park_companion_if_colliding()`,
  `_finish_companion_move()`, `_host_managed_move`, `_companion_pair_pks()` and its use in
  `_check_rack_slot_not_occupied()`, the companion clauses in `_locked_fields()`, and the companion
  branches in `delete()` / `NetworkDeviceQuerySet.delete()`.
- **The companion-only `validate_unique()` and `validate_constraints()` overrides**
  (`models.py:3530-3618`).

**Kept and unchanged:** the `_lock_type_rows()` helper, which predates ADR 0018 and has other callers.

**Note on `host`:** PR 2 drops the `OneToOneField` and PR 3 adds a `ForeignKey` of the same name.
Django supports that across two migrations; the review confirms the drop/add itself is not the
hazard — *live references* are, which is why the inventory above and the admin/UI/registry inventory
below must be complete in PR 2. Doing it as drop-then-add keeps each PR's migration honest about
what that PR means, and the rows it held are deleted by PR 2 anyway.

### Migration `0014_retire_companions`

The one genuinely dangerous step in this plan. Order matters and is now specified exactly (review
notes 1, 3, 4):

1. **Guard.** For every `NetworkDevice` with a `host`, assert the admissible shape: it is linked, its
   type has **exactly one** type port, that port is static (not DHCP) and its instance port carries an
   address, and the host's type has a port on the same VLAN. Anything else — a multi-port companion, a
   DHCP companion, a companion whose type has no live instance — raises `RuntimeError` naming the row.
   **Never delete a companion shape this migration was not written for** (settled decision 9).
2. **Create the type port on each affected host type**, not just an instance port (review note 3):
   one `NetworkDeviceTypePort` with `address_source=OPERATOR`, `slot_offset=0`, the companion's VLAN,
   `description="Device Control"`, `hostname_suffix="device-control"`, and the next free `ordinal`;
   then **increment that type's `port_count`**. Without this the host type fails
   `_validate_device_type_port_profile()`'s `count != port_count` check the next time a device of that
   type is created.
3. **Move the instance port row** (settled decision 8): update its `device`, `description`, `ordinal`
   and `source_type_port` in one write. Do **not** create a new port and delete the old one —
   `unique_device_port_vlan_address_value` forbids both existing at once, and moving preserves the
   port's audit history.
4. **Clear `companion_type` on every host type** before deleting the now-portless companion devices
   and their types (review note 3) — the FK is `PROTECT`, so deletion is refused while it is set.
5. `RemoveField` ×2 (`companion_type`, `host`).

Irreversible by design — say so in the docstring, and state that the production database is rebuilt
from the CSVs (ADR 0022, "What this does to production data").

### Importer — `inventory/management/commands/import_prod_data.py`

- `DeviceTypeSpec` drops `companion_key`; its port tuples grow the two new fields.
- `dm7c` and `dm3` each gain a fourth port: `("Device Control", FN_DANTE_PRIMARY, offset 0,
  OPERATOR, "device-control")`.
- **`sd12`'s existing `Engine` port gains `hostname_suffix="engine"`** (review note 12) — it is half
  the stated reason `hostname_suffix` exists, and the rev-1 plan never assigned it.
- `dm7c_devctrl` and `dm3_devctrl` specs are **deleted**, as are their **`DESCRIPTION_TO_DEVICE_KEY`**
  entries (`:337`, `:346`, `:349`) — the rev-1 plan called this mapping `DEVICE_TYPE_BY_DESCRIPTION`,
  which does not exist (review note 14).
- **`_classify_companion_pairs()`** (`:897` — *not* `_pair_device_control_rows()`, review note 14) is
  rewritten: it still matches a `<host>-device-control` row to its host by hostname stem within one
  rack, and still errors loudly on zero or several matches, but instead of emitting a merged companion
  entry it feeds the row's address into the host's `operator_addresses`. The keyed-on-hostname
  reasoning in its docstring stays true and stays.
- **`CONSOLES` slots 4 and 16 are released.** Nothing else moves — every other device keeps its slot,
  so no address anywhere else changes.

### `verify_prod_import.py` — three changes, all required (review notes 5, 6)

The rev-1 plan understated this; a correct import currently **fails** verification.

- **`_check_cross_vlan_alignment()` (`:1091-1110`) must exclude `address_source=OPERATOR` ports**,
  resolved through `source_type_port`. It requires every static port on a device to share one host
  offset, and a DM7C at slot 5 carrying `.4` deliberately breaks that. This is a verification *rule*
  change, not a data fix, and it needs a test covering both consoles.
- **`_device_address()` must address ports by description or `source_type_port`, not by
  `(vlan, offset)`** (`:550`, `:729-730`). Dante Primary and Device Control now share both values, so
  the existing selector is ambiguous and would silently compare the wrong port.
- **The companion pre-pass consumes both CSV rows but locates one host device**, and the expectations
  become: `CONSOLES` slots 4 and 16 **empty**, the exact expected device-slot set, and both VLAN-201
  addresses asserted independently. Negative tests: corrupt only the Device Control address, and add
  an unexpected static port — both must fail verification.

### Admin

`NetworkDeviceAddForm` loses `companion_rack_slot` / `companion_hostname` and their validation;
**`NetworkDeviceChangeForm`** loses its companion handling; `admin.py:1162`'s comment about
creation-time-only companion inputs loses its companion half; **`delete_selected_devices`**
(`:1113`) loses its companion branch; `NetworkDeviceAdmin` loses its `host` display and read-only
handling; `NetworkDeviceTypeAdmin` loses `companion_type`.

### Read-only UI

`device_detail.html` and `rack_detail.html` lose the tether encoding, **and so do `TetherInfo`
(`views.py:238`), `_tether_for()` (`:395`), `Occupant.tether` and the companion query shaping around
them, plus the tether CSS and any UI fixtures that build a pair.** The registry loses its
**`FieldSpec("Companion type", …)` entries (`views.py:1394`, `:1401`), its
`list_select_related`/`detail_select_related` `companion_type` entries (`:1419-1420`), its
`FieldSpec("Host", …)` entries (`:1440`, `:1449`)**, and `host` from the device querysets'
`select_related` (`:624`, `:914`).

`UnrackedCompanionTetherTests`, the tether assertions in `ElevationEncodingTests`, `DeviceCompanionTests`,
`DeviceCompanionMigrationTests` **and the companion tests at `tests.py:6407-6597`** are deleted —
**not** rewritten against PR 3's cards, which have no tether: a card owns its own slot and appears in
the elevation in its own right.

### Docs

`CONTEXT.md`'s **Device Companion** entry is deleted **and replaced by an Add-in Card entry** (review
note 16 — deletion alone leaves the concept undocumented). **`DESIGN.md:114-125` loses its
`companion_type` and cascading-`OneToOneField` bullets**, which would otherwise keep describing a
model that no longer exists. ADR 0018 gains a superseded banner. ADR 0017 gains an amended banner
naming its scope-boundary section. ADR 0013 gains an amended banner naming its one-address-per-VLAN
rule.

---

## PR 3 — Add-in cards

### Model

```python
# NetworkDeviceType
is_add_in_card = models.BooleanField(
    default=False,
    help_text=(
        "This type's instances are cards fitted inside another device and routinely moved "
        "between hosts — a DMI-DANTE, an X-Dante. Leave off for ordinary equipment."
    ),
)

# NetworkDevice
host = models.ForeignKey(
    "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="installed_cards",
)
```

**`is_add_in_card` joins `NetworkDeviceType`'s locked-field snapshot** (`:2762-2821`, review note 11)
and its admin read-only set. Flipping it after instances exist would either strand already-fitted
devices or retroactively offer ordinary equipment to the fit picker.

ADR 0022 decision 6's three edges are enforced **in `save()` as well as `clean()`** (review note 7) —
`objects.create()` never calls `clean()`, and this module enforces every other invariant on the save
path:

- a device with a `host` must have `is_add_in_card` set;
- a `host` must not itself be an add-in card (no nesting);
- `host` may not be `self` — asserted explicitly, which the rev-1 plan omitted.

Cross-rack is explicitly *not* checked, and a card with no host is explicitly legal — both need a
test asserting they are **allowed**, so a later reader does not add the "missing" validation.

`host` is **not** in `_locked_fields()`: changing it is what fitting and pulling *are*. `rack` and
`rack_slot` are likewise untouched by fitting — a pulled card keeps both, holding its address in the
pool, which is ADR 0022's central claim and wants its own test.

### Migration `0015_add_in_cards`

`AddField` ×2. No data migration.

### `config/settings.py`

Add `host` to `NetworkDevice`'s `AUDITLOG_INCLUDE_TRACKING_MODELS` entry (`:282`), which currently
tracks only `rack`, `rack_slot`, `created_at` (review note 17). Without it, fitting and pulling — the
two operations this PR exists for — leave no audit trail, against ADR 0004.

### Admin

- `NetworkDeviceTypeAdmin`: `is_add_in_card` on the form, in `list_filter`, and read-only once
  instances exist.
- `NetworkDeviceAdmin`: a host column in `list_display`, `host` in `list_select_related`, and
  **`("host", EmptyFieldListFilter)`** for fitted/unfitted — a plain `host` filter lists every
  individual host (review note 8).
- **A real "Fit a card" view, not a deep link to the add form** (review note 8). A deep link cannot
  fit an *existing hostless card* without creating a second row, and `host` is excluded from both
  current forms. The view takes a host, offers two mutually exclusive paths — *choose an existing
  hostless card* or *create a new one* — validates the host and the chosen type server-side (not just
  in the queryset), mutates on POST only, checks change permission on both rows, and runs in one
  transaction. Its test must assert that the existing-card path **preserves the primary key**.
- A **"Pull"** action clearing `host` and leaving `rack`, `rack_slot` and every address alone.

### Read-only UI

`device_detail.html` grows a "Cards fitted" list on a host and a "Fitted to …" line on a card.
`REGISTRY`'s `networkdevicetype` entry gains `is_add_in_card`; `networkdevice` gains `host` (a
`FieldSpec` with `render="relation"`, plus `select_related`). `device_detail.html` is a shaped
template (`canonical_detail_view` redirects), so the registry alone will not surface either.

### Importer

`dmi_dante` is marked `is_add_in_card=True`, and the card entry created at `:893` is linked to the
console whose row carried the marker. Per settled decision 7 as revised, the detection at `:862-867`
**collects the marker rows, not just their rack names**, and requires **exactly one** — two
marker-bearing consoles in `CONSOLES` currently collapse into a single set entry and pass the guard
undetected. `verify_prod_import.py` gains the link as an expectation, with a test that catches a card
linked to the wrong console. **No address changes** — the card stays at `CONSOLES` slot 17 with
`10.201.6.17` / `10.202.6.17`, and **#41 stays open**.

---

## Tests

`inventory/tests.py` unless noted.

**PR 1** — an `OPERATOR` port materializes from `operator_addresses` and a missing key is refused by
name; two ports on one VLAN are accepted when one is `OPERATOR` and still refused when both are
`SLOT`; an `OPERATOR` port's address stays editable after creation (the test that distinguishes #42
from ADR 0017); `OPERATOR` + `slot_offset > 0` is rejected at the type-port profile check; an
unracked device materializes the `OPERATOR` port DHCP; `hostname_suffix` is editable on a type with
instances while every other field on the same row is still refused — **at the model layer and through
the admin inline**, the latter proving review note 9's permission relaxation works; the derived port
hostname is `None` when the device is unnamed and when the suffix is blank; `validate_dns_label`
accepts and rejects per its spec, and a suffix is lowercased and stripped on save. `test_ui.py`: the
derived hostname renders, the device-detail query budget still holds with `source_type_port`
prefetched, and the `networkdevicetype` inline shows both new fields.

**PR 2** — the four deleted test classes plus `tests.py:6407-6597` go (settled decision 6); a DM7C
materializes four ports with the Device Control at its supplied address; **the migration moves the
port row rather than duplicating it, and the host type gains a type port and a bumped `port_count`**;
the migration **raises on each inadmissible companion shape** (multi-port, DHCP, unlinked) and
succeeds on a second valid pair beyond the two production ones; nothing else in `CONSOLES` moves slot.
`test_prod_import.py`: the four CSV rows reproduce exactly, `CONSOLES` has two fewer devices, slots 4
and 16 are free, both VLAN-201 addresses are asserted independently, corrupting only the Device
Control address fails verification, and every other rack is byte-identical to before.

**PR 3** — a card is fitted to an existing host and pulled, keeping its rack, slot and addresses; a
pulled card is re-fitted to a *different* host as the same row; deleting a host leaves its cards
racked, addressed and hostless; a non-card type cannot be given a host, a card cannot host a card,
and a card cannot host itself — **each asserted through `objects.create()` as well as `full_clean()`**
(review note 7); a card may sit in a different rack from its host and a hostless card is legal (both
asserted as *allowed*); `is_add_in_card` cannot be changed once instances exist; the fit view's
existing-card path preserves the primary key and its create path does not; fit, pull and host
deletion each leave an audit entry. `test_ui.py`: the cards-fitted panel, the fitted-to line, and the
registry additions.

## Verification

Per PR, in a worktree:

```bash
set -a; source .env; set +a
python manage.py test inventory
python manage.py makemigrations --check --dry-run
```

PR 2 and PR 3 additionally rebuild from the CSVs and run `verify_prod_import.py`. Per review notes 5
and 6 that verifier is **not** sufficient as it stands — the three changes listed under PR 2 are what
make it actually prove the claim, and the negative tests are what stop it passing vacuously.

## Review response

| Note | Resolution | Section |
|---|---|---|
| 1 (P0) | Accepted — verified `unique_device_port_vlan_address_value` at `models.py:4373`. The migration now **moves** the port row; settled decision 8 records why. | Settled decision 8; PR 2 migration step 3 |
| 2 (P0) | Accepted — spot-verified `validate_unique`/`validate_constraints` (`:3530`, `:3564`), `TetherInfo` (`views.py:238`), `_tether_for` (`:395`), `delete_selected_devices` (`admin.py:1113`), the registry `FieldSpec`s (`:1394-1449`). Deletion inventory rewritten across model, admin, UI and tests. | PR 2 "Removed", "Admin", "Read-only UI" |
| 3 (P0) | Accepted. The migration now creates a type port, bumps `port_count`, repoints `source_type_port`, and clears `companion_type` before deleting types held by a `PROTECT` FK. | PR 2 migration steps 2, 4 |
| 4 (P1) | Accepted. Admissible shape defined; `RuntimeError` on anything else; tests for a second valid pair and each unsupported shape. Settled decision 9 records the rule. | Settled decision 9; PR 2 migration step 1; Tests |
| 5 (P1) | Accepted — `_device_address()` selects on `(vlan, offset)` (`:550`, `:729`), which two same-VLAN ports make ambiguous. Verifier changes promoted to their own section with negative tests. | PR 2 "`verify_prod_import.py`" |
| 6 (P1) | Accepted — verified `_check_cross_vlan_alignment()` (`:1091-1110`) subtracts only `slot_offset`, so a DM7C's `.4` on a slot-5 device fails. This is the sharpest catch in the review: a correct import would have failed verification. Named in Context as a deliberate rule change. | Context; PR 2 "`verify_prod_import.py`" |
| 7 (P1) | Accepted. The three edges move to `save()` as well as `clean()`, matching how every other invariant here is enforced, and the omitted self-host case is added. | PR 3 "Model"; Tests |
| 8 (P1) | Accepted. The deep link is replaced by a real fit view with two explicit paths, server-side validation, POST-only mutation and a pk-preservation test; `EmptyFieldListFilter` for the filter. | PR 3 "Admin" |
| 9 (P1) | Accepted — verified `has_change_permission()` returns `False` outright when locked (`admin.py:729-732`), which would have made the model-level exemption unreachable. Inline permission relaxed, with an admin-layer test. | PR 1 "Admin"; Tests |
| 10 (P1) | Accepted, and it corrects **this plan's own settled decision 7**, which asserted a guard that does not work: `dmi_dante_racks` is a set of rack *names*. Detection now keeps the rows and requires exactly one. | Settled decision 7; PR 3 "Importer" |
| 11 (P1) | Accepted. `is_add_in_card` joins the type's locked-field snapshot and the admin read-only set. | PR 3 "Model" |
| 12 (P2) | Accepted, both halves. The SD12 `Engine` port gains `hostname_suffix="engine"`. Casing divergence (`DM7C-1-device-control` vs the sheet's lowercase) is recorded as known and deferred to phase 18, and no assertion here compares case-sensitively. | PR 1 "Model"; PR 2 "Importer" |
| 13 (P2) | Accepted — confirmed there is no top-level `networkdevicetypeport` registry entry. Fields go on the `networkdevicetype` inline; `source_type_port` prefetch and the view permission are added. | PR 1 "Read-only UI" |
| 14 (P2) | Accepted — both symbols were wrong in rev 1. Corrected to `DESCRIPTION_TO_DEVICE_KEY` and `_classify_companion_pairs()`. | PR 2 "Importer" |
| 15 (P2) | Accepted. The blanket "changes nothing" claim is replaced by a precise one plus an explicit list of the three validation behaviours that *do* change. | Context |
| 16 (P2) | Accepted. `DESIGN.md:114-125` added to PR 2's doc work; `CONTEXT.md` gets an Add-in Card entry, not just a deletion. | PR 2 "Docs" |
| 17 (P2) | Accepted. `host` added to the auditlog tracking list, with tests for fit, pull and host deletion. | PR 3 "`config/settings.py`"; Tests |

No finding was rejected, and none reached the escalation gate — none contradicts a committed ADR,
changes a deliverable, or attacks a decision settled with Mike on 2026-08-08. Notes 6 and 10 are the
two that changed the plan's substance rather than its detail: one found a verification rule that
would have failed a correct import, the other disproved a guard this plan claimed already existed.

## Risks

**PR 2's migration is the sharp edge.** It moves an address between rows, creates a type port,
mutates `port_count`, and deletes device and type rows — in one transaction, in a fixed order, with
a guard that refuses shapes it does not understand. Review notes 1, 3 and 4 all landed here, which is
itself the signal.

**PR 2 is a deletion of hard-won code.** ADR 0018's lock-ordering and torn-read handling came out of
five rounds of Codex review (`models.py:3158-3217`). It is being removed, not adapted — the risk is
not that the deletion is subtle but that it is incomplete. Review note 2 found eleven references the
first draft missed; the `companion` count in `models.py` should reach zero, and `grep -ri companion
inventory/ config/` should return nothing but historical migrations.

**`hostname_suffix`'s lock exemption is a hole in ADR 0010's guarantee**, in two layers now — the
model and the admin inline. It is justified (a derived label has no materialized counterpart), but it
is the first exemption that lock has ever had, and both layers must compare against the *persisted*
row or the exemption becomes a way to smuggle any edit through.

## Out of scope

Hostname assembly, collision and uniqueness (phase 18); hostname *casing* (phase 18, per review note
12); `Owner`, `location_slug`, `hostname_slug` (phase 17); `VLAN.role` (phase 21); issue #27's
shared-address shape, which `address_source` does not help with; issue #41, which stays open by
decision; card-slot capacity on host types, which ADR 0022 decision 7 declines along with unaddressed
hardware.
