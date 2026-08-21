> **Revision 2** — incorporates review notes from `REVIEW-1-PLAN-adr-0024.md`, an independent
> `codex` review of revision 1. See "Review response" for the mapping. No finding hit the
> escalation gate; one (note 5) is accepted rather than solved, with the argument recorded.

# Implement ROADMAP phase 22 — Dante device names and the Yamaha unit ID

## Context

`docs/adr/0024-dante-device-names.md` landed as a design record in e9e8935 and separates three
things this project had been treating as one: a **hostname** (a convenience nothing reads), a
**Dante device name** (an identifier Dante routes audio by), and a **Yamaha unit ID** (the `Y0##`
key a console addresses a box with). This phase builds it.

The ADR calls it small, and it is: one nullable field, one derived property, one conditional
validation. No migration touches data. What makes it non-trivial is that the derived name composes
with machinery phase 18 already shipped — ADR 0023's bulk recompute action can now silently take
equipment off air — and that the *blank* state of the new field is load-bearing, which breaks the
pattern every other suggester in this codebase follows.

### The estate, measured

**Measured against the deployment database** (`network-addresser-9000-db-1`, current through
`0018_seed_hostname_slugs`), through the app container — *not* the database `.env` points at.

Those are two different databases, and the distinction matters for anyone re-running these numbers.
`.env` says `DB_HOST=127.0.0.1 DB_PORT=3306`, and the only container publishing 3306 to the host is
`na9000-mariadb-dev`, which is **three migrations behind** — `0016`/`0017`/`0018` unapplied, no
`owner_id` column, hostnames still in their pre-normalisation form (`Cisco SG300-10MP (For 3xAmp
Rack Primary)`). The deployment's own database is not published to the host at all, so `.env` cannot
reach it. Measure through `docker exec`; the test suite, which uses `.env`, wants the dev database
migrated separately.

| | value |
|---|---|
| devices / switches | 63 / 21, 84 non-blank hostnames |
| **devices needing a unit ID** | **2** — `mps-rio3224d3-1` (Yamaha Rio3224-D3, pk 70) and `bej-tio1608d2-1` (Yamaha Tio1608-D2, pk 46) |
| **consoles that must not get one** | **3** — `mps-dm7c-1`, `mps-dm7ex-1`, `bej-dm3-1` (decision 6) |
| unit IDs assigned | 0 — the field does not exist yet |
| longest hostname | **19** — `mps-wpm2sl-plm20q-1` |
| hostnames over 26 (would error where a unit ID is set) | **0** |
| hostnames over 31 (would advise) | **0** |
| longest type `hostname_slug` | 9 — `rio3224d3`, `tio1608d2` |
| longest name ADR 0023's scheme *can* build from live components | 33 — `bej-w8lm1sr-rio3224d3-midhi-01-04`, 36 with a sequence (ADR 0024 decision 2) |

Three things follow, and they change how this phase is tested.

**Neither the blocking error nor the advisory is reachable on today's data.** Nothing is within 7
characters of the limit. Both rules are latent in exactly the way the 63-character cap was before
phase 18 measured it — so **every test of them constructs its own case**; none can lean on a
fixture that happens to be long. The 33-character example above remains reachable only by composing
component values that exist separately, which is the ADR's own argument.

**The ADR's headline count is confirmed exactly.** Five Yamaha devices are in the estate; two are
controlled boxes needing an ID, three are consoles doing the controlling. That is decision 6 as a
measurement rather than an assertion, and it gives PR 2's tests real hostnames to use.

**The Rio has already been renamed by phase 18.** `prod/MPS Audio Network Standards - Dante
Devices.csv:29` records its Dante name as `mps-stage-rio-1`; the live row is now `mps-rio3224d3-1`.
Neither carries a `Y0##` prefix, so the ADR's claim holds — the box is not correctly integrated
today — but the *name in the CSV is already stale*, which is worth knowing before anyone reaches
for that file as a source of truth about what to type into Dante Controller.

## Decisions this plan settles (ADR 0024 left them to the build)

All eleven were put to Mike and answered before this plan was written.

