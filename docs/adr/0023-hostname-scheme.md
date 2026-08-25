# Hostnames are assembled from five stored components

`docs/MORE_MUSINGS.md` asks for computed hostnames:

> Hostnames should be computed using the following dash-joined component:
> 1. The owner such as mps or bej.
> 2. The location, if the item is in a physical rack such as "wpcsrl" or "w8lm1sl". Items in a
>    virtual rack or address pool like "console" or "avio" do not use this component.
> 3. The device type such as "ik42" or "sg300-10mp"
> 4. An optional free-form field describing the purpose of the device such as "midhi-01-04" or
>    "sub"
> 5. An optional field which is a squential value for devices with identical names such as 1 or 2
>    for mps-avio-aes-1 and mps-avio-aes-2, respectively

Three of the five components have no representation in the data model at all. This ADR settles the
whole scheme — the ingredients *and* the computation and collision rules — before either ships,
because the two halves constrain each other and settling them separately would mean discovering in
phase 18 that phase 17 shipped the wrong field types. That is the ADR 0021 pattern: decide both
axes on paper, build one at a time.

`ROADMAP.md` phase 17 builds the fields; phase 18 builds the behaviour. Issue #31 tracks both.

This ADR does not need to amend ADR 0018 — the companion whose hostname copied its host's verbatim
no longer exists. ADR 0022 superseded it outright, and the Yamaha Device Control interface that
forced the original question is now a *port* on its console carrying `hostname_suffix`.

> **Amended 2026-08-16, while planning and reviewing phase 18.** Six blocks below are corrected in
> place, each marked **Amendment**:
>
> - **Decision 6** — uniqueness is **rename-only**: enforced when an existing row's hostname
>   changes, not asserted as an invariant and not enforced on creation
> - **Decision 7** — bump-until-free is underspecified, and wrong in two reachable cases
> - **Decision 8** — on-write normalisation cannot close the casing divergence on its own; a
>   backfill is required. Its no-validator half survives on new reasoning
> - **Decision 10** — `hostname_slug` *is* seeded after all, and has since largely been set by hand
> - **Consequences** — "those tests need no change" is wrong; lowercasing breaks eight assertions
>   and the production verifier
>
> Every one was found by measuring the **live database** rather than the CSVs this ADR was written
> from — three of them only after an independent review of the phase 18 plan.
>
> **A seventh, made 2026-08-17, during phase 18's implementation rather than its planning.**
> Decision 7's starting-value table gained a zeroth row after an independent code review of the
> built code (not the plan) found that self-exclusion alone does not make recompute idempotent for
> the bare-named member of a numbered group — see that decision's second amendment block, below the
> first.
>
> **An eighth, made 2026-08-17, from a live-admin report after phase 18 shipped.** Decision 7's
> starting-value table gained a `hostname_purpose` column: a blank purpose now starts numbering at
> **1** unconditionally rather than reserving 1 for the twin advisory, which raises the production
> reproduction rate from 42/52 to 49/52. The two advisory messages were also reworded — Django
> admin's `capfirst` was capitalising a lowercase hostname at the front of every advisory string,
> and neither message named the row it was about — see decision 7's third amendment block.
>
> **Health warning on the evidence below.** Every number in the next section was measured against
> `prod/*.csv`. The deployment database has since diverged — 49 hostnames have been renamed by hand
> into scheme shape and every prose value is gone — so re-deriving these figures from the CSVs
> gives answers that are right about the export and wrong about the system. Where a decision turns
> on a count, the live figure is given alongside.
>
> **Amended 2026-08-24, by `docs/adr/0026-device-model-entity.md` PR 2.** Decision 1's table below
> named `hostname_slug` as living "on both Type models". That stopped being true on the device side:
> the field moved off `NetworkDeviceType` onto `NetworkDeviceModel` (ADR 0026 decision 2, PR 2), the
> same model-not-profile reasoning as that ADR's `description` field, but blocking here rather than
> cosmetic — a divergent slug between two profiles of one model used to silently compute different
> hostnames for identical hardware, and now can't, since there is only one value to read. The table
> row is corrected in place rather than left describing a schema that no longer exists. The switch
> side is unaffected — `NetworkSwitchType.hostname_slug` stays exactly as this ADR describes it,
> until issue #78 aligns the two Type models.

## What the production data actually shows

Every decision below was tested against `prod/MPS Audio Network Standards - Dante Devices.csv`,
which turned out to carry the component scheme as *columns*: `Owner`, `Location`, `Model`,
`Additional Field 1`, `Additional Field 2`, and an assembled `Dante Device Name`. All 52 rows
assemble exactly as `'-'.join(non-empty components).lower()`, with no exceptions.

Four findings from that pass shaped the decisions, and they are recorded here because each one
contradicts something previously written down.

**The scheme reproduces production, but the two "Additional Field" columns are a spreadsheet
artifact.** Modelled as *one* free-form purpose plus *one* integer sequence — the roadmap's scheme
exactly as written — all 52 names reproduce. Production never uses purpose and sequence together
(30 rows purpose-only, 19 sequence-only, 3 neither), so the sheet's two columns are one component
split for typing convenience, not two components.

