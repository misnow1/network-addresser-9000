# Things That Would Be Nice To Have

This document attempts to list some things that would be nice to have that may already be documented elsewhere but I'm just sort of thinking out loud right now. Note that these items aren't necessarily in any meaningful order.

Whatever we plan, remember that this entire tool is designed to be used by people who know A/V and production but not networking. All created elements should be straightforward, well-documented with plain language, and easy to use.

> **Scoped 2026-08-06.** Every item below now has a home in `ROADMAP.md`, annotated under each
> heading. The framing decision for that pass was **model work before any further UI work**, so
> all three UI items are parked behind one gate (ADR 0020 decision 2) rather than scattered.

## Javascript

It's unavoidable, isn't it? We need some live UI goodness to really make this shine.

> *→ `ROADMAP.md` Later, behind ADR 0020 decision 2.* Worth knowing this is really two features:
> JS as **progressive enhancement of the read-only views** needs no `POST` and doesn't touch
> ADR 0020 at all, while JS as a **write surface** amends it and takes on everything read-only
> was chosen to avoid. They can land separately.

## Department Name

VLANs should have a Department name. It may make sense to tag other entities - racks, materialized devices, etc. - in the future but I don't think that's necessary right now. The Department can be an enum or an fk'd table. It will include names and associations like:
* Audio (VLANs 200-207)
* Lighting (VLANs 100-101)
* Video (VLANs 220-221)

> *→ `ROADMAP.md` phase 16.* An FK'd table, not an enum — no code branches on the value, and a
> new department must not need a migration and a redeploy. Optional `PROTECT` FK from VLAN,
> descriptive only. Your instinct to leave racks and devices untagged held; the one place the
> pass went further was pinning **role** (Control / Dante Primary / Dante Secondary) in the same
> ADR, scoped per-department, because departments are what give role its correct uniqueness
> scope. Role itself ships with phase 21.

## Hostname Computation (parked in roadmap, issue #31 I think)

Hostnames should be computed using the following dash-joined component:
1. The owner such as mps or bej.
2. The location, if the item is in a physical rack such as "wpcsrl" or "w8lm1sl". Items in a virtual rack or address pool like "console" or "avio" do not use this component.
3. The device type such as "ik42" or "sg300-10mp"
4. An optional free-form field describing the purpose of the device such as "midhi-01-04" or "sub" (for amps that drive Mid-Frequency and High-Frequency speakers 1 through 4 or Subwoofers, respectively)
5. An optional field which is a squential value for devices with identical names such as 1 or 2 for mps-avio-aes-1 and mps-avio-aes-2, respectively

For related devices, the device should use the parent's hostname plus a suffix indicating the address' purpose. For example "-engine" for the Digico console engine or "-device-control" for the Yamaha device control port.

This differs from the production export in the following ways:
1. The rack name was *always* enforced.
2. The Digico SD12 names also included -control for the control port.

Hostnames are computed at materialization but are not immutable. Hostnames must be unique and users must be presented with an error if they are about to cause a hostname collision. Collisions should not be possible across racks since rack name uniqueness is enforced. The following rules may also be used:
* In virtual racks (address pools), the 5th field should be incremented if the 4th field is unused or cannot be used to avoid a collision. If this is the first collision, and the first device does not have a 5th field assigned, set the new device's value to 2 and recommend to the user that the existing device be assigned 1.
* In a physical rack, the 4th field should be used to avoid collisions.

> *→ `ROADMAP.md` phases 17 (ingredients) and 18 (computation).* Four corrections came out of
> scoping this:
>
> 1. **It is not blocked on #30.** Issue #31 says it is, because #31 imagined a per-slot label
>    only a populated rack template could supply. Your component scheme needs nothing from #30 —
>    #31 gets rewritten.
> 2. **"Rack name uniqueness is enforced" is not true today** — `Rack.name` has no `unique=True`.
>    Rather than dedup live rack names, component 2 becomes a new optional `Rack.location_slug`,
>    unique where non-blank. Blank means "no location component", which gets your virtual-rack
>    rule without adding the rack purpose field `CONTEXT.md` and ADR 0019 rule out.
> 3. **"Hostnames must be unique" contradicts ADR 0018 decision 3**, which copies the host's
>    hostname verbatim onto a companion. Your own `-device-control` suffix rule is the fix, so
>    phase 18 amends that ADR.
> 4. **`-engine` had nowhere to live** — ports have no hostname field. It becomes a derived,
>    read-only property from a `hostname_suffix` on the type port, mirroring how ADR 0017 already
>    treats that same port's *address*.
>
> Also: the two physical-rack collision rules aren't automatable (the system can't invent
> `midhi-01-04`, and can't act on an already-saved twin), so they ship as advisory messages while
> the sequence auto-increments everywhere.

## Rack Creation Wizard (related to roadmap phase )

I'd like a wizard to help with creating new racks!

This UI would ask things like:
* Physical rack or virtual address pool?
* How many spaces or addresses?
* Which VLAN(s)?

A live UI showing the proposed address space(s) and a physical representation of the rack/pool with addresses would be even more awesome!

This materializes an empty rack and the required address pools. The next step is to add switches:

* Which model?
* How are they connected to VLANs?
* Slots 1-*n*? Or elsewhere?

Materialize the switches and update the UI. Now add devices:

*document wizard flow for new devices here*

> *→ `ROADMAP.md` Later.* The most expensive single item in this document: a multi-step write
> flow (needs ADR 0020 v2 above it) that also materializes equipment (needs #30, populated rack
> templates, below it). Both halves are undesigned.

## Other CSV Import

Other departments are working on rack layouts. I can get a copy of those so we can evaluate the shape and what is needed for import.

> *→ `ROADMAP.md` Later, blocked on obtaining the sample exports.* Nothing to design until the
> shape is in hand.

## Password Change Form (parked in roadmap)

The admin should be able to create users that require a new password (or have none set). The user should receive an email with a link to set up their account and password. The user should also be able to request a password reset email. This may require Django SMTP configuration which should be documented in the setup/deployment instructions.

> *→ `ROADMAP.md` Later, behind ADR 0020 decision 2.* The roadmap already carried the narrow
> version (a Viewer can't change their own password); this widens it to account setup and reset
> emails, and the SMTP-configuration note is now recorded there.

## Container Build and Deployment Instructions (roadmap phase 7)

This is definitely already on the roadmap!

> *→ `ROADMAP.md` phase 7, still unchecked.* Left in place and out of order rather than
> renumbered, so it stays visible that it was scoped and then skipped.

## Switch Config Generator

A method to export a text file that can be imported into a switch's administrative interface. This requires more work to store port configuration and other VLAN- and switch-level configs.

> *→ `ROADMAP.md` Later, not a phase.* Your own caveat is the reason: the port-level and
> switch-level configuration storage doesn't exist, and it overlaps multicast (#22). The next
> step is a data-model investigation, not an implementation plan.

## User Documentation

Users will want things to read! Bonus points to pictures of the UI included!

> *→ `ROADMAP.md` Later,* deliberately deferred until the write UI exists so screenshots are shot
> once. The accepted cost is recorded there: phase 15 already moved every Viewer out of the admin
> and into a UI they have never used, and they stay on an undocumented interface until then.
