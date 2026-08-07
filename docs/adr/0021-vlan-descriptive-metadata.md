# VLAN carries descriptive metadata: Department now, role designed

`CONTEXT.md` has described a VLAN as flat since the first design pass — an 802.1Q ID plus its
IPv4 addressing, and nothing else:

> A top-level object combining an 802.1Q VLAN ID with its IPv4 addressing (subnet/CIDR, default
> gateway, DHCP range as a start/end address pair). A VLAN and its IPv4 network are the same row.

`docs/MORE_MUSINGS.md` asks for a department name on VLAN:

> VLANs should have a Department name. It may make sense to tag other entities - racks,
> materialized devices, etc. - in the future but I don't think that's necessary right now. The
> Department can be an enum or an fk'd table. It will include names and associations like: Audio
> (VLANs 200-207), Lighting (VLANs 100-101), Video (VLANs 220-221).

This is the third independent request for a field on VLAN, and `ROADMAP.md` carried a standing
"watch item" saying the flat-VLAN position should be reversed **once**, deliberately, when a third
axis arrived — rather than three times by accident, as one feature at a time asks for one field at
a time.

This ADR is that reversal. It ships **Department**, and it settles **role** on paper without
building it, because role's uniqueness scope has to be decided now or phase 21 will get it wrong.

## The two axes are different in kind, and that is the whole design

The reason both are settled here is that they look like the same feature — "descriptive metadata
on a VLAN" — and are not. The distinction is what governs every decision below.

**Department is operator vocabulary.** It is a label operators already use amongst themselves.
No code reads it; nothing branches on it; no address, offset, or allocation depends on it. Its
value set belongs to the people using the system, and the set is open — a site with a Broadcast
department is not a site this software has to be modified for.

**Role is code vocabulary.** Phase 21's device-type addressing modes branch on it — "which VLAN
is Control here", exactly as the existing code branches on `PortMode`. A role that no code knows
about does nothing at all. Its value set belongs to the software, and it is closed at any given
version: `DESIGN.md`'s Shure Split Mode needs a fourth member ("Shure Control"), and adding it
means writing the mode that consumes it.

That difference is the test this ADR asks future proposed VLAN fields to pass. **Does code branch
on this value?** If no, it is a table and operators own it. If yes, it is an enumeration and the
codebase owns it. Applied here it gives Department a table and role a `TextChoices`, and the
asymmetry between them is deliberate rather than an inconsistency to be tidied up later.

## Decision

### 1. Department is a table, not an enumeration

A `Department` model with a unique, non-blank `name` and an optional `description`.

`MORE_MUSINGS.md` offers "an enum or an fk'd table" and this takes the table, for one reason:
adding a department must not require a migration and a redeploy. `MORE_MUSINGS.md` opens by
naming its own audience — *"people who know A/V and production but not networking"* — and an
enumeration means those people cannot add "Broadcast" without a developer, a code change and a
deployment. That is a dead end for exactly the users the tool is for.

Nothing in the codebase branches on a department's value, so the table costs nothing that the
enum would have saved.

### 2. `VLAN.department` is an optional `PROTECT` FK

Optional, because two populations of rows have no valid department and never will: the
system-seeded VLAN 1 ("Default VLAN"), which is L2-only and belongs to no department, and every
row that exists today, which must remain valid across the migration without a backfill inventing
answers.

`PROTECT` needs more argument than it first appears to, because **this is the codebase's first
purely descriptive foreign key.** The existing 14 `PROTECT` relationships all guard *structural*
facts — a VLAN referenced by a rack range, a Type referenced by an instance — where deleting the
target would leave behind something that no longer computes. Deleting a department leaves every
VLAN perfectly functional. So ADR 0007's "block non-empty containers" rule does not obviously
reach this case, and citing the codebase's habit would be citing a habit.

