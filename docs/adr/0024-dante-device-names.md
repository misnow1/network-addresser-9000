# Dante device names, and the Yamaha unit ID that prefixes them

`docs/Shure Devices.md` asked a narrow question: where do we record the `Y0**` Dante name that Shure
receivers need for Yamaha console integration? Answering it turned out to require separating three
things this project has been treating as one.

**A hostname is a convenience.** For most equipment nothing reads it; ADR 0023 computes it so that
operators can identify a box by eye.

**A Dante device name is an identifier the network acts on.** Dante routes audio *by name*. It has
its own rules, and breaking them takes a device off air.

**A Yamaha unit ID is an addressing key.** Yamaha consoles discover and control Dante devices by a
`Y0##` prefix on that name. It is not a naming convention; it is how the console says *which box*.

Treating all three as "the hostname" works until it doesn't, and where it stops working is exactly
the hardware this site owns: Yamaha Rio and Tio stage boxes today, Shure receivers when they arrive.

## What the vendors actually require

Recorded here because three documents had to be read together to get a consistent picture, and two
of them disagree.

**Dante device names** ([Audinate, Dante Controller user guide][audinate]):

- Maximum **31 characters**
- Legal characters are `A-Z`, `a-z`, `0-9` and `-`. No spaces, no underscores, no Unicode
- May not begin or end with a hyphen
- Comparison is **case-insensitive**
- **Must be unique on the network.** On a conflict the loser is renamed to `<old name> (2)` and
  *cannot transmit* until manually renamed
- Renaming is **identity-changing**: *"If a new device or channel is then given the old name, Dante
  routing will route from the new device in place of the previous device"*