**A rack-level location cannot be per-device, and does not need to be.** 46 of 52 rows take their
location from something that is straightforwardly a rack. Of the rest, four were a source-data
defect (below) and two — `mps-foh-dm7c-1` and `mps-stage-rio-1`, both resident in the `CONSOLES`
address pool — encode where the console physically sits rather than which rack holds it. Those two
belong to a different namespace (see "What this scheme is not"), not to hostnames.

**Virtual racks are not uniformly location-free.** `MORE_MUSINGS.md` says items in a virtual rack
or address pool "do not use this component", and `PLAN-hostname-ingredients.md` decision 4 repeated
that as the rationale for a blank `location_slug`. The data disagrees: `AVIO` and `SPARE` are both
address pools and both contribute location components (`mps-avio-avio-aes-1`, `mps-spare-ik42-1`).
Only `CONSOLES` is blank. The decision — optional, rack-level — is right; the reason given for it
was wrong. The question the field asks is *"does this rack have a location name?"*, not *"is this
rack virtual?"*, exactly as the roadmap's own wording has it.

**A source-data defect worth recording.** The Dante sheet places `W8LM #1` at location `W8LM2SR`
and `W8LM #2` at `W8LM1SL`, while the addressing sheet has racks `W8LM1SR` and `W8LM2SL`. The sides
agree; the digits are transposed. The addressing sheet is authoritative, because it follows the
pattern the WPC racks establish — SR numbered top to bottom, then SL top to bottom
(`WPC1SRU`, `WPC2SRL`, `WPC3SLU`, `WPC4SLL`) — which puts the SR unit at 1 and the SL unit at 2.
The importer reads the addressing sheet and needs no change. Anyone re-comparing the two sheets
will hit this again.

## Decision

### 1. Five components, three skippable and two blocking

| # | Component | Field | Absent |
|---|---|---|---|
| 1 | owner | `Owner` FK on `NetworkSwitch` / `NetworkDevice` | **blocks** |
| 2 | location | `Rack.location_slug` | skipped |
| 3 | type | `hostname_slug` on `NetworkSwitchType`, and on `NetworkDeviceModel` since ADR 0026 PR 2 (amended 2026-08-24, see above) | **blocks** |
| 4 | purpose | `hostname_purpose` on `NetworkSwitch` / `NetworkDevice` | skipped |
| 5 | sequence | `hostname_sequence` on `NetworkSwitch` / `NetworkDevice` | skipped |

Skipped means the component contributes nothing and the remaining components still assemble.
Blocks means no hostname is computed at all.

Owner blocks because the roadmap requires that *"component 1 is never skipped"*, and the field is
nevertheless optional so that no existing row needs backfilling. Both hold only if a missing owner
means no computed name rather than a name missing its first component — and a name whose first
component has silently become the location is worse than no name, because nothing on the wire says
which reading is right.

`hostname_slug` blocks for the reason already settled: blank means *"this Type offers no computed
hostnames"*, which is unreachable if blank ever auto-fills.

### 2. Location lives on `Rack` and only on `Rack`

No `location_slug` on equipment, and no per-device override. The alternative — a free-form location
string per device — was rejected on the grounds that it is precisely the free-text identity field
issue #10 documents the cost of, and it would let two devices in one rack disagree about where they
are with nothing to reconcile them.

Assembly therefore reads `device.rack.location_slug`. Spare-pool equipment has no rack
(`CONTEXT.md`, "Spare Pool") and so contributes no location component, which is correct: it is not
anywhere.

This is the one place the scheme deliberately cannot reproduce production, and it costs two rows.

### 3. `hostname_sequence` is an integer

`PositiveIntegerField`, nullable. This is the only choice that makes decision 7's *"bump the
sequence until the name is free"* a defined operation — there is no next value after `01-04`.

Production's second additional-field column holds both `1`, `2`, `3`, `4` and `01-04`, `05-08`,
`13-16`. The non-numeric ones are purposes and belong in `hostname_purpose`, which reproduces them
character-for-character; an operator who types `01-04` into the sequence field is told to put it in
purpose, which is where it already effectively is. Production never zero-pads in the sequence
position, so nothing is lost.

`hostname_purpose` is `CharField(63)`, blank-able, `validate_dns_label`-validated, stripped and
lowercased on write.

### 4. Assembly fills a blank hostname and never overwrites a typed one

At creation, a hostname is computed only if the operator left the field blank. A hand-typed value
is stored verbatim, and the components are stored alongside it unused.

This is ADR 0019's suggest-don't-lock and ADR 0013's `port_addressing` applied to names, and it is
what makes decision 7's uniqueness error meaningful: because the computed path bumps until it finds
a free name, the only way to produce a duplicate is to type one, which is exactly when an operator
should be stopped.

Once stored, a hostname is never re-derived automatically. That is ADR 0003's rule for static
addresses, restated in #31.

### 5. Assembly runs in the add forms and the recompute action, nowhere else