`PROTECT` is chosen on asymmetric risk instead. Under `SET_NULL`, one delete button silently
strips the label off every VLAN that carried it — recoverable only by reading the audit trail —
and if that behaviour is later judged wrong, tightening it to `PROTECT` breaks a workflow people
have started relying on. Under `PROTECT`, the delete is refused and Django names the VLANs
blocking it; if *that* is later judged wrong, loosening it to `SET_NULL` is harmless and breaks
nothing. Being wrong in the `PROTECT` direction is cheap and being wrong in the other direction
is not.

**Renaming a department is an edit to the existing row, never delete-and-recreate.** That is the
only workflow `PROTECT` obstructs, and it obstructs it in favour of the operation that preserves
the audit trail anyway.

### 3. No system-seeded departments

Unlike the "Default" Switch Port VLAN Profile (ADR 0012) and the "Default VLAN" it points at, no
department is meaningfully a default. `seed_defaults` is untouched, and a fresh database has no
departments until an operator creates one.

### 4. Department names are trimmed; case-folding comes from the collation

`Department.name` is stripped in both `clean()` and `save()`, following `RackTemplate`
(`inventory/models.py`) exactly. Both, not one: `Model.save()` never calls `clean()`, so
`Department.objects.create(name="Audio ")` would otherwise persist trailing whitespace that the
unique constraint treats as a distinct value. Case-insensitivity is free — MariaDB's collation
already folds it, so `"audio"` and `"Audio"` collide on the constraint without any code.

This is issue #10's gap, and this decision keeps a new model out of it. **It does not close #10**,
which is about Type profile identity `(manufacturer, model, name)` and device port
`(device, description)` — untouched here.

### 5. Role is `TextChoices` on VLAN, and it requires a department

Designed here, built in phase 21.