**The Yamaha unit ID prefix** ([Yamaha Rio3224-D2 owner's manual][rio]):

> UNIT ID — Specifies the ID of the unit. **Y000(0)–Y07F(127)**, default `Y001`
>
> "Do not change the first five characters of `Y0##-` (## is the UNIT ID). Even if you try to change
> them, they are forcibly corrected back to `Y0##-`." … "A maximum of 31 characters total"

and its troubleshooting table: *"The UNIT ID setting conflicts with another R series unit — Set each
UNIT ID uniquely."*

**The Shure side** ([Shure ULX-D manual][shure], [Yamaha's Shure integration guide][yamaha-shure]):

> "Yamaha: Adds a prefix starting with 'Y' followed by 3-digits to the receiver model name to create
> a device ID that allows Dante enabled Yamaha mixing consoles to discover ULX-D receivers on a Dante
> network. (ex: `Y001-Shure-ULXD`)"
>
> "The Dante network requires unique Dante device IDs to prevent a loss of audio signal routing."
>
> "Changing the Dante ID will cause a loss of audio signal. After an ID has been changed, use the
> Dante controller to restore audio route subscriptions using the new ID."

and Yamaha's guide: *"The device's Dante name needs to be edited so it **begins with** the format
`Y0**`, where ** is a hexadecimal number between 01 and FF."*

### Three things that only emerge from reading all three

**`Y0##-` is a five-character prefix, not the whole name.** Yamaha's guide says "begins with"; Rio
fixes only the first five characters and permits a free tail to 31; Shure's own panel produces
`Y001-Shure-ULXD`, which *has* a tail. So a meaningful name survives the convention — `Y001-mps-
stage-rio-1` is legal and readable, which answers the objection that a bare `Y001` tells an operator
nothing.

**The ranges disagree.** Rio allows `Y000`–`Y07F` (0–127); Shure's Yamaha mode allows hex `01`–`FF`
(1–255). The usable intersection is **1–127**.

**The shared namespace is inferred, not documented.** Rio requires uniqueness among *R-series* units;
Shure requires it among *Shure* devices. Neither says the two share a space. They almost certainly
do — the console addresses by `Y0##`, so a Rio and a receiver both claiming `Y001` gives it two
answers to one question — but this ADR should not claim documentation it does not have. Designing
for one namespace costs a wasted ID number; designing for two risks a control conflict during a
show.

**Rio enforces its prefix; Shure appears not to.** Rio forcibly corrects the first five characters.
Nothing in Shure's documentation says the same. So a Dante Controller rename can silently strip the
prefix from a receiver and break Yamaha discovery with nothing objecting — which is the strongest
argument for this tool *displaying* the name the operator should type.

## Decision

### 1. Only the unit ID is stored; the Dante name is derived

`NetworkDevice.dante_unit_id` — `PositiveSmallIntegerField`, null, range **1–127**. Nothing else is
added. The Dante device name is a **read-only property**:

| `dante_unit_id` | `hostname` | `dante_device_name` |
|---|---|---|
| null | blank | `None` |
| null | set | `hostname` |
| set | blank | **`None`** |
| set | set | `Y0{id:02X}-{hostname}` |

**A blank hostname blocks, even with a unit ID set.** The obvious reading —
`Y0{id:02X}-{hostname}` unconditionally — emits `Y001-` for an unnamed device, and Audinate is
explicit that a name *"cannot begin or end with a hyphen"*, so that is an illegal name generated in
a reachable case. Emitting the bare `Y001` instead would be legal but is precisely the meaningless
string this ADR exists to avoid. So the hostname is a **blocking input**, exactly as ADR 0023
decision 1 treats a missing owner or type slug: a required part is absent, so nothing is computed
and the UI says what is missing.

Deriving rather than storing follows the same reasoning as ADR 0022 decision 4's derived port
hostname: a stored copy would have nothing keeping it in step with the hostname it is built from.
It also matches how this site already thinks about the two names — they *are* the same name — while
making the one real difference (the prefix) explicit rather than something an operator remembers.

`{id:02X}` is uppercase hex, per both vendors' examples (`Y01B`, not `Y01b`). Dante comparison is
case-insensitive, so this is presentation, not correctness.

### 2. Dante's rules bind only devices that carry a unit ID

A unit ID is the opt-in. Where one is set:

- the **assembled name** is validated at **31 characters** — Dante's actual limit
- the derived name is surfaced in the UI

The check is stated against the assembled result, not against a 26-character hostname cap, even
though 26 is what it works out to today. 26 is a derived number with the prefix length baked into
it, and would silently become wrong if a prefix were ever a different length — 31 is the rule Dante
publishes. The error is *raised on the hostname field*, so it lands where the operator is typing,
and states the arithmetic rather than asserting a limit:

> With Dante unit ID 1 this device's Dante name would be 33 characters. Dante allows 31, and the
> `Y001-` prefix uses 5, leaving 26 for the hostname.

Where it is null, nothing changes: the hostname keeps ADR 0023's 63-character cap and this ADR says
nothing about the device.

**No `is_dante_device` flag**, deliberately. A flag would be a second hand-maintained truth that can
disagree with reality, and would need 25 existing Types classified by hand. Deriving the answer
structurally — "does it have a port on a VLAN whose *role* is Dante Primary?" — is what ADR 0021
designed VLAN role for, but role is phase 21 and unbuilt, and matching on a VLAN's free-text name is
the failure mode issue #10 documents. Making the unit ID itself the opt-in needs neither.

**An over-long hostname advises, even with no unit ID.** The tool cannot identify Dante devices
structurally, so it cannot *enforce* Dante's limit on a device that never opted in — but staying
silent about a name it can see will fail is worse. Any hostname over **31 characters** raises an
advisory, whatever its unit ID:

> This hostname is 37 characters. Dante's device-name limit is 31, so if this device is on a Dante
> network its name will be rejected.

Non-blocking: the device saves. Lighting and video equipment is never constrained by an audio
protocol, and nothing is refused for a rule that may not apply to it.

This is reachable now, not theoretical. Composing the longest component values already in the
database — owner `mps`, location `floatswitch`, type slug `rio3224d3`, purpose `midhi-01-04` —
gives `mps-floatswitch-rio3224d3-midhi-01-04`, 37 characters, and 40 with a sequence. Nothing live
is close (the longest hostname is 19), but ADR 0023's own scheme can build it from parts that exist.

**The gap that remains:** the advisory fires on length only. Nothing checks that an un-flagged Dante
device's name is *unique on the Dante network*, because the tool does not know which devices share
one. If ADR 0021's VLAN role ships in phase 21 the population becomes derivable and this closes with
no schema change.

### 3. Unit IDs are unique site-wide, and validated

Dante requires uniqueness "on the network". This system has no Dante-network concept — VLANs are
reachable only through ports, and their *role* is unbuilt — so site-wide uniqueness is the available
approximation. It is stricter than required, which is the safe direction: the cost is a wasted number
out of 127, against a control conflict if two boxes answer to one ID.

Validated in `full_clean()` with a plain-language error, null exempt. Unlike ADR 0023's rename-only
hostname rule, this one applies on **creation as well**, because there is no importer writing unit
IDs and therefore no rebuild to break.

The 31-character limit is **enforced where a unit ID is set**, and merely **advised elsewhere**
(decision 2). Enforced, because exceeding it means Dante refuses the name outright, which is a
different class of consequence from a name that merely reads badly; advised elsewhere, because the
tool cannot tell whether the rule applies.

### 4. The suggester never hands out a retired ID

Suggested value is **highest assigned + 1**, not lowest free. The field stays operator-editable, so
a retired ID can be reclaimed deliberately.

This is ADR 0023 decision 7's rule with a far harder justification. There, reusing a retired
`hostname_sequence` was argued from things the system cannot see — DNS, switch configs, the label on
the box. Here it is mechanical and vendor-stated: Dante routes to whatever currently holds a name,
and Shure warns that changing a Dante ID *"will cause a loss of audio signal"*. An automatically
reused ID could silently pull audio from the wrong box, mid-show, with the routing table looking
correct.

At **127 available and 2 in use** the cost is theoretical for years. When the highest assigned ID
reaches 127 the suggester falls back to the lowest unused value and says so, naming the ID it is
reclaiming — degrading loudly rather than refusing.

### 5. Recomputing a hostname warns when it changes a Dante name

ADR 0023's "Recompute hostname" action overwrites hostnames in bulk. Under decision 1 the Dante name
is *derived from* the hostname, so recomputing a device that carries a unit ID silently changes its
Dante device name — and both vendors state what that costs: Audinate routes to whatever currently
holds a name, and Shure warns that *"changing the Dante ID will cause a loss of audio signal. After
an ID has been changed, use the Dante controller to restore audio route subscriptions."*

Selecting seventeen devices and running the action could therefore take equipment off air, with
nothing in the tool saying so.

The action still renames — that is what it is for — but emits a warning per affected device:

> `mps-stage-rio-1` is a Dante device (unit ID 1). Its Dante name is now `Y001-mps-stage-rio-2` —
> update it in Dante Controller and rebuild its subscriptions, or audio will not route.

Skipping such devices instead was rejected: it would make recompute unusable for exactly the
equipment whose names most need to be right. This is report-don't-enforce, consistent with the rest
of the project, and it reuses the advisory machinery phase 18 already built.

**Neither ADR has this hazard alone.** It exists only where ADR 0023's bulk rename composes with
this ADR's derived name, which is why it is recorded here rather than left for someone to discover.

### 6. Yamaha consoles get no unit ID

The console is the controller doing the discovering, not a discovered device. `DM7C`, `DM7-EX` and
`DM3` carry no `Y0##` prefix. Only controlled equipment does — Rio and Tio stage boxes today, Shure
receivers when they arrive.

## What this does not cover

- **Allen & Heath Avantis integration is unknown.** Recorded as unknown rather than assumed absent;
  if it has its own convention, this ADR's shape should extend to it, but nothing here is designed
  against it.
- **DiGiCo has no equivalent integration** at the time of writing.
- **Dante channel names** (as distinct from device names) have their own rules — any character
  except `=`, `.` and `@`, unique per device — and nothing in this system models channels.
- **Shure Control (WWB6) device IDs** are a second, separate ID space. Shure states duplicates there
  do not affect Dante, and recommends matching the two by convention. This ADR models only the Dante
  one.

## Consequences

- **`NetworkDevice` gains one nullable field and one property.** No migration touches data; no
  existing row changes.
- **Two devices need an ID today** — one Rio3224-D3 and one Tio1608-D2. Neither has one, so neither
  is currently integrated correctly, which this ADR makes visible for the first time.
- **`hostname` gains a conditional 31-character check** — an error where a unit ID is set, an
  advisory everywhere else. The longest live hostname is 19, so nothing breaks today, but ADR 0023's
  scheme can produce 37 from component values already in the database
  (`mps-floatswitch-rio3224d3-midhi-01-04`). Latent in the same way the 63-character cap was before
  phase 18 measured it.
- **ADR 0023's recompute action gains a warning** for devices carrying a unit ID (decision 5). That
  is a change to behaviour shipped in phase 18, not new machinery — the advisory path already
  exists.
- **`sync_roles` must be re-run after migrating**, per ADR 0021's standing note, if a new model is
  added. This ADR adds none, so it is not required.
- **No addressing behaviour changes.** No suggestion, materialization, offset or stored address is
  affected, and `DESIGN.md` needs no amendment.

[audinate]: https://dev.audinate.com/GA/dante-controller/userguide/webhelp/content/device_and_channel_names.htm
[rio]: https://data.yamaha.com/files/download/other_assets/9/1128889/rio3224d2_en_om_e0.pdf
[shure]: https://www.manualslib.com/manual/1149465/Shure-Ulx-D.html?page=20
[yamaha-shure]: https://usa.yamaha.com/files/download/other_assets/8/1198998/Shure_Wireless_with_Yamaha_CL_en.pdf
