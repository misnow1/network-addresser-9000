> **Revision 4** — PR 1 (#58, `39cf6bc`) and PR 2 (#59, `f4417f4`) are both merged. Incorporates a
> third independent review, of the **PR 3 section only**, against the post-PR-2 tree:
> `REVIEW-1-PLAN-adr-0022-pr3.md`. Revisions 2 and 3 incorporated the two earlier reviews.
> See "Review response" for all three mappings.

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
change; **no addressing or allocation constraint** is added, dropped or altered. If a diff reaches
`suggestions.py`, `_suggest_*`, `occupied_rack_slot_ranges()` or `unique_device_rack_slot`,
something has gone wrong.

*(Rev 3, review note 13: rev 2 said "no database constraint" flat, which was false. PR 2 drops
`NetworkDevice.host` and `NetworkDeviceType.companion_type`, and with them a one-to-one `UNIQUE`
index and two foreign keys. Those are schema constraints on the relationship being deleted, and
dropping them is the point; the claim above is narrowed to the constraints that govern addressing.)*

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
   inventory. *(Rev 3, review note 3: the guard enumerates types referenced by `companion_type`, not
   devices that have a `host`. A companion orphaned by host deletion cannot exist — `host` was
   `CASCADE` — so the shape rev 2's guard was written to catch was the wrong one.)*
10. **`0014_retire_companions` uses historical models throughout** (rev 3, review note 5) —
    `apps.get_model()`, `schema_editor.connection.alias`, and `address_source` written as the
    literal `"operator"`. A module-level import would load the *post*-PR-2 models, where `host` and
    `companion_type` no longer exist, and the live `save()` would reject the ownership and ordinal
    changes as locked fields. This is standard Django practice, but rev 2 never said it, and the
    migration is the one place in this plan where getting it wrong destroys data.

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

**All `file:line` citations in this section were refreshed against `main` after PR 1 merged**
(rev 3, review note 15). Rev 2's citations were taken from the pre-PR-1 tree and several now point
at unrelated code — `admin.py:1162` is now `NetworkSwitchTypeAdmin.get_readonly_fields`, which a
mechanical deletion would have damaged.

### Model — `inventory/models.py`

**Removed** (rev 3 — expanded again per review note 9; rev 2's list was still missing public API
and a whole queryset class):

- `NetworkDeviceType.companion_type` and `_validate_companion_type()`, and `companion_type` from
  `NetworkDeviceType`'s locked-field snapshot.
- `NetworkDevice.host` (the `OneToOneField`), `_materialize_companion()`,
  `_check_companion_creation_possible()`, `_check_companion_type_compatibility()`,
  `_plan_companion_move()`, `_park_companion_if_colliding()`, `_finish_companion_move()`,
  `_host_managed_move`, `_companion_pair_pks()` and its use in `_check_rack_slot_not_occupied()`,
  `_persisted_host_id()` (`:3939`), `_check_pending_move_no_overlap()`, the companion clauses in
  `_locked_fields()`, and the companion branches in `delete()`.
- **The public `companion_rack_slot` / `companion_hostname` properties** (`:3489-3517`) and their
  `_companion_rack_slot` / `_companion_hostname` transient backing attributes. These are ADR 0018's
  documented API, not internals.
- **`NetworkDeviceQuerySet` and its custom manager in their entirety** (`:3175-3217`, `:3284`) —
  the class exists only to give `delete()` companion-aware behaviour. `NetworkDevice.objects`
  returns to a plain manager. Rev 2 described this as "a branch to edit", which understated it.
- **The companion-only `validate_unique()` and `validate_constraints()` overrides**
  (`:3805-3893`).

**Kept and unchanged:** `_lock_type_rows()`, which predates ADR 0018 and has other callers.

**Note on `host`:** PR 2 drops the `OneToOneField`; PR 3 adds a `ForeignKey` of the same name.
Django supports that across two migrations — the review confirmed the drop/add is not the hazard,
*live references* are, which is what the inventories in this section exist to make complete.

### Migration `0014_retire_companions`

The one genuinely dangerous step in this plan.

**Historical models only** (rev 3, review note 5 — rev 2 never said this, and it is the difference
between a migration that works and one that cannot even import). Every lookup and write uses
`apps.get_model()`, never a module-level import: the live models are the *post*-PR-2 shape, where
`host` and `companion_type` no longer exist. Writes go through the historical model's `save()` /
`update()`, never the live one, whose `_locked_fields()` would reject the ownership and ordinal
changes outright. Every query uses `schema_editor.connection.alias`, and `address_source` is
written as the literal `"operator"` rather than by importing the enum.

Ordered steps:

1. **Guard.** Enumerate the `NetworkDeviceType` rows referenced by any `companion_type`, and check
   the whole population — **not** just devices that have a `host`. Rev 2's guard iterated devices
   with a host and so could not see the failure it promised to catch (review note 3). Require:
   every instance of a companion type has a host and a compatible host type; every affected host
   instance has exactly one companion; each companion type has exactly one type port, static, with
   an address on its instance port; and the host type has a port on that same VLAN. Anything else
   raises `RuntimeError` naming the row.
   **A companion orphaned by host deletion is not a case that can exist** — `host` is
   `OneToOneField(..., on_delete=CASCADE)` (`inventory/migrations/0011_device_companions.py:103-116`),
   so deleting a host took its companion with it. The corrupt case that *can* exist is an
   **unlinked instance of a companion type**, and that is what the guard must catch.
2. **Create the type port on each affected host type**, copying `port_type` **and `port_number`**
   from the companion's sole type port (review note 2 — `port_type` is required, and a historical
   `create()` will happily store `""` because choices are not a database constraint). Plus
   `address_source="operator"`, `slot_offset=0`, the companion's VLAN, `description="Device
   Control"`, `hostname_suffix="device-control"`, and the next free `ordinal`. Then **increment that
   type's `port_count`**, or the host type fails `_validate_device_type_port_profile()`'s
   `count != port_count` check the next time a device of that type is created.
   **A host type shared by several devices gets exactly one type port and exactly one `port_count`
   increment**, with every moved instance port pointing at it. Iterate types, not devices.
3. **Guard the destination, then move the instance port row.** Moving can violate
   `unique_device_port_description` or `unique_device_port_ordinal` (`inventory/models.py:4646-4647`)
   if the host already carries a `"Device Control"` port or the chosen ordinal (review note 4).
   Check both before writing, and check the moved row's `port_type`/`port_number` match the type
   port created in step 2. Then update `device`, `description`, `ordinal` and `source_type_port` in
   one write. Do **not** create-then-delete: `unique_device_port_vlan_address_value` forbids both
   rows existing at once, and moving preserves the port's audit history.
4. **Clear `companion_type` on every host type** before deleting the now-portless companion devices
   and their types — the FK is `PROTECT`, so deletion is refused while it is set.
5. `RemoveField` ×2 (`companion_type`, `host`).

Irreversible by design — say so in the docstring, and state that the production database is rebuilt
from the CSVs (ADR 0022, "What this does to production data").

### Importer — `inventory/management/commands/import_prod_data.py`

Rev 3 completes this inventory per review note 6 — rev 2 dropped `companion_key` while leaving
three live readers of it.

- `DeviceTypeSpec.companion_key` removed, **and `_DeviceEntry.companion_slot` /
  `_DeviceEntry.companion_hostname`** (`:439-451`), **the stage-7 companion linking pass**
  (`:794-805`), and **the now-obsolete constructor kwargs in `_stage9_devices()`** (`:1022-1027`),
  which are replaced by an `operator_addresses` payload passed to `NetworkDevice`.
- `DeviceTypeSpec.ports` tuples grow `address_source` and `hostname_suffix`.
- `dm7c` and `dm3` each gain a fourth port: `("Device Control", FN_DANTE_PRIMARY, offset 0,
  OPERATOR, "device-control")`.
- **`sd12`'s existing `Engine` port gains `hostname_suffix="engine"`** — half the stated reason
  `hostname_suffix` exists, and rev 1 never assigned it.
- `dm7c_devctrl` and `dm3_devctrl` specs deleted, with their `DESCRIPTION_TO_DEVICE_KEY` entries
  (`:337`, `:346`, `:349`).
- **`_classify_companion_pairs()`** (`:897`) is rewritten: it still matches a
  `<host>-device-control` row to its host by hostname stem within one rack, and still errors loudly
  on zero or several matches, but feeds the row's address into the host's `operator_addresses`
  instead of emitting a merged companion entry. The keyed-on-hostname reasoning in its docstring
  stays true and stays.
- **`CONSOLES` slots 4 and 16 are released.** Nothing else moves — every other device keeps its
  slot, so no address anywhere else changes.

### `verify_prod_import.py` — four changes, all required

Rev 2 said three. Review note 1 found a fourth that is not optional: the verifier **queries the
deleted schema** and would raise `FieldError` on every run.

- **Remove `EXPECTED_COMPANION_TYPES` (`:168`) and its two readers** (`:508`, and the
  `select_related("companion_type")` query at `:1003-1028`). Delete the two companion identities
  from the expected catalogue (`:183-226`) and **add a `Device Control` port to both console
  profiles**, which currently expect three ports each.
- **`_check_cross_vlan_alignment()` (`:1091-1110`) must exclude `address_source=OPERATOR` ports**,
  resolved through `source_type_port`. It requires every static port on a device to share one host
  offset, and a DM7C at slot 5 carrying `.4` deliberately breaks that. Without this a *correct*
  import fails verification. A port with `source_type_port=NULL` stays included.
- **`_device_address()` must select by description or `source_type_port`, not `(vlan, offset)`**
  (`:550`, `:729-730`). Dante Primary and Device Control now share both values, so the existing
  selector is ambiguous and would silently compare the wrong port.
- **The companion pre-pass consumes both CSV rows but locates one host device**, and the
  expectations become: `CONSOLES` slots 4 and 16 **empty**, the exact expected device-slot set, and
  both VLAN-201 addresses asserted independently.

### Admin — `inventory/admin.py`

Rev 3 completes this per review note 7. Several of these are fatal at import or form-construction
time, not cosmetic.

- **`NetworkDeviceTypeForm` and `NetworkDeviceTypeAdmin.form`** (`:1245-1287`) — companion-only, and
  the form names a field that will no longer exist, so it breaks at form construction. Removed.
- **`NetworkDeviceAddForm`'s `companion_of__isnull=True` queryset filter** (`:469-477`), both forms'
  **`Meta.exclude = ["host"]`** and the companion-aware add-form branches (`:442-477`, `:568-628`,
  `:665-727`), plus the `companion_rack_slot` / `companion_hostname` fields and their validation.
- **`delete_selected_devices` (`:1301-1332`) and `NetworkDeviceAdmin.actions` referencing it** — the
  action exists only to enforce companion deletion rules.
- `NetworkDeviceAdmin` loses its `host` display and read-only handling; `NetworkDeviceTypeAdmin`
  loses `companion_type`.

### Read-only UI — `inventory/views.py`, templates

- `device_detail.html` and `rack_detail.html` lose the tether encoding, and so do **`TetherInfo`**
  (`:238`), **`_tether_for()`** (`:395`), `Occupant.tether` and the companion query shaping around
  them, plus the tether CSS and any UI fixtures that build a pair.
- The registry loses its **`FieldSpec("Companion type", …)` entries**, its `companion_type`
  relation hints (`:1428-1429`), and its **`FieldSpec("Host", …)` entries** (`:1449`, `:1458`).
- **`REGISTRY["networkdevice"].list_select_related=("device_type", "rack", "host")`
  (`views.py:1481`)** (review note 8) — rev 2 removed the Host *columns* but missed this, and it is
  a live query that fails outright once `host` is dropped. `host` also comes out of the rack and
  detail querysets (`:624`, `:914`).

### Docs

`CONTEXT.md`'s **Device Companion** entry is deleted and replaced by an **Add-in Card** entry.
**`DESIGN.md:114-125` loses its `companion_type` and cascading-`OneToOneField` bullets.** ADR 0018
gains a superseded banner. ADR 0017 gains an amended banner naming its scope-boundary section. ADR
0013 gains an amended banner naming its one-address-per-VLAN rule.

---

## PR 3 — Add-in cards

**All citations in this section were refreshed against `main` after PR 2 merged** (rev 4, review
note 6). PR 2 deleted ~3,200 lines, and several PR 3 references had drifted — including one that was
wrong from the start: the device forms do **not** use `Meta.exclude = ["host"]`, they use explicit
field allowlists (`admin.py:431-433`, `:610-612`). `host` is absent from both, and **stays absent
deliberately** — fitting happens through the dedicated flow below, never the ordinary add form.

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

`is_add_in_card` joins `NetworkDeviceType`'s locked-field snapshots (`:2788-2793`, `:2825-2830`) and
its admin read-only set. Flipping it after instances exist would either strand fitted devices or
retroactively offer ordinary equipment to the fit picker.

ADR 0022 decision 6's three edges are enforced in **`save()` as well as `clean()`** — `objects.create()`
never calls `clean()`, and this module enforces every other invariant on the save path:

- a device with a `host` must have `is_add_in_card` set;
- a `host` must not itself be an add-in card (no nesting);
- `host` may not be `self`.

~~**The self-host edge also gets a database `CheckConstraint`**~~ (rev 4, review note 2) —
**withdrawn during implementation. MariaDB will not accept it.**

`~Q(host=F("id"))` raises error 1901, *"Function or expression 'AUTO_INCREMENT' cannot be used in the
CHECK clause of `id`"*. Verified directly against this project's database, with a control confirming
the identical CHECK is accepted on a non-`AUTO_INCREMENT` column — so it is the engine's rule about
`AUTO_INCREMENT`, not the expression's shape, and no rephrasing gets around it.

All three edges are therefore enforced in `_check_host_invariants()` on the `save()` and `clean()`
paths only, with **no database backstop for any of them.** That raises the stakes on the save-path
check: the code review found `objects.create(pk=42, host_id=42)` slipping past it, because
dereferencing a host row that does not exist yet returns `None` and the guard exited early. The
constraint would have caught exactly that. It now compares `host_id` against `pk` before
dereferencing anything.

This is the third engine limitation to defeat a planned constraint in this project — after
`supports_partial_indexes = False` (`PLAN-hostname-ingredients.md` decision 4, and ADR 0022 decision
9's `slot_claim` design before it was withdrawn). Recorded here so a fourth attempt starts from the
answer.

Cross-rack is explicitly *not* checked, and a card with no host is explicitly legal — both need a
test asserting they are **allowed**, so a later reader does not add the "missing" validation.

`host` is **not** in `_locked_fields()` (`:3563-3564`): changing it is what fitting and pulling *are*.
`rack` and `rack_slot` are untouched by fitting — a pulled card keeps both, holding its address in
the pool, which is ADR 0022's central claim and wants its own test.

### Migration `0015_add_in_cards`

`AddField` ×2. No data migration, and **no `AddConstraint`** — see the withdrawal above.

`0014` dropped a `OneToOneField` named `host`; this adds a `ForeignKey` of the same name. Verify the
resulting index name matches what a fresh build produces — `makemigrations --check` will not catch a
divergence that only shows up in the database.

### Concurrency — `select_for_update`, in PK order

Rev 4, review note 3. One transaction is not enough: two requests can each observe the same card as
hostless and fit it to different hosts, last commit winning; and Django's deletion collector can
discover `SET_NULL` children before a concurrent fit commits, then fail the host delete on the new
FK.

The fit flow locks the target host **and** the existing card with `select_for_update()` in
deterministic primary-key order, then re-checks inside the lock that the host still exists, is not
itself a card, and the card is still hostless and card-typed. Host deletion locks the host before
collector discovery. `TransactionTestCase` coverage for competing fits, and for a fit racing a host
deletion.

Deterministic lock ordering is the same discipline ADR 0018's move machinery needed and got wrong
twice before review caught it; that code is gone, but the hazard is not.

### Audit — the `SET_NULL` path does not audit itself

Rev 4, review note 1, and the sharpest finding in this review. Django's deletion collector clears
reverse FKs with `QuerySet.update()`, which bypasses `save()` and therefore every auditlog signal.
Adding `host` to `AUDITLOG_INCLUDE_TRACKING_MODELS` (`config/settings.py:282`) is **necessary but not
sufficient** — orphaning a card by deleting its host would silently leave no trace on the card.

So deleting a host **clears its cards through audited per-row saves first**, then deletes the host.
Same for **Pull**: it must not be `queryset.update(host=None)`.

The tests must assert the card's own log records `host: <old pk> → None` **with the actor** — not
merely that "an audit entry exists", which passes vacuously on the host's own deletion entry. Cover
instance `delete()`, queryset delete, and the admin's delete action.

### Admin — `inventory/admin.py`

- `NetworkDeviceTypeAdmin`: `is_add_in_card` on the form, in `list_filter`, read-only once instances
  exist.
- `NetworkDeviceAdmin`: a host column in `list_display`, `host` in `list_select_related`, and
  **`("host", EmptyFieldListFilter)`** for fitted/unfitted — a plain `host` filter lists every
  individual host.
- **A dedicated "Fit a card" view**, wrapped in `admin_site.admin_view`, reached from an object tool
  on the host's change page. Two mutually exclusive paths:
  - *Choose an existing hostless card* — requires **change** permission on both rows.
  - *Create a new card* — requires **add** permission on `NetworkDevice` plus **change** on the host
    (review note 4; "change on both rows" is wrong for a row that does not exist yet).

  The create path **reuses `NetworkDeviceAddForm.with_operator_fields()`** with its type queryset
  restricted to `is_add_in_card=True`, instantiated against `NetworkDevice(host=locked_host)` so
  `full_clean()` sees the relationship and the row is inserted once with `host` already set (review
  note 5). A bespoke form would silently bypass that form's rack-slot suggester, operator-address
  fields, materialization pre-flight and transient properties.

  Host and type are validated **server-side**, not merely shaped by a queryset — a crafted POST
  naming a non-card type or a card-typed host must be refused. POST-only mutation, one transaction,
  and a named redirect target.
- A **"Pull"** action (change permission) clearing `host` via an audited save, leaving `rack`,
  `rack_slot` and every address alone.

### Read-only UI — `inventory/views.py`, templates

`device_detail.html` grows a "Cards fitted" list on a host and a "Fitted to …" line on a card.
`REGISTRY`'s `networkdevicetype` entry gains `is_add_in_card`; `networkdevice` gains `host` (a
`FieldSpec` with `render="relation"` plus `list_select_related`).

**The registry does not feed the device page.** `canonical_detail_view` redirects `networkdevice` to
the shaped `device_detail()` (`views.py:854-865`), so the new relations must be added to *that*
view's queryset (review note 8): `host` in `select_related()`, `installed_cards` prefetched with
whatever type/rack relations the panel renders. Without it, "Fitted to" is one extra query and "Cards
fitted" is an N+1. Extend the existing device-detail query-budget test.

### Importer — `inventory/management/commands/import_prod_data.py`

`dmi_dante` is marked `is_add_in_card=True`, and the card entry is linked to the console whose row
carried the marker.

Per settled decision 7, the detection at **`:895-900`** must **collect the marker rows, not just
their rack names**, and require **exactly one**. `dmi_dante_racks` is still a `set[str]` of rack
names, so two marker-bearing consoles in `CONSOLES` collapse to one entry and pass the guard
undetected. The entry is appended at `:926` and the device created at `:1055-1072` (rev 4, review
note 6 — the rev-3 citations `:862-867` and `:893` now point at unrelated control flow).

`verify_prod_import.py` gains the link as an expectation, with a test that catches a card linked to
the wrong console. **No address changes** — the card stays at `CONSOLES` slot 17 with
`10.201.6.17` / `10.202.6.17`, and **#41 stays open**.

### Docs

`CONTEXT.md`'s **Add-in Card** entry (`:45-47`) gains the nullable `host` link and states plainly
that it carries no addressing meaning. **`DESIGN.md:109-124`** gains `is_add_in_card` and `host` in
its model outline, noting that a card keeps its own rack, slot, addresses and lifecycle (rev 4,
review note 9).

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

**PR 2** (rev 3 — the deletion targets are now exact, per review notes 10, 11, 12, 14):

*Deleted:* `DeviceCompanionTests`, `DeviceCompanionMigrationTests`, `UnrackedCompanionTetherTests`,
the tether assertions in `ElevationEncodingTests`, and — **not** the range rev 2 named. `tests.py:6407-6597`
was wrong: `:6407-6409` is the tail of an unrelated spanning-type helper and `:6422-6507` is ordinary
rack-slot suggestion coverage that must survive. The companion-specific material is the helper at
`:6411-6420`, the form inputs at `:6441-6442`, and the tests at `:6512-6605`. Delete those pieces.

*Rewritten, not deleted:* `test_prod_import.py:537-561` for four console ports; `test_ui.py:1856-1889`,
which expects the Companion type and Host columns; `ElevationEncodingTests` at `:943-960`, which uses
a companion pair outside its tether assertion; and `tests.py:7204-7205`, which still submits the
removed add-form fields.

*Kept and renamed:* `ImportProdDataMalformedCompanionTests` (`test_prod_import.py:696-721`). The
rewritten pre-pass still promises to reject an unmatched `-device-control` row, so the coverage
stays; add the several-host-match case it also promises. Delete the broken/reverse-link tests at
`:566-592`, which test a relationship that no longer exists.

*New:* a DM7C materializes four ports with the Device Control at its supplied address; the migration
**moves** the port row rather than duplicating it; the migration handles **a host type shared by two
devices** — one type port created, `port_count` incremented once, both moved rows pointing at it; the
migration **raises on each inadmissible shape** (multi-port, DHCP, and an *unlinked instance of a
companion type*, which is the corruption that can actually exist) and succeeds on a second valid pair
beyond the two production ones; the migration refuses when the destination already carries a
`"Device Control"` description or the target ordinal; nothing else in `CONSOLES` moves slot.

*Verifier:* the four CSV rows reproduce exactly, `CONSOLES` has two fewer devices, slots 4 and 16 are
free, both VLAN-201 addresses are asserted independently, corrupting only the Device Control address
fails verification, and every other rack is byte-identical to before. **The alignment exemption is
bounded** (review note 14): a corrupted ordinary `SLOT` port on the same console must still fail
alignment, and a port with `source_type_port=NULL` must still be included — otherwise an
implementation that exempts the whole console passes the happy-path test.

**PR 3** (rev 4 — audit, concurrency and permission coverage added):

*Lifecycle:* a card is fitted to an existing host and pulled, keeping its rack, slot and addresses; a
pulled card is re-fitted to a *different* host as the same row; deleting a host leaves its cards
racked, addressed and hostless.

*Edges:* a non-card type cannot be given a host, a card cannot host a card, and a card cannot host
itself — **each through `objects.create()` as well as `full_clean()`**, and the self-host case also
at the database level via the new `CheckConstraint`. A card may sit in a different rack from its host
and a hostless card is legal — **both asserted as allowed**. `is_add_in_card` cannot be changed once
instances exist.

*Audit* (rev 4, review note 1): fitting, pulling, and **orphaning by host deletion** each write an
entry **on the card**, asserted as `host: <old pk> → None` with the actor — not merely "an audit
entry exists", which passes vacuously on the host's own deletion record. Covers instance `delete()`,
queryset delete and the admin delete action.

*Concurrency* (`TransactionTestCase`, review note 3): two competing fits of one card resolve to a
single host rather than last-write-wins; a fit racing a host deletion neither orphans silently nor
fails the delete on a newly created FK.

*Permissions* (review note 4): the existing-card path requires change on both rows, the create path
requires add on `NetworkDevice` plus change on the host, Pull requires change — with a denial test
per missing permission, plus a crafted POST naming a type outside the picker queryset and one naming
a card-typed host.

*Fit flow:* the existing-card path **preserves the primary key**; the create path inserts once with
`host` already set and goes through `NetworkDeviceAddForm.with_operator_fields()`, so a card type
carrying an `OPERATOR` port still prompts for its address.

*Importer* (review note 7): **zero** DMI-DANTE markers and **two same-rack** markers must each raise
`CommandError`, and a failed import must leave no partial rows. The current guard passes both cases
— zero because the block never runs, two because `dmi_dante_racks` is a set of rack names.

`test_ui.py`: the cards-fitted panel, the fitted-to line, the registry additions, and the shaped
device-detail query budget with `host` selected and `installed_cards` prefetched.

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

### Second review — `REVIEW-1-PLAN-adr-0022-pr2.md` (rev 3)

The PR 2 section only, re-reviewed against the tree **after PR 1 merged**. Fifteen findings, all
verified against the code, all folded in, none rejected and none escalated.

| Note | Resolution | Section |
|---|---|---|
| 1 (P1) | Accepted — confirmed `EXPECTED_COMPANION_TYPES` at `verify_prod_import.py:168` with readers at `:508` and a `select_related("companion_type")` query at `:1003-1028`. The verifier would have raised `FieldError` on every run. Verifier work goes from three changes to four. | PR 2 "`verify_prod_import.py`" |
| 2 (P1) | Accepted. The new type port copies `port_type` **and** `port_number` from the companion's type port; a historical `create()` would otherwise store `port_type=""` silently, since choices are not a DB constraint. | PR 2 migration step 2 |
| 3 (P1) | Accepted, and it corrects the guard's whole premise — `host` was `CASCADE` in `0011`, so an orphaned companion cannot exist and rev 2's guard was watching for the wrong corruption. It now enumerates types referenced by `companion_type` and catches the case that can exist: an unlinked companion-type instance. | Settled decision 9; PR 2 migration step 1 |
| 4 (P1) | Accepted — `unique_device_port_description` and `unique_device_port_ordinal` both apply to the destination device (`models.py:4646-4647`). Both are guarded before the move, with a shared-host-type test. | PR 2 migration step 3; Tests |
| 5 (P1) | Accepted. Historical models, connection alias and literal enum value are now a settled decision of their own rather than an unstated assumption. | Settled decision 10; PR 2 migration preamble |
| 6 (P1) | Accepted — rev 2 dropped `companion_key` while leaving three live readers. `_DeviceEntry.companion_slot`/`companion_hostname`, the stage-7 linking pass and the `_stage9_devices()` kwargs are now named. | PR 2 "Importer" |
| 7 (P1) | Accepted. `NetworkDeviceTypeForm` breaks at form construction once the field is gone, which rev 2 would not have caught until runtime. That plus the queryset filter, both `Meta.exclude`, the add-form branches and the delete action are now listed. | PR 2 "Admin" |
| 8 (P1) | Accepted — confirmed `list_select_related=("device_type", "rack", "host")` at `views.py:1481`. Rev 2 removed the Host *columns* and missed the live query behind them. | PR 2 "Read-only UI" |
| 9 (P1) | Accepted. The public `companion_rack_slot`/`companion_hostname` properties, `_persisted_host_id()`, `_check_pending_move_no_overlap()`, and the whole of `NetworkDeviceQuerySet` and its manager are added — rev 2 called the last of these "a branch to edit", which understated it. | PR 2 "Model" |
| 10 (P1) | Accepted, and the most dangerous finding here: the stated range `tests.py:6407-6597` begins inside an unrelated spanning-type helper and covers ordinary rack-slot suggestion tests. Replaced with the exact companion-specific pieces. | Tests, PR 2 |
| 11 (P1) | Accepted. `ImportProdDataMalformedCompanionTests` is **kept and renamed** rather than deleted — the rewritten pre-pass still owes that behaviour — while the reverse-link tests go. | Tests, PR 2 |
| 12 (P2) | Accepted. `test_ui.py:1856-1889`, `ElevationEncodingTests:943-960` and `tests.py:7204-7205` named as rewrites. | Tests, PR 2 |
| 13 (P2) | Accepted. The "no database constraint" claim is narrowed to addressing and allocation constraints, and the dropped `UNIQUE`/FK indexes are acknowledged as the point of the change. | Context |
| 14 (P2) | Accepted. The alignment exemption gets negative tests, so an implementation that exempts the whole console cannot pass. | Tests, PR 2 |
| 15 (P3) | Accepted. All PR 2 citations refreshed against post-PR-1 `main`; `admin.py:1162` in particular now points at unrelated switch-admin code. | PR 2, throughout |

### Third review — `REVIEW-1-PLAN-adr-0022-pr3.md` (rev 4)

The PR 3 section only, re-reviewed against the tree **after PR 2 merged**. Nine findings, all
verified against the code. Eight accepted; one accepted in part with the remainder argued down.

| Note | Resolution | Section |
|---|---|---|
| 1 (P1) | Accepted — the sharpest finding here. Django's deletion collector clears `SET_NULL` reverse FKs with `QuerySet.update()`, bypassing `save()` and every auditlog signal, so adding `host` to the tracking list was necessary but not sufficient: orphaning a card would have left no trace on the card. Host deletion and Pull both go through audited per-row saves, and the tests assert the specific `host: <pk> → None` transition with actor rather than "an entry exists". | PR 3 "Audit"; Tests |
| 2 (P1) | **Accepted in part.** The self-host edge gains a database `CheckConstraint` (`~Q(host=F("id"))`) — cheap and worth having. **Rejected: narrow queryset/manager guards for `host`, `device_type` and `is_add_in_card`.** `models.py:203-206` already documents `QuerySet.update()`/`bulk_create()` bypassing `Model.save()` as a **known, deliberate, project-wide gap** covering every invariant in this module, not just these. Adding a custom queryset for `host` alone would be inconsistent with every neighbouring invariant, and PR 2 has just finished deleting the only device queryset this model ever had. Reintroducing one to guard the replacement relationship is exactly the shape of drift these reviews exist to prevent. The gap is documented and tested as a gap, not closed here; closing it belongs to a change that closes it everywhere. | PR 3 "Model"; Tests |
| 3 (P1) | Accepted. `select_for_update()` on host and card in deterministic PK order, re-checking the invariants inside the lock; host deletion locks before collector discovery; `TransactionTestCase` coverage for competing fits and fit-versus-delete. PR 3 also gets its own Risks entry, which it lacked. | PR 3 "Concurrency"; Tests; Risks |
| 4 (P1) | Accepted — "change permission on both rows" is wrong for a row that does not exist yet. Create requires `add` on `NetworkDevice` plus `change` on the host; the view is wrapped in `admin_site.admin_view`; denial tests per permission and for crafted POSTs. | PR 3 "Admin"; Tests |
| 5 (P2) | Accepted. The create path reuses `NetworkDeviceAddForm.with_operator_fields()` against `NetworkDevice(host=locked_host)` rather than a bespoke form, which would have silently bypassed the rack-slot suggester, the operator-address fields and the materialization pre-flight. | PR 3 "Admin" |
| 6 (P2) | Accepted. Citations refreshed; note that `Meta.exclude = ["host"]` never existed — both forms use explicit allowlists, and `host` stays out of them deliberately. | PR 3, throughout |
| 7 (P2) | Accepted. Zero markers and two same-rack markers must each raise; the current guard passes both. | Tests, PR 3 |
| 8 (P2) | Accepted — `canonical_detail_view` redirects to the shaped `device_detail()`, so registry `select_related` does nothing for it. Both relations move to that view's queryset, with the query budget extended. | PR 3 "Read-only UI"; Tests |
| 9 (P3) | Accepted. `CONTEXT.md:45-47` and `DESIGN.md:109-124` both updated. | PR 3 "Docs" |

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

**PR 3's sharp edge is concurrency, not data** (rev 4). Nothing it does destroys anything — the worst
outcome is a card fitted to the wrong host, or orphaned without an audit trail. But `SET_NULL` and a
fit flow that mutates two rows together put it in the same territory ADR 0018's move machinery
occupied, and that code needed five rounds of review to get its lock ordering right. The machinery is
gone; the hazard is not. `select_for_update()` in deterministic PK order, and `TransactionTestCase`
coverage rather than `TestCase`, are the whole defence.

**The `SET_NULL` audit gap is the kind of thing that passes review by looking done.** Adding a field
to the auditlog tracking list reads as sufficient and is not, because the deletion collector never
calls `save()`. A test asserting only that "an audit entry exists" passes on the host's own deletion
record while the card silently has none.

## Out of scope

Hostname assembly, collision and uniqueness (phase 18); hostname *casing* (phase 18, per review note
12); `Owner`, `location_slug`, `hostname_slug` (phase 17); `VLAN.role` (phase 21); issue #27's
shared-address shape, which `address_source` does not help with; issue #41, which stays open by
decision; card-slot capacity on host types, which ADR 0022 decision 7 declines along with unaddressed
hardware.