A helper — `inventory/hostnames.py` — called from `NetworkDeviceAddForm.clean()`,
`NetworkSwitchAddForm.clean()`, and an explicit "Recompute hostname" admin action. Not from
`save()`, not from `clean()`, not from the importer.

The add forms are where this belongs because it is already where suggestions live: the `rack_slot`
lowest-free-ordinal fill (`admin.py`, ADR 0019) and the rack-derived owner default are three lines
away. The decisive constraint is that the two advisory messages in decision 7 are
`messages.info()` calls, which need a request that `save()` does not have.

Keeping it out of `save()` also means programmatic `objects.create()` and the importer stay inert,
so no existing test changes behaviour and decision 10's "no backfill" is enforced structurally
rather than by remembering.

**The recompute action fills a blank owner from the rack before computing.** The add-form owner
default never fired for already-imported rows, so without this every production device would be
permanently blocked on a null owner. Doing it in the action rather than in assembly keeps the
value *stored* rather than inherited: an operator asked for it, it is written once, and it stays
overridable. `compute_hostname()` itself never reads through to the rack for owner.

### 6. One `hostname_is_taken()` predicate, spanning three tables, forward only

Uniqueness is checked across `NetworkSwitch.hostname`, `NetworkDevice.hostname` **and** the derived
`NetworkDevicePort.hostname` (ADR 0022 decision 4), in `full_clean()`, with a plain-language error,
blank exempt. The same predicate is what the sequence bump asks "is this name free?".

Including port hostnames is not tidiness. The collision is reachable through the *computed* path:
a console named `mps-avio-sd12` with an `engine` port suffix has a derived port hostname
`mps-avio-sd12-engine`, and a separate device with purpose `engine` computes to the same string.
Purpose is free-form operator input, so nothing prevents it. Without port names in the predicate,
the claim that computation always yields a free name is simply false.