`VLAN.role` is a `TextChoices` field whose members today would be Control, Dante Primary and
Dante Secondary, with "Shure Control" already known to be needed (`DESIGN.md`'s Shure Split Mode)
and expected to arrive with the mode that uses it.

**Uniqueness is per-department, not site-wide.** Audio, Lighting and Video each have their own
Control VLAN; a site-level unique constraint would make the second department's Control VLAN
unrepresentable. Getting this scope right is the entire reason Department ships first — role
designed in isolation would have taken the site-level scope, and correcting it would have cost a
second ADR.

**Setting a role requires a department.** This falls out of the scope decision and is not a
separate preference. "Unique per department" has no meaning for a VLAN with no department, and
the database will not supply one: in MariaDB a `UniqueConstraint(department, role)` permits
unlimited duplicate roles among rows where `department IS NULL`, because NULLs never collide. The
choices were to invent a "no department" pseudo-department, to leave the null group unpoliced, or
to require a department before a role may be set. The third is the only one that leaves the
invariant phase 21 branches on actually true.

VLAN 1 therefore stays department-less and role-less, which is correct: it is L2-only and carries
no addressing for a role to describe.

### 6. Department is visible wherever a VLAN already is, and nowhere else

Admin CRUD, a department list filter on the VLAN changelist, a Department column on the read-only
VLAN list and detail pages, and a department line on the address-map view.

The read-only UI gains a `/models/department/` list and detail page. This is not optional: ADR
0020's read-parity means a Viewer at `is_staff=False` can reach every admin-registered model
through the purpose-built UI, so registering a model in the admin without a registry entry
reopens the gap phase 15 closed.

The Department detail page lists the VLANs in that department. This is the read-only registry's
first inline with no admin counterpart, so it is recorded as a deliberate exception rather than
drift: the admin gets a department `list_filter` on VLAN, the read-only UI has no filtering at
all, and this inline is that filter's equivalent. The parity being kept is of *capability*, not
of markup.

**The index page's VLAN tiles are not regrouped by department.** That is a layout change to a
shaped view, and the 2026-08-06 design pass fixed the framing that model work comes before
further UI work. A column is field display; a regrouping is a redesign.

## Department does not scope allocation

Stated explicitly, so that nobody later reads the existence of a grouping field as permission to
group allocation by it.

`ROADMAP.md`'s aligned-rack-allocation phase gives four reasons for declining department-scoped
alignment. Shipping Department dissolves exactly one of them — that there was no field to scope
by. The other three stand, and the two carrying the weight are:

- All 21 production racks carry only audio VLANs, so department-scoped and global alignment
  produce identical results on the only real data this system has.
- The spreadsheet's own model is one offset per rack applied to *every* VLAN base. Scoping by
  department would depart from current practice, not formalise it.

**This is also not ADR 0014 decision 1 reversed.** That decision declined a *dynamic membership
flag for rack templates* — a mechanism by which a template's VLAN set would be computed from a
property at rack-creation time rather than listed. Department is a descriptive label with no
effect on any allocation path, and no template consults it. The two are neighbours in subject and
unrelated in substance.

## Rejected alternatives

**Department as `TextChoices`.** `MORE_MUSINGS.md` offers it. Rejected by decision 1's reasoning:
it puts a developer, a migration and a deployment between an operator and a new department, for a
value no code reads.

**Department on Rack, NetworkSwitch and NetworkDevice too.** `MORE_MUSINGS.md` raises the
possibility and answers it in the same sentence — *"I don't think that's necessary right now"* —
and nothing in the roadmap contradicts that. A VLAN belongs to a department; a rack routinely
carries VLANs from several. Adding the field to equipment now would mean inventing a rule for
what happens when a device's department disagrees with its VLANs', with no case asking for one.

**A `role` table, symmetric with Department.** Rejected: a role with no code behind it is inert,
so operator-authored roles would create rows that nothing can act on. Branching on a database
string is also strictly more fragile than branching on an enum member, and phase 21's whole
purpose is to branch on it.

**Shipping the `role` field now, nullable and unused.** One migration instead of two, and the
column would be harmless. Rejected because a field nothing writes, validates or reads is a trap
for the next reader, and phase 21 is entitled to refine the design before it becomes schema. The
design is what needed pinning here; the column is not.

**Seeding Audio / Lighting / Video from the production importer.** The eight VLANs it creates map
cleanly onto the three departments, so this looked free. Rejected because **no production CSV has
a department column** — the importer would have to hardcode a function-to-department table and
assert knowledge its input does not contain. Nothing in the import, the arithmetic or the
verifier depends on department. Creating three rows and assigning eight VLANs is a two-minute
task in the admin, and it is an operator's judgement rather than an importer's inference.

**A data migration backfilling departments by `vlan_id` range.** Same objection, worse placement:
it would bake one site's VLAN numbering into the schema history permanently.

## Consequences

- **`CONTEXT.md`'s VLAN entry is no longer accurate as written** and is amended, alongside a new
  Department glossary entry. This is the reversal the watch item was holding out for, and the
  roadmap item is marked resolved by this ADR rather than by the plan that implements it.

- **`sync_roles` must be re-run after migrating.** It enumerates the app's models dynamically and
  needs no code change, but permission rows are created by `post_migrate` after `migrate`
  finishes — so until it is re-run, no role holds `view_department` and the read-only UI, which
  permission-gates every registry page, hides Departments from everyone.

- **`Department` is registered for audit-trail tracking** (ADR 0004). `VLAN` is already tracked
  with all fields, so `department` is covered on the VLAN side with no configuration change.

- **The read-only UI's registry-exhaustive tests will fail until updated.** They assert that every
  registry slug has a query-budget factory and a fixture row. That is those guards working, and
  the failures are the intended notification that a new model needs parity coverage.

- **Phase 21 inherits a settled role design**: `TextChoices`, unique per department, requiring a
  department. What it still owns is the member list, the addressing modes that branch on them,
  and whether the constraint is enforced in the schema or in `full_clean()`.

- **No addressing behaviour changes.** No suggestion, materialization, offset or stored address is
  affected by anything in this ADR, and `DESIGN.md` needs no amendment.