1. **Two PRs**, split rules / surfaces:

   | PR | Contents | Why separate |
   |---|---|---|
   | 1 | Migration, `dante_unit_id`, `inventory/dante.py`, the derived property, uniqueness + the 31-character rule, the suggester, auditlog | The whole rule set, reviewable without any UI noise. Nothing renders yet — deliberately, the same shape as phase 18 PR 2 |
   | 2 | Admin forms and live help text, the recompute skip and the rename warning, `device_detail.html`, the parity registry | Every operator-visible surface, judged against rules already settled |

2. **The suggester is displayed, never written.** Every other suggester here (`hostname_sequence`,
   `address_range`, `rack_slot`, `default_gateway`) fills a blank field in `clean()`.
   `dante_unit_id` **must not**: blank means "not controlled by a Yamaha console", which is decision
   6's entire point (`DM7C`/`DM7-EX`/`DM3` carry no ID) and decision 2's opt-in. Filling it would
   hand a unit ID to all 61 devices including the consoles, and silently make every one of them
   subject to the 31-character rule. Instead `NetworkDeviceAdmin.get_form()` appends the live
   suggestion to the field's help text, on **both** add and change forms — assigning an ID to an
   already-existing device is the common case here, since both boxes that need one already exist.

3. **`inventory/dante.py` is pure — no queries, no model imports.** `inventory/hostnames.py` imports
   from `models.py`, which is why `models.py` cannot import *it* and has to carry a duplicate of
   `assemble_hostname()` (`_assemble_hostname_stem`, `models.py:586`) with an apology attached.
   Dante does not repeat that: the derivation rule lives in one module that imports nothing from the
   app, so `models.py` imports it directly and there is exactly one copy. The suggester stays pure
   by taking the assigned IDs as an argument — the `lowest_free_run()` pattern
   (`suggestions.py:89`) — and the admin does the query.

4. **Uniqueness is enforced twice.** A `UniqueConstraint` on `dante_unit_id` *and* the ADR's
   plain-language `full_clean()` error naming the conflicting device. The ADR only specifies the
   latter; the constraint is the backstop for paths that never validate — `objects.create()`, the
   recompute action's `save()`, any future importer. Safe to add unconditionally: the column is born
   entirely null and MariaDB does not collide NULLs in a unique index, the same property
   `unique_device_rack_slot` already relies on over nullable `rack`/`rack_slot`.

   This deliberately **departs from the hostname posture**, which is report-don't-enforce with no DB
   uniqueness at all. The justification is that the two cases differ in kind: the live estate holds
   32 rows across 5 duplicated hostnames by design, whereas a duplicated unit ID has no legitimate
   reading — it is two boxes answering one console.

5. **The 1–127 range is enforced twice too** — `MinValueValidator`/`MaxValueValidator` plus a
   `CheckConstraint`, matching `networkdevice_rack_slot_gte_1` (`models.py:3573`) rather than
   inventing a second posture on the same model.

6. **The over-31 advisory is `NetworkDevice`-only, and only where the unit ID is null.**

   Two narrowings, both deliberate:

   - **Not `NetworkSwitch`.** A switch carries Dante traffic but is never itself a Dante device and
     has no Dante device name to be rejected. Firing "Dante will reject this name" on a Cisco SG300
     asserts something untrue, which is worse than silence.
   - **Only where no unit ID is set.** ADR 0024 decision 2 says the advisory fires "whatever its
     unit ID"; `ROADMAP.md` phase 22 says "where no unit ID is set … raises a non-blocking advisory
     **instead**". They disagree in exactly one case — a unit-ID device with a hostname over 31 —
     and in that case the blocking error has already fired (33 + 5 = 38), so the ADR's reading shows
     the operator two messages saying nearly the same thing with different numbers. This plan
     follows ROADMAP. **Flagged for the reviewer as a knowing divergence from the ADR's wording**;
     Mike declined to amend the ADR, so the record keeps its phrasing and this plan carries the
     reason.

