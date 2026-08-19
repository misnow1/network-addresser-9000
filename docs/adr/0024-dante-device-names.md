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

| `dante_unit_id` | `dante_device_name` |
|---|---|
| null | `hostname` (or `None` if the hostname is blank) |
| set | `Y0{id:02X}-{hostname}` |

Deriving rather than storing follows the same reasoning as ADR 0022 decision 4's derived port
hostname: a stored copy would have nothing keeping it in step with the hostname it is built from.
It also matches how this site already thinks about the two names — they *are* the same name — while
making the one real difference (the prefix) explicit rather than something an operator remembers.

`{id:02X}` is uppercase hex, per both vendors' examples (`Y01B`, not `Y01b`). Dante comparison is
case-insensitive, so this is presentation, not correctness.

### 2. Dante's rules bind only devices that carry a unit ID

A unit ID is the opt-in. Where one is set:

- the hostname is validated at **26 characters** — 31 minus the five-character prefix
- the derived name is surfaced in the UI

Where it is null, nothing changes: the hostname keeps ADR 0023's 63-character cap and this ADR says
nothing about the device.

**No `is_dante_device` flag**, deliberately. A flag would be a second hand-maintained truth that can
disagree with reality, and would need 25 existing Types classified by hand. Deriving the answer
structurally — "does it have a port on a VLAN whose *role* is Dante Primary?" — is what ADR 0021
designed VLAN role for, but role is phase 21 and unbuilt, and matching on a VLAN's free-text name is
the failure mode issue #10 documents. Making the unit ID itself the opt-in needs neither.

**The known gap this accepts:** an ordinary Dante device with no unit ID and a 40-character hostname
gets no warning that Dante will refuse the name. Nothing is close today — the longest live hostname
is 19 characters — and the operator who sets a unit ID is the one who gets the check. If VLAN role
ships in phase 21, the population becomes derivable and this gap can close without a schema change.

### 3. Unit IDs are unique site-wide, and validated

Dante requires uniqueness "on the network". This system has no Dante-network concept — VLANs are
reachable only through ports, and their *role* is unbuilt — so site-wide uniqueness is the available
approximation. It is stricter than required, which is the safe direction: the cost is a wasted number
out of 127, against a control conflict if two boxes answer to one ID.

Validated in `full_clean()` with a plain-language error, null exempt. Unlike ADR 0023's rename-only
hostname rule, this one applies on **creation as well**, because there is no importer writing unit
IDs and therefore no rebuild to break.

The 26-character budget is **enforced, not advised** — exceeding it means Dante refuses the name
outright, which is a different class of consequence from a name that merely reads badly.

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

### 5. Yamaha consoles get no unit ID

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
- **`hostname` gains a conditional 26-character validation.** The longest live hostname is 19, so
  nothing breaks — but ADR 0023's own scheme can produce 28 (`mps-wpc1sru-ik42-midhi-01-04`), so a
  device carrying both that shape and a unit ID would be refused. Latent, in the same way the
  63-character cap was before phase 18 measured it.
- **`sync_roles` must be re-run after migrating**, per ADR 0021's standing note, if a new model is
  added. This ADR adds none, so it is not required.
- **No addressing behaviour changes.** No suggestion, materialization, offset or stored address is
  affected, and `DESIGN.md` needs no amendment.

[audinate]: https://dev.audinate.com/GA/dante-controller/userguide/webhelp/content/device_and_channel_names.htm
[rio]: https://data.yamaha.com/files/download/other_assets/9/1128889/rio3224d2_en_om_e0.pdf
[shure]: https://www.manualslib.com/manual/1149465/Shure-Ulx-D.html?page=20
[yamaha-shure]: https://usa.yamaha.com/files/download/other_assets/8/1198998/Shure_Wireless_with_Yamaha_CL_en.pdf