This is the same shape as the existing cross-table static-address check and inherits its known
race (#5), rather than introducing a second, stricter mechanism for names. Blank-exempt means the
spare pool and every existing row need no backfill.

**Forward only.** Renaming a device changes its ports' derived names, which could collide with
something else; that cascade is not validated. See "Known gaps".

> **Amendment — uniqueness is enforced on *change*, not asserted as an invariant.**
>
> As written above this decision is unshippable. The live database holds **32 equipment rows across
> 5 duplicated hostnames** — `IK42` alone names 17 amps, because the importer gave every instance of
> a model the same bare model name. Validating unconditionally in `full_clean()` would make all 32
> unsaveable, so an operator editing one amp's serial number would be refused over a hostname they
> did not create.
>
> The escape hatch does not exist either: the fix is decision 5's recompute action, which is blocked
> on `hostname_slug`, which **no** Type carries. The estate would be duplicated *and* frozen, with
> nothing in the product able to resolve it.
>
> So `full_clean()` runs the check **only when an existing row's `hostname` changes** — that is,
> when `pk is not None` and the value differs from what is stored. Renaming *into* a duplicate is
> refused; the computed path still never collides, because the bump uses the same predicate; editing
> any other field on an already-duplicated row saves cleanly.
>
> **Creation is deliberately exempt**, which is narrower than this amendment first claimed. The
> importer creates duplicates by design — `import_prod_data.py` commits every row to
> `construct → full_clean() → save()` and writes `hostname = row.description`, and the addressing
> CSV repeats `IK42` eighteen times — so enforcing on new rows would break a rebuild and every test
> in `test_prod_import.py` with it. The guard loses little: the computed path cannot collide, so a
> hand-typed **rename** is the realistic way to create a duplicate, and that is still refused.
>
> The honest consequence: **hostnames are not unique in the database, and no code may assume they
> are.** Nothing branches on a hostname, so nothing does. All 32 are amps and processors still
> carrying the bare model name the importer gave them, and they are exactly what recompute is for
> once slugs are seeded (decision 10 as amended). A sixth duplicate — two switches in WPM1SR slots 1
> and 2 both named `mps-wpm1sr-sg350-1` — was a data error rather than an import artifact, and was
> corrected by hand before this phase started.

### 7. Collisions bump the sequence, and two advisories ride along

The computed path increments `hostname_sequence` until `hostname_is_taken()` says the name is free,
in physical and virtual racks alike, and never blocks a save.

Two messages are advisory only, because neither is automatable:

- Where a twin exists with no sequence, recommend assigning it `1`. This is a message about an
  already-saved row, not an action on it.
- Where the rack has a `location_slug`, note that a purpose reads better than a number.
  `MORE_MUSINGS.md`'s *"in a physical rack, the 4th field should be used to avoid collisions"* is
  guidance: the purpose is free-form operator input (`midhi-01-04`, `sub`) that the system cannot
  invent.

Production already follows the first of these by hand — `mps-spare-ik42-1` and `mps-foh-dm7c-1` are
singletons that were given `1` anyway — which is why it is worth saying out loud rather than
enforcing.

> **Amendment — "increment until free" is underspecified, and wrong in two reachable cases.**
>
> Bump-until-free picks the lowest free value, which gives the wrong answer where
> `MORE_MUSINGS.md` is explicit: *"If this is the first collision, and the first device does not
> have a 5th field assigned, set the new device's value to **2** and recommend to the user that the
> existing device be assigned 1."* Naively, `…-1` is free, so the new device would take it —
> producing a bare/`-1` pair that reads as two unrelated devices, where production is uniformly
> `-1`/`-2`.
>
> It is also silent about the case where the bare stem is free but numbered siblings exist. Once an
> operator takes the first advisory and numbers the original `1`, a *third* identical device
> computes the bare stem, finds it free, and takes it — leaving `-1`, `-2` and a bare name. Ordinary
> hardware reaches this: production has four Amphenol outputs numbered `1`–`4` and three NA2
> D-lines numbered `1`–`3`.
>
> The starting value is therefore chosen before the free-check loop runs:
>
> | State of the stem | Start at |
> |---|---|
> | nothing exists | no sequence — take the bare name |
> | bare name exists, no numbered siblings | **2**, leaving `1` for the advisory |
> | any numbered sibling exists | **highest + 1** |
>
> then increment until `hostname_is_taken()` says free, which remains the correctness guarantee —
> the table only decides where to start.
>
> **Highest + 1, never lowest-free**, so a gap left by a deleted device is not reused. A hostname
> that has been in service is referenced by things this system cannot see — DNS, switch configs,
> the label on the box, someone's notes — and handing it to different hardware makes all of them
> silently wrong. A gap in the numbering is cosmetic; resurrection is a fault.

> **Amendment — the table above needs a zeroth row, or recompute is not idempotent for the
> bare-named member of a numbered group.**
>
> Self-exclusion (decision 3 of the plan's settled decisions) removes the object being computed
> from the sibling scan, which is right for a *numbered* device — excluding a device already named
> `…-3` stops it from counting its own suffix as evidence. It is not enough for the *bare*-named
> device in the same group: with it excluded, the highest **remaining** sibling looks like the
> group's own top, so the bare device is started at `highest + 1` and renamed to a numbered suffix
> — then, on the next recompute, excluded again, sees a new "highest remaining" one lower, and is
> renamed again. Seventeen identical devices recomputed twice is enough to reach this: the bare one
> is bumped to `…-18` on the second pass, a name nothing held before.
>
> | State of the stem | Start at |
> |---|---|
> | the object's own current hostname already fits this stem (bare, or `stem-<digits>`) and, excluding the object itself, nothing else holds it | **that name, unchanged** |
> | nothing exists | no sequence — take the bare name |
> | bare name exists, no numbered siblings | **2**, leaving `1` for the advisory |
> | any numbered sibling exists | **highest + 1** |
>
> The zeroth row is checked first and short-circuits the rest of the table when it applies — the
> other three rows are otherwise unchanged, including the free-check loop that follows. It is
> reachable only when the caller already found `hostname_sequence` null (decision 6 already
> exempts a non-null one from this whole table) but the *hostname text* nonetheless still matches
> the stem — exactly the bare-name case, since a numbered name normally carries its own number in
> the field too. Found by an independent code review of the implementation, not the plan.

> **Amendment — a blank `hostname_purpose` starts numbering at 1, not 2, and the two advisories
> were reworded.** Reported from the live admin after phase 18 shipped, not found in planning.
>
> **The measurement.** Against all 52 production hostnames, the table above (start at 2 when a bare
> name exists, leaving 1 "for the advisory") reproduces 42. Numbering from 1 unconditionally
> reproduces **49**. Every one of the 10 misses under the old rule is the same shape — a
> purpose-less group such as `mps-avio-amph-output`, whose first member production names
> `mps-avio-amph-output-1`, never bare. Production's own convention, in other words, is that a
> purpose-less name is *always* numbered, starting from 1, whether or not it turns out to have
> siblings — not that the first member of a group is left bare until a second one shows up.
>
> **The rule is conditional on `hostname_purpose`, not a wholesale replacement.** Applying "start
> at 1" regardless of purpose would turn `mps-wpc1sru-ik42-sub` into `mps-wpc1sru-ik42-sub-1` and
> break the 30 purpose-carrying production rows that are correctly bare today — decision 1's
> "purpose and sequence are independently meaningful" already established that a purpose-carrying
> name has no need of a number. So the starting-value table above gains a column:
>
> | State of the stem | `hostname_purpose` blank | `hostname_purpose` set |
> |---|---|---|
> | current hostname already fits this stem and nothing else holds it | *(see below — the bare half of this row no longer applies when blank)* | that name, unchanged |
> | nothing exists | **1** | no sequence — take the bare name |
> | bare name exists, no numbered siblings | **1** | 2, leaving 1 for the advisory |
> | any numbered sibling exists | highest + 1 | highest + 1 |
>
> The blank-purpose column collapses to one rule in practice: **1 unless a numbered sibling already
> exists, in which case highest + 1** — a bare twin existing doesn't change the answer, because
> nothing yet holds the literal `stem-1` string, so 1 is genuinely free. Highest + 1 is kept rather
> than always taking the lowest free value for the same reason decision 7's first amendment gives:
> a gap left by a deleted device must not be handed to different hardware.
>
> **The zeroth row's bare half is now conditional on purpose too, or existing bare names would
> never renumber.** The zeroth row above exists so a device's own numbered name survives
> self-exclusion (settled decision 3's idempotence requirement) unchanged. Left unconditional, it
> would do the same for a *bare* name — but the new blank-purpose rule says a bare, purpose-less
> stem should carry `1`, not stay bare, so honouring it as "unchanged" would permanently preserve
> exactly the shape this amendment exists to renumber. **Settled: existing bare names renumber.**
> The bare half of the zeroth row therefore only fires when `hostname_purpose` is set — a
> legitimate answer there, unchanged — and falls through to the ordinary derivation when blank. The
> *numbered* half is unconditional either way, which is what keeps a blank-purpose row that has
> already taken `-1` idempotent on its next recompute: `mps-avio-aes` computes to `mps-avio-aes-1`
> on the first run, and the second run reproduces it rather than excluding it from the sibling scan
> and deriving a new, higher number.
>
> **The two advisories were reworded, for a reason unrelated to the numbering rule.** Django admin
> renders every message through `capfirst`
> (`django/contrib/admin/templates/admin/base.html`), which uppercases the message's first
> *letter* — wrong for a hostname, a DNS label that is always lowercase, so the twin advisory was
> rendering as `"Mps-danrx shares this hostname…"`. Both advisories, plus the explicit-sequence
> advisory (decision 7's original text, "increment the sequence" section), now open with an
> ordinary word `capfirst` may safely capitalise and, per decision 7's own original wording, name
> the row they are about — the purpose advisory names the row being computed, the twin advisory
> names the *other*, already-saved row. This had shipped with no test asserting the purpose
> advisory's text at all.
>
> **The twin advisory's own trigger condition needed the same purpose qualifier.** It used to fire
> whenever the starting value came out to 2 with a bare twin on record. Under the new rule that
> only still happens when `hostname_purpose` is set — for a blank purpose, the bare-name branch
> that used to produce 2 now produces 1 directly, so recommending "assign it 1" is no longer
> reachable through that path, and the only remaining way to reach 2 for a blank purpose is a
> numbered sibling already holding 1 — a case where the advice would be *wrong*, since 1 is already
> taken by something else. The advisory is gated on `hostname_purpose` being set accordingly; kept
> rather than removed, since it is still correct advice for a purpose-carrying stem, but is now
> near-moot for the blank-purpose case it used to exist for.

### 8. `hostname` is normalised on write and capped at 63

`NetworkSwitch.hostname` and `NetworkDevice.hostname` drop from `CharField(255)` to
`CharField(63)`, and are stripped and lowercased in both `clean()` and `save()`, matching every
slug field in the project.

No `validate_dns_label` on these fields. Computed names are legal by construction, since every
component is validated. The only illegal values are eight imported switch rows whose "hostname" is
prose the importer had nothing better to put there — `Cisco SG300-10MP (For Drive Rack Primary)`,
`Used by BEJ for his gear`. Validating would make those unsaveable and give the importer no legal
value to substitute, blocking any operator who edited such a switch for a reason unrelated to what
they were changing. Those rows are exactly what phase 18 exists to replace.

The 63 cap closes the hole `PLAN-hostname-ingredients.md` decision 7 identified: five components
each capped at 63 can assemble to over 300, and `mps-wpcsrl-ik42-sub-1` is a single label with no
dots. It is free — the longest live value is 22 characters.

Lowercasing also closes the divergence `models.py`'s `NetworkDevicePort.hostname` docstring defers
to this phase: the port property starts yielding `dm7c-1-device-control`, matching the addressing
sheet, instead of `DM7C-1-device-control`.

> **Amendment — on-write normalisation cannot close that divergence on its own.**
>
> The paragraph above is wrong as stated. `clean()` and `save()` normalise on **write**, and a row
> nobody writes to is never normalised — so `DM7C-1` sits in the database indefinitely and the port
> property keeps yielding `DM7C-1-device-control`. The divergence closes only if the migration
> rewrites existing rows.
>
> **The migration backfills**: strip and lowercase every non-blank `NetworkSwitch.hostname` and
> `NetworkDevice.hostname`. **40 of 83 live rows change.** Safe to run — no two hostnames collide
> once lowercased, and nothing is near the 63 cap. Irreversible in practice, since the original
> casing is unrecoverable; the reverse operation is a no-op.
>
> The decisive argument is not tidiness. Those 40 rows are going to be rewritten *anyway*, one at a
> time, whenever somebody happens to save an unrelated field on them — so the choice is not whether
> they change but whether they change as one reviewable migration or as 40 unattributable surprises
> spread over months.
>
> The migration must **refuse rather than truncate** if any row exceeds 63 characters. None does
> today, but a silent truncation is data loss and MySQL's error at `ALTER` time names a column and a
> row number rather than the problem.
>
> **Amendment — the no-validator justification has changed, though the decision has not.** The
> eight prose rows cited above no longer exist: every live hostname is a legal DNS label once
> lowercased. The reason to keep `hostname` unvalidated is now the **importer**, whose docstring
> commits every row to `construct → full_clean() → save()` and which writes
> `hostname = row.description` — still prose in the CSVs. A validator would break a rebuild, and a
> switch's hostname is the only human-readable label it has, there being no description field on
> `NetworkSwitch`. Every *input* to computation stays DNS-validated; only the output field is
> permissive.

### 9. `hostname_diverges` — a stateless indicator

A read-only property on both models: every component present, a stored hostname present, and the
two differ. No new field.

This covers #54 — a rack move leaves the previous rack's location baked into a name, with nothing
to say the name no longer describes where the equipment is. Surfaced as a marker in the read-only
UI and an admin list filter.

Stateless deliberately. A `hostname_is_computed` boolean would be more precise, but it has to be
cleared whenever an operator hand-edits a hostname or it starts lying — state that can itself go
stale is what the indicator exists to catch. The residual false positive is a device that was
hand-named *and* has every component filled in; the ADR accepts that noise, which is narrower than
it looks, because a deliberately hand-named device usually sits on a Type with no `hostname_slug`
and is therefore not evaluated at all.

The framing is divergence, not staleness: the property says the name differs from what its
components produce, and does not claim to know which is right.

### 10. No per-device backfill; the importer seeds the vocabulary

`PLAN-hostname-ingredients.md` decision 9 refused a backfill on the grounds that "the spreadsheet
has no owner or location column". That is false — it has both, plus both additional fields. The
conclusion survives for a stronger reason: **the sheet that carries the components cannot be joined
to the sheet the importer reads.** Zero of 52 Dante descriptions appear in the addressing sheet
(the same hardware is described differently in each — `FOH Lake #1` versus `LM44`), and the Dante
sheet's `Slot` column is empty in all 52 rows. There is no join key. Recovering per-device
components would mean committing a hand-written 52-row mapping, which is human inference dressed as
data.

What *does* ship, because it needs no join:

- Two `Owner` rows, `MPS` and `BEJ`, from the sheet's two distinct owner values
- `Rack.owner` on each imported rack — **all `mps`**. Of 52 Dante rows 51 are `MPS`; the single
  `BEJ` row is an unracked console, so `bej` is seeded as vocabulary an operator will need rather
  than because any rack points at it
- `Rack.location_slug` on each imported rack, by **slugifying the rack name, with a small constant
  of exceptions** for the ones that do not slug directly (`FOH Drive #1` → `foh1` and its siblings);
  virtual pools that are not places map to null deliberately. A name that neither appears in the
  constant nor slugs to a legal DNS label is an error, not a silent skip

  > **Since amended (2026-08-18).** The constant's contents drifted from the live estate and had to
  > be re-derived from it. `XE300-1`/`-2` are **`xe300-1`/`xe300-2`**, not the `xe1`/`xe2` this ADR
  > originally recorded: XE300 is a Martin Audio speaker model and this rack holds the amps driving
  > them, so the model number is the meaningful part — and `xe1` would not distinguish it from a
  > future XE500. `CDD` and `CONTROL` are virtual pools like `CONSOLES` and map to **null**; without
  > explicit entries they slugified to `cdd`/`control` and gained a location component they should
  > not have. Both errors were live: `verify_prod_import` was failing on four racks, and a rebuild
  > would have overwritten the operator's chosen slugs. See the note on `HOSTNAME_SLUGS` under this
  > decision — the same drift, caught there before it shipped and here only after.

A rule-plus-exceptions shape rather than a full enumeration, because the importer creates 21 racks
and only 14 appear as a location in the Dante sheet — enumerating all 21 would mean inventing five
slugs with no production evidence behind them. It also lets `verify_prod_import.py` re-derive the
expectation instead of importing the constant, which its module docstring forbids on the grounds
that a check sharing the importer's helper proves nothing.

`NetworkDevice.owner`, `NetworkSwitch.owner`, `hostname_purpose`, `hostname_sequence` and
`hostname_slug` are **not** seeded and stay blank until an operator fills them or runs the recompute
action.

> **Amendment — `hostname_slug` *is* seeded, in phase 18.**
>
> Leaving it blank makes the whole scheme inert. It is a blocking component (decision 1), and **0
> of 33 Types carried one at the time this was written**, so phase 18 as originally specified would ship a feature that computes
> nothing whatsoever on the live estate — every recompute reporting "no type slug" and doing
> nothing.
>
> This is not the backfill decision 10 refuses. That refusal is about *per-device* components,
> and it stands: the sheet carrying them has no join key to the sheet the importer reads. A Type's
> abbreviation has no such problem — Types are already created from a hand-written constant in the
> importer, and the abbreviations are visible in the Dante sheet's `Model` column and in
> `ROADMAP.md`'s own `sg300-10mp` example.
>
> A `{(manufacturer, model): slug}` constant — `("Martin Audio", "IK-42"): "ik42"`, `("Cisco",
> "SG300-10MP"): "sg300-10mp"` — applied by **both** the importer (for rebuilds) and a data
> migration matching on `(manufacturer, model)` (for the database that already exists, which will
> not be rebuilt just to pick these up). Keyed on the model, not the profile, so `IK-42 — with
> Dante Card` and `— without Dante Card` both get `ik42`, as this ADR already requires.
>
> Not `slugify()`: it is wrong for exactly the cases this ADR already documents — `IK-42` → `ik-42`
> where the name in use is `ik42`, and likewise `SQ-5`, `DM7-EX`, `NA2-DLINE`.
>
> **Since amended:** most of this seeding has since been done by hand. 24 of 25 device Types now
> carry a slug; the 8 switch Types and `Neutrik NA2-DLINE` remain blank. Phase 18's constant must
> therefore be *derived from* the live values rather than invented, or a rebuild would silently
> produce a different estate from the one running — several hand-set values are not what a naive
> constant would hold (`plm20q`, `avioao2`, `rio3224d3`). Four Amphenol slugs were entered as
> `rdj…` against models spelled `RJD…`; that transposition is a typo and phase 18 corrects both the
> constant and the four live rows.
>
> `hostname_diverges` will **not** fire widely, contrary to what this amendment first assumed.
> Owner blocks, and phase 17 seeded no per-equipment owner — only 9 of 84 equipment rows have one,
> so 6 rows diverge today. The estate becomes visible to the indicator only as operators run
> recompute, which sets the owner *and* the name in one step, so a recomputed row does not diverge
> either.

### 11. The phase seam is fields versus behaviour

Phase 17 ships schema and nothing that computes: `Owner` and its `PROTECT` FKs,
`Rack.location_slug`, `hostname_slug` on both Type models, `hostname_purpose` and
`hostname_sequence` on both equipment models, the importer seeding, and full read-parity UI.

Phase 18 ships behaviour: `compute_hostname()`, `hostname_is_taken()`, the sequence bump and its
advisories, the cross-table uniqueness validation, the recompute action, `hostname_diverges`, and
one small migration to shrink and normalise `hostname`.

This moves `hostname_purpose` and `hostname_sequence` from where `ROADMAP.md` phase 18 had them.
The seam is cleaner — one phase is a migration with no logic, the other is logic with almost no
migration — and it matches what phase 17 already promised: *"adds them as ordinary optional fields
and computes nothing — each is independently meaningful."* Purpose and sequence are independently
meaningful the moment they exist; an operator can record `sub` as documentation before anything
assembles it.

## What this scheme is not

The `Dante Device Name` column that all of the evidence above is drawn from **is not a hostname**.
It is one of three namespaces that share this vocabulary, and issue #64 covers the other two:

| Namespace | Shape | Case | Uniqueness |
|---|---|---|---|
| Hostname (this ADR) | all five components | lower | global, valid DNS label |
| Dante device name | the same five, but location may be a physical position | lower | within the Dante network |
| Martin Vu-Net name | drops owner and model | UPPER | per model only |

`SPARE-1` appears twice in the Vu-Net column — once for the IK42 spare, once for the IK81 — because
dropping the model makes them collide. A hostname may not do that.

Production has **no hostname column at all**: the importer sets `hostname = row.description` from
the addressing sheet, which is prose. The Dante name is the nearest existing analogue, which is why
it is used here as evidence that the component vocabulary is sound — not as an oracle for what a
hostname should be. Nobody should read the two unreproducible rows as a defect in this scheme.

## Known gaps

- **The rename cascade.** Renaming a device changes every derived port hostname under it, which may
  collide with an existing name. `hostname_is_taken()` is forward only. Validating the reverse
  direction would mean refusing an edit for a reason two tables away from the form the operator is
  looking at.
- **A derived port name can exceed 63 characters** even when the device name does not — a 55-char
  device plus a 15-char suffix is 70. Not checked, for the same forward-only reason. Nothing in
  production comes close: the worst case today is 43.
- **The cross-table uniqueness race (#5)** is inherited, not fixed.
- **#28, the address-side sibling of #54**, stays open. A stored static address surviving a slot
  move is the same *class* of staleness but a different mechanism — it compares against the
  allocator, not the naming code — and pulling that into a naming phase would carry its own
  decisions about derived, offset and operator-set addresses. One UI treatment should eventually
  serve both.
- **Total-length validation cannot be guaranteed per component.** The 63 cap is on the assembled
  result only; five 63-character components remain individually valid.

## Rejected alternatives

**Location on equipment, or on both with a rack-derived default.** This is the shape decision 3 of
`PLAN-hostname-ingredients.md` uses for owner, and it would reproduce all 52 production rows
including the two `CONSOLES` cases. Rejected because a free-form location per device is the failure
mode issue #10 documents, and because those two rows turned out to belong to the Dante namespace
(#64) rather than to hostnames — so the flexibility would have been bought for a case that is not
this feature's to solve.

**`Rack.location_slug` required.** Would guarantee component 2 on every racked item. Rejected
because it cannot deliver what it promises: spare-pool equipment has `rack` null, so the component
is still absent for exactly the rows that most need naming. It would also force a backfill of every
existing rack, block rack creation on choosing a slug, and add a location component to `bej-dm3d`,
whose *shape* is right only because `CONSOLES` is blank.

To be precise about that last one, since it is easy to overclaim: blank `CONSOLES` gets the
**location** component right. The name still does not reproduce end to end, because its owner
component is `bej` and no per-device owner is backfilled — the recompute action fills a blank owner
from the rack, and `CONSOLES` is owned by `mps`, so the first recompute yields `mps-dm3d` until an
operator sets the device's owner. That is the intended behaviour of decision 5, not a defect, but
this ADR should not be read as promising `bej-dm3d`.

**`hostname_sequence` as a `CharField`.** Mirrors the spreadsheet column literally and would let
`01-04` live in either field. Rejected because it makes the bump undefined and creates two
indistinguishable free-form fields with nothing to tell an operator which to use.

**Assembly in `Model.save()`.** Would give programmatic creation a name too. Rejected because the
advisories cannot fire without a request, the bump would run a three-table query inside every save,
and the importer would start naming rows that decision 10 says it must not.

**Falling back to `rack.owner` inside `compute_hostname()`.** Would make every imported device
computable with no action at all. Rejected as inheritance, which `PLAN-hostname-ingredients.md`
decision 3 explicitly declined in favour of suggest-don't-lock — and it would make a rack move
silently change a computed name, which is the staleness class #54 exists to surface, not to create.

**DNS-validating `hostname`.** Rejected on the eight prose switch rows; see decision 8.

**Auto-filling `hostname_slug` by slugifying the model.** Already settled and restated here for
completeness: if blank auto-fills, blank is unreachable, and `slugify("IK-42")` gives `ik-42` where
the name in use is `ik42`. A wrong component nobody read is worse than a missing one.

## Consequences

- **Two production names are not reproducible** by design: `mps-foh-dm7c-1` and `mps-stage-rio-1`
  compute as `mps-dm7c-1` and `mps-rio-1`. Both are `CONSOLES`-resident, and both are Dante names
  rather than hostnames (#64).

- **Four production names are intentionally corrected.** The `devno` placeholder in
  `mps-avio-avio-bt-devno` and three siblings becomes a sequence number, since each is a singleton:
  `mps-avio-avio-bt-1`.

- **Eight imported switch hostnames get lowercased** into prose like
  `cisco sg300-10mp (for drive rack primary)`. Ugly and temporary — they are placeholders that
  phase 18's recompute action replaces.

- **`NetworkDevicePort.hostname` changes case**, from `DM7C-1-device-control` to
  `dm7c-1-device-control`. ADR 0022 PR 1 anticipated this and forbade case-sensitive assertions on
  that property.

  > **Amendment — "those tests need no change" is wrong.** ADR 0022's forbearance covered only that
  > one property, and was not honoured even there: `test_prod_import.py:560` asserts
  > `"DM7C-1-device-control"` exactly. Lowercasing changes *stored* values, so eight existing
  > case-sensitive assertions fail across `test_prod_import.py` and `test_ui.py`, and — worse —
  > `verify_prod_import.py` compares `hostname != description` against the raw CSV (`:624`, `:760`),
  > so it would report a failure for nearly every row. Phase 18 must make those two comparisons
  > case-insensitive and update the eight assertions; the verifier may do this without breaking its
  > independence contract, since it imports nothing from the importer.

- **`sync_roles` must be re-run after migrating**, for the same reason ADR 0021 records: permission
  rows for `Owner` are created by `post_migrate`, and until then no role holds `view_owner` and the
  read-only UI hides Owners from everyone.

- **`Owner` is registered for audit-trail tracking** (ADR 0004). The new fields on the existing
  models are **not** covered automatically: `config/settings.py` registers `NetworkSwitch` and
  `NetworkDevice` with `include_fields` whitelists, which do not pick up new fields, so `owner`,
  `hostname_purpose` and `hostname_sequence` must be added to both lists explicitly or they are
  silently untracked. `Rack` and the two Type models are registered bare and do pick theirs up.

- **The read-only UI's registry-exhaustive tests will fail until updated.** `Rack` and
  `NetworkDevice` both set `canonical_detail_view` (`views.py`), so `model_detail` redirects to the
  hand-written `rack_detail.html` / `device_detail.html` and their registry `detail_fields` never
  render — `Rack.location_slug`, `Rack.owner` and `NetworkDevice.owner` are invisible unless added
  to those templates by hand, and since Stage C moved Viewers out of the admin, invisible there is
  invisible full stop. `RackAddForm.Meta.fields` is an explicit list and will silently drop
  `location_slug`.

- **Issue #31 is the tracking issue** for both phases and needs no further rewriting. Issue #54 is
  closed by decision 9. Issues #5, #28 and #64 stay open, named above.

- **No addressing behaviour changes.** No suggestion, materialization, offset or stored address is
  affected by anything in this ADR, and `DESIGN.md` needs no amendment.