7. **Recompute skips a device whose computed name would break Dante's limit**, rather than writing
   it. The action saves via `current.save()`, which never calls `full_clean()`, so decision 2's
   enforcement is bypassed on that path — the action could write a hostname the device's own change
   form then refuses to save, stranding the row. Skipping reuses the action's existing
   `_HostnameRecomputeResult("skipped", reason, …)` path (the one that already reports "missing
   owner and type's hostname_slug") and never leaves a device in a state the admin cannot save.

   This does **not** contradict decision 5's "the action still renames". Decision 5 refuses to skip
   a device *merely for carrying a unit ID*; this skips for a computed name that is invalid, which
   the action already does for other reasons.

8. **The Dante rename warning fires on every path that changes a unit-ID device's Dante name**, not
   only the bulk recompute action decision 5 names. Editing `dante_unit_id` from 1 to 3 on the
   change form changes the Dante name as completely as any rename, and Shure's warning — *"Changing
   the Dante ID will cause a loss of audio signal"* — is literally about changing the ID. Warning
   only on the path that changes the *hostname* would leave the more direct hazard silent.

   Scope, precisely:

   | Change | Warns? |
   |---|---|
   | recompute renames a device carrying a unit ID | yes |
   | change form edits `dante_unit_id` (1 → 3) | yes |
   | change form renames a device carrying a unit ID | yes |
   | change form sets a first unit ID (null → 1) | yes — the name gains a prefix |
   | change form clears a unit ID (1 → null) | yes — the name loses one |
   | change form renames a device with no unit ID either side | **no** — the tool cannot know it is a Dante device (decision 2's structural gap) |
   | creating a device with a unit ID | **no** — nothing exists to re-subscribe; that is commissioning, not an outage |

   Broader than decision 5's literal text. **Flagged for the reviewer**; Mike chose the behaviour
   without an ADR amendment.

9. **Surfacing: the ID everywhere, the name only where an ID is set.** `dante_unit_id` joins the
   admin changelist and the parity list columns — sparse (2 rows of 61) and informative, and "which
   IDs are taken" is the question an operator actually has. The derived name renders only on detail
   surfaces and only where a unit ID is set: the property returns the bare hostname when the ID is
   null (decision 1's table), so showing it unconditionally would print a "Dante device name" equal
   to the hostname on 59 devices, asserting Dante membership the tool cannot establish.

10. **`dante_unit_id` must be added to `AUDITLOG_INCLUDE_TRACKING_MODELS`.** `NetworkDevice` is
    registered with an `include_fields` **whitelist** (`config/settings.py:314`), which does not pick
    up new fields — the same trap the comment above that entry already documents for phase 18's
    `hostname`. Without this, the one change both vendors describe as audio-affecting leaves no
    audit trail at all.

11. **No data migration.** The Rio and the Tio get their IDs by hand, from Mike, against the actual
    front panels. A unit ID records what the physical box is set to; the CSV records none, so any
    value this phase wrote would be inference wearing the costume of an import — and a wrong ID that
    looks authoritative routes audio to the wrong box, which is the exact failure decision 4 exists
    to prevent.

## PR 1 — the rules

### `inventory/dante.py` (new)

Pure functions and the four constants, importing nothing from the app (settled decision 3).

```python
DANTE_UNIT_ID_MIN = 1            # Rio: Y000–Y07F is 0–127; Shure: hex 01–FF is 1–255
DANTE_UNIT_ID_MAX = 127          # the intersection (ADR 0024, "The ranges disagree")
DANTE_NAME_MAX_LENGTH = 31       # Audinate's published limit
DANTE_UNIT_ID_PREFIX_LENGTH = 5  # "Y0##-" — never used to derive a hostname cap, only to explain one
```

- `dante_device_name(unit_id: int | None, hostname: str) -> str | None` — decision 1's table, in
  order: a blank `hostname` returns `None` **first**, before the unit ID is consulted at all, which
  is what makes the `set`/`blank` row return `None` rather than the illegal `Y001-`. Otherwise
  `hostname` where `unit_id` is null, `f"Y0{unit_id:02X}-{hostname}"` where it is not. `:02X` is
  uppercase per both vendors' examples (`Y01B`, never `Y01b`).
- `over_length_advisory(hostname: str) -> str | None` — the non-blocking message for a hostname over
  `DANTE_NAME_MAX_LENGTH`, `None` otherwise. Callers own the "only when the unit ID is null"
  condition (settled decision 6), not this function.
- `length_error(unit_id: int, hostname: str) -> str | None` — the blocking message, stating the
  arithmetic exactly as the ADR does. `None` when the assembled name fits.
- `rename_warning(label, *, old_unit_id, new_unit_id, old_name, new_name) -> str` — one helper, so
  the change form and the recompute action cannot drift apart (settled decision 8).
- `suggest_unit_id(assigned: Iterable[int]) -> UnitIdSuggestion | None` — a `NamedTuple` of
  `(value: int, reclaimed: bool)`, or `None` when all 127 are in use. Highest + 1 while the highest
  is below 127; at 127 it falls back to the lowest unused value with `reclaimed=True`, so the caller
  can name what is being reclaimed (decision 4's "degrading loudly rather than refusing"). Never
  lowest-free before that point: Dante routes to whatever currently holds a name.

### Operator-facing strings

Pinned here so review argues with the wording once, not per call site. **No ADR references in any
of them** — they are for operators.

- `dante_unit_id` help text — the ADR's, verbatim: *"Yamaha consoles find and control this device by
  this number. Must be unique across every Yamaha-controlled device on the network — stage boxes and
  wireless receivers share one range. 1–127. Leave blank for equipment that is not controlled by a
  Yamaha console, including the consoles themselves."*
- Suggestion appended to it (PR 2): `"Next free unit ID: 3."`
- Reclaim wording: `"Allocation has reached 127, so the next suggestion is 4 — a gap, which may
  have been used before. Check what last held Y004- before using it, or audio may route to the
  wrong box."` **Claims only what the system knows** (review note 6): "every ID up to 127 is
  assigned" is false whenever the highest is 127 *and* gaps exist, which is precisely the state
  that triggers this message, and nothing retains the history that would prove 4 was ever used.
- Exhausted: `"All 127 unit IDs are in use."`
- Uniqueness error, on `dante_unit_id`: `"Dante unit ID 1 is already used by mps-stage-rio-1."`
- Length error, on `hostname`: *"With Dante unit ID 1 this device's Dante name would be 33
  characters. Dante allows 31, and the `Y001-` prefix uses 5, leaving 26 for the hostname."*
- Length advisory (info): *"This hostname is 33 characters. Dante's device-name limit is 31, so if
  this device is on a Dante network its name will be rejected."*
- Rename warning (warning): *"mps-stage-rio-1 is a Dante device (unit ID 1). Its Dante name is now
  `Y001-mps-stage-rio-2` (was `Y001-mps-stage-rio-1`) — update it in Dante Controller and rebuild
  its subscriptions, or audio will not route."* The `(was …)` clause is omitted when there was no
  previous name. Where the ID was cleared, the first sentence becomes *"… no longer carries a Dante
  unit ID."*; where the hostname is blank so the new name is `None`, the second becomes *"It has no
  Dante name until it has a hostname — its old name `Y001-…` is now unclaimed in Dante Controller."*
- `dante_device_name` read-only help text — the ADR's, verbatim: *"The name to set in Dante
  Controller. Dante routes audio by this name, so changing it drops audio until subscriptions are
  rebuilt — and a name that was previously in use will pull audio from whatever now holds it."*

### `inventory/models.py`

- `NetworkDevice.dante_unit_id` — `PositiveSmallIntegerField(null=True, blank=True,
  validators=[MinValueValidator(1), MaxValueValidator(127)], help_text=…)`. Not a locked field: the
  whole point is that an operator can set and change it.
- `Meta.constraints` gains `UniqueConstraint(fields=["dante_unit_id"],
  name="unique_device_dante_unit_id")` and `CheckConstraint(condition=Q(dante_unit_id__isnull=True) |
  Q(dante_unit_id__range=(1, 127)), name="networkdevice_dante_unit_id_range")`.
- `dante_device_name` — a read-only `@property` delegating to `dante.dante_device_name(self.
  dante_unit_id, self.hostname)`. Nothing stored (decision 1, and ADR 0022 decision 4's reasoning: a
  stored copy has nothing keeping it in step).
- `NetworkDevice.clean()` gains a Dante block at the end, **accumulating into one error dict** so an
  operator who has both problems sees both:
  - uniqueness, null exempt, on creation as well as rename (decision 3 — no importer writes unit
    IDs, so no rebuild can break). Excludes `self.pk` only when it is not `None`, mirroring
    `_validate_hostname_unique()` rather than relying on `exclude(pk=None)`'s isnull rewrite.
  - the 31-character check, raised **on the `hostname` field** (decision 2 — it lands where the
    operator is typing), only where a unit ID is set.

### `config/settings.py`

`dante_unit_id` joins the `inventory.NetworkDevice` `include_fields` list (settled decision 10),
with a comment naming the reason so the next field to be added gets the same treatment.

### `inventory/migrations/0019_dante_unit_id.py`

`AddField` + the two constraints. No `RunPython`, no data touched (settled decision 11).

### Tests (PR 1, `inventory/tests.py`)

- `dante_device_name` across **all four rows** of decision 1's table, including blank-hostname-with-
  a-unit-ID yielding `None` and **not** `Y001-`.
- Uppercase hex: 27 → `Y01B`; and 1 → `Y001`, 127 → `Y07F`.
- Uniqueness: rejected on **creation** as well as rename; null exempt (two null devices coexist);
  the error names the conflicting device.
- The DB constraint bites where `full_clean()` was skipped — `IntegrityError` on a duplicate written
  straight through `objects.create()`.
- Range: 0 and 128 rejected by validation; 1 and 127 accepted; the check constraint rejects 0 from a
  raw write.
- The 31-character rule: errors only where a unit ID is set (a 27-character hostname with an ID
  errors; the same hostname without one saves); the message states the assembled length, the limit,
  the prefix length and the remaining budget.
- **The length boundary from both sides** (review note 2): a 26-character hostname with a unit ID
  saves (26 + 5 = 31, exactly the limit); 27 is rejected. Each such test asserts the length of the
  string it built, so it cannot silently stop testing the boundary if a fixture changes.
- **The range boundary from both sides at the database** (review note 2): a raw write of **128** is
  rejected by the check constraint, not only 0.
- **Self-exclusion**: an existing device re-validates cleanly against its own unchanged unit ID —
  otherwise every ordinary save of a Dante device fails on its own ID (review note 2).
- **Both errors together** (review note 2): a device with a duplicate unit ID *and* an over-long
  hostname reports a `dante_unit_id` error and a `hostname` error in one `ValidationError`, which
  is what the accumulate-into-one-dict decision above exists to produce.
- `suggest_unit_id`: empty → 1; `{1, 2}` → 3; a gap is **not** reclaimed (`{1, 3}` → 4); `{…, 127}`
  → lowest unused with `reclaimed=True`; all 127 assigned → `None`.
- Auditlog records a `dante_unit_id` change (guards the whitelist trap directly, not by inspection).

## PR 2 — the surfaces

### `inventory/admin.py`

- `dante_unit_id` joins **both** `NetworkDeviceAddForm.Meta.fields` and
  `NetworkDeviceChangeForm.Meta.fields`. Both are explicit lists, and `construct_instance()` silently
  drops anything left out — the trap already documented on the add form's `Meta`.
- `NetworkDeviceAdmin.get_form()` appends the live suggestion to
  `form.base_fields["dante_unit_id"].help_text`. **Mutate only the class `super().get_form()`
  returns** — `ModelAdmin.get_form()` builds a fresh class through `modelform_factory()`, whose
  metaclass rebuilds `base_fields` with new field instances per call, so appending there cannot
  leak across requests. Appending to `NetworkDeviceChangeForm.base_fields` directly *would* leak,
  and the symptom is a help string that grows by one sentence per page load. Guarded by a test
  that issues the same admin GET twice (review note 4). One aggregate query for the highest
  assigned ID; the full ID set is fetched **only** when that highest is 127.
- **Both advisory and warning are computed in `_post_clean()`, after `super()._post_clean()` —
  not in `clean()`** (review note 1, which corrected revision 1's reasoning as well as its code).
  Revision 1 said to compute in `clean()` "against the stored row, not `self.instance`, which
  `_post_clean()` has already mutated". The lifecycle is the other way round: `full_clean()` calls
  `_clean_form()` (which runs `self.clean()`) **before** `_post_clean()`, so at `clean()` time
  `self.instance` still holds the *old* values and `cleaned_data` holds the *un-normalized* new
  ones — the form's `CharField` strips whitespace but never lowercases; `hostname` is lowercased by
  `NetworkDevice.clean_fields()`, which only runs inside `_post_clean()`.

  Comparing at `clean()` time is therefore wrong in a way that fires on real input: an operator
  retyping `MPS-RIO3224D3-1` over the stored `mps-rio3224d3-1` changes nothing once normalized, but
  a raw comparison sees a rename and warns about a Dante outage that is not happening. Computing
  after `super()._post_clean()` means comparing the fully-normalized `self.instance` against the
  stored row, so normalization can never manufacture a difference.

  Stashing (not raising) in `_post_clean()` is safe: a form that fails model validation never
  reaches `save_model()`, so a stashed message on an invalid form is simply never emitted.

  Applies to **both** forms, for one hook and one comparison rule:
  - `NetworkDeviceChangeForm` (which has no `clean()` or `_post_clean()` today) stashes
    `_hostname_advisories` (the over-31 advisory, where the unit ID is null — settled decision 6)
    and `_dante_warnings` (the rename warning — settled decision 8).
  - `NetworkDeviceAddForm` stashes the advisory only. Its `clean()` still runs
    `_fill_computed_hostname()` as today — assembly has to happen there, since `cleaned_data` is
    what `construct_instance()` reads — but the *advisory* is measured afterwards, off the
    normalized instance. No rename warning on creation (settled decision 8).
- `_emit_dante_warnings(request, form)` beside `_emit_hostname_advisories()`, emitting at
  `messages.warning` rather than `messages.info` — this is a hazard, not a suggestion. Called from
  `NetworkDeviceAdmin.save_model()`.
- `recompute_hostnames` (the **device** admin's copy only — the switch admin's is untouched, settled
  decision 6) gains, inside the existing per-row `transaction.atomic()` block:
  - capture `original_hostname` before `_recompute_hostname()`;
  - after it, where `dante_unit_id` is not null and the assembled name exceeds 31, **skip** —
    `continue` without `current.save()`, appending a reason naming the computed name, its length, the
    assembled length and the limit (settled decision 7);
  - where the row was renamed and carries a unit ID, emit the rename warning (settled decision 8);
  - where the unit ID is null and the new hostname exceeds 31, emit the advisory.

  `_recompute_hostname()` itself stays model-agnostic and unchanged — it is shared with
  `NetworkSwitchAdmin`, which must not grow any of this.
- `list_display` gains `dante_unit_id`; `get_readonly_fields()` adds `dante_device_name` for an
  existing object that carries a unit ID (settled decision 9).

### `inventory/views.py` and templates

- **`ROADMAP.md`'s phase 22 closing paragraph is corrected** (review note 7). It currently reads
  "an ordinary Dante device with no unit ID and an over-long hostname gets no warning"
  (`ROADMAP.md:543`), which contradicts phase 22's own bullet six lines earlier requiring exactly
  that advisory (`ROADMAP.md:507`) — and mis-states the ADR, whose remaining gap is **uniqueness**,
  not length: *"the advisory fires on length only. Nothing checks that an un-flagged Dante device's
  name is unique on the Dante network."* The paragraph is rewritten to name the uniqueness gap.
  Documentation only; no behaviour changes, and no ADR is amended — this makes ROADMAP agree with
  the ADR it is summarising.
- `REGISTRY["networkdevice"]`: `dante_unit_id` joins `list_columns` and `detail_fields` (the latter
  never renders — the spec redirects to `device_detail` — but the two are kept in step, as the Rack
  entry's own note requires).
- `device_detail.html`: in the Placement panel's meta line, a Dante block rendered **only** when
  `device.dante_unit_id` — the unit ID and the derived name. Where the hostname is blank so the name
  is `None`, it says what is missing rather than printing an em dash alone.
- No new queries: `dante_device_name` reads two columns already on the loaded row.

### Tests (PR 2, `inventory/test_ui.py` and `inventory/tests.py`)

- The advisory fires on the add form and on the change form for a >31 hostname with a null unit ID,
  and does **not** fire for a switch.
- The rename warning fires for each row of decision 8's table and stays silent for the two "no" rows.
- Recompute, covering every branch the action now has (review note 3):
  - warns for a renamed unit-ID device; **silent for an unchanged one** (recompute that produces
    the same name is not a rename and must not claim an outage);
  - **skips** the over-limit unit-ID device, and asserts the stored row is untouched by
    `refresh_from_db()` on **all three** fields `_recompute_hostname()` mutates — `owner`,
    `hostname_sequence` *and* `hostname`. Asserting the hostname alone would pass while the action
    silently persisted a recomputed owner or sequence;
  - a **null**-ID device whose recomputed name exceeds 31 **saves**, and emits the advisory — the
    skip is conditional on the unit ID, and an unconditional skip would be a regression in phase
    18's behaviour;
  - an assembled name of **exactly 31** saves;
  - a **mixed batch** — one skipped row among rows that rename — proves the skip neither prevents
    later rows from saving nor distorts the renamed/unchanged/skipped counters.
- **The suggestion is never written** (review note 4) — the single most important consequence of
  settled decision 2, and untested in revision 1. Submitting the add form *and* the change form
  with a blank `dante_unit_id` persists `NULL`, not the suggested value. Every other suggester in
  this codebase fills a blank field, so this is exactly the regression a future contributor would
  introduce by making this one "consistent".
- The help text is asserted through **rendered admin GETs** on both the add and change forms, and
  the same GET issued **twice** shows the suggestion once — the accumulation guard (review note 4).
- The suggester reaches the operator: help text carries `Next free unit ID: 3`; the reclaim wording
  appears at 127; the exhausted wording appears when all 127 are assigned.
- `device_detail` renders the derived name where an ID is set and omits the block entirely where it
  is not; the parity list page renders the ID column.
- Both forms actually persist `dante_unit_id` — the `Meta.fields` omission trap, asserted rather
  than assumed.

## Consequences accepted, not solved

**The unit-ID race loses the operator's form input, and shows them a raw database error.**
(Review note 5, accepted rather than fixed.)

Two operators assigning the same unit ID at the same moment can both pass `full_clean()` — the
uniqueness query and the `INSERT` are not one atomic step. The loser's `save()` then violates
`unique_device_dante_unit_id`. That does **not** 500: `AuditedModelAdminMixin.changeform_view()`
(`inventory/admin.py:78`) catches `IntegrityError`, emits `messages.error(f"Could not save: {exc}")`
and redirects. So the operator sees `Could not save: (1062, "Duplicate entry '1' for key
'unique_device_dante_unit_id'")` and loses what they typed.

Accepted, for three reasons:

- **Integrity is never lost**, which is the constraint's entire job — settled decision 4 asked for
  a backstop, and the backstop holds. The friendly error still covers every non-racing case, which
  is all of them outside a genuine simultaneous submit.
- **Translating an `IntegrityError` into a field error means matching on a MySQL error string or
  constraint name.** Nothing in this codebase does that anywhere, and introducing the pattern here
  would put a driver-message dependency behind a rare path — a fragile surface, in exchange for
  better wording in a race between two operators over a field that today has two legitimate users
  in the entire estate.
- **The failure is loud and non-destructive.** Nothing wrong is written; the operator is told the
  save failed and can retype. The bad outcome this decision guards against is a *silent* duplicate,
  and that outcome is impossible.

Recorded here rather than left to be rediscovered. If unit IDs ever become high-traffic — a Shure
receiver estate, say — this is the first thing to revisit.

## Risks and what could still be wrong

- **Both new rules are unreachable on live data**, so nothing in the estate exercises them. The
  longest hostname is 19 against a 26-character budget — a 7-character margin that no live row is
  anywhere near. That is not a reason to soften either rule (ADR 0023's scheme can build 33 from
  parts already present), but it does mean the tests are the *only* thing standing behind them, and
  a test that quietly stops constructing an over-long name would fail silently. Each such test
  should assert the length it built, not just the outcome.
- **The uniqueness constraint is stricter than any vendor documents.** ADR 0024 says so explicitly
  and argues the cost is one wasted number out of 127. Recorded here because it is the one decision
  that would be expensive to reverse once IDs exist.
- **Nothing checks that an un-flagged Dante device's name is unique on the Dante network.** The ADR
  names this gap; it closes for free if ADR 0021's VLAN role ships in phase 21, with no schema
  change. Not this phase's work.
- **Two knowing divergences from the ADR's wording** — settled decisions 6 and 8, both flagged
  above, neither amending the record.

## After merge

- **Migrate the dev database before implementing** — it is three migrations behind, and it is what
  the test suite builds `test_na9k_*` from:

  ```bash
  set -a; source .env; set +a
  python manage.py migrate
  ```

  The deployment database is already current and needs nothing; `0019` reaches it the usual way.
- Mike sets the Rio's and the Tio's unit IDs by hand, against the front panels.
- `ROADMAP.md`: mark phase 22 done and move the header, which still reads "Current phase: 19" and
  will then need to name 19–21 as the outstanding ones.

## Review response

Findings from `REVIEW-1-PLAN-adr-0024.md` (independent `codex` review of revision 1, reasoning
effort high). No P0 findings. Every finding was checked against the cited code before being
accepted — including the two that corrected revision 1's *reasoning* rather than its conclusion.

| Note | Sev | Resolution | Section |
|---|---|---|---|
| 1 — change-form hook uses the wrong lifecycle order; comparing un-normalized `cleaned_data` produces a false rename warning on a case-only edit | P1 | **Folded, and revision 1's stated reason was wrong.** `_clean_form()` runs `self.clean()` *before* `_post_clean()`, so at `clean()` time `self.instance` holds the old values and `cleaned_data` the un-normalized new ones. Both the advisory and the warning now compute in `_post_clean()` after `super()`, off the normalized instance. No-op, unrelated-field and case-only tests added | PR 2 → `inventory/admin.py` |
| 2 — PR 1 tests do not prove the validation boundaries or error accumulation | P1 | **Folded in full.** Four cases added: 26 accepted / 27 rejected with the built length asserted; a raw write of 128 rejected, not only 0; self-exclusion on an unchanged own ID; both field errors reported together | PR 1 → Tests |
| 3 — recompute verification underspecified; "leaves the row untouched" must cover all three mutated fields | P1 | **Folded in full.** The skip test now refreshes and asserts `owner`, `hostname_sequence` and `hostname`; added the unchanged-device silence case, the null-ID >31 saves-and-advises case, the exactly-31 case, and a mixed batch proving a skip does not block later rows or distort counters | PR 2 → Tests |
| 4 — the "suggest, never fill" behaviour has no negative test; help text untested through real renders and could accumulate | P1 | **Folded in full.** Blank-submits-as-NULL asserted on both forms; help text asserted through rendered admin GETs, with the same GET issued twice to catch accumulation. The plan now also names *which* class may be mutated (the one `modelform_factory()` returns) and why that is safe | PR 2 → `inventory/admin.py`, Tests |
| 5 — the race path preserves integrity but shows a raw DB error and loses form input | P2 | **Half folded, half rejected — argued.** The diagnosis is correct and is now recorded explicitly under "Consequences accepted, not solved". The suggested translation is **declined**: it requires matching a MySQL error string or constraint name, a pattern this codebase has nowhere, in exchange for wording in a race over a field with two legitimate users estate-wide. Integrity holds; the failure is loud and writes nothing | New section |
| 6 — reclaim help text asserts facts the system cannot know | P1→P2 | **Folded.** "Every unit ID up to 127 is assigned" is false in exactly the state that triggers the message, and nothing retains the history proving a gap was ever used. Reworded to claim only what is known | PR 1 → Operator-facing strings |
| 7 — `ROADMAP.md` phase 22 contradicts itself on the advisory | P3 | **Folded.** Confirmed: line 543 says a null-ID over-long hostname "gets no warning"; line 507 requires exactly that advisory. The ADR's remaining gap is *uniqueness*, not length. ROADMAP's paragraph is corrected in PR 2. Documentation only — no ADR is amended | PR 2 → `inventory/views.py` and templates |

**Confirmed by the review, not changed:** the sequencing holds and PR 1 lands independently; every
cited `file:line` and symbol is real (`models.py:586`, `models.py:3573`, `suggestions.py:89`,
`views.py:1166`, `config/settings.py:314`); the change form genuinely has no `clean()`; recompute
genuinely saves without `full_clean()`; the auditlog registration is genuinely a whitelist; the
derived-name ordering produces all four rows of decision 1's table and can emit neither a
trailing hyphen nor a bare `Y001`; the recompute skip is safe against a disposable per-iteration
instance; and no phase 18 invariant breaks — a Dante name is a separate namespace and correctly
stays out of `hostname_is_taken()`, `choose_sequence()` and `hostname_diverges`.
