# Switches materialize their VLAN addresses at creation, from the rack's ranges

Creating a racked device materializes one `NetworkDevicePort` per type port and fills each
one's static address (ADR 0013). Creating a racked switch materializes its
`NetworkSwitchPort` rows and stops — `NetworkSwitch._materialize_ports()`
(`inventory/models.py:2128`) creates L2 ports only. Every `NetworkSwitchAddress` is a
hand-entered inline row.

ADR 0013 closed exactly this problem for devices, on exactly this reasoning ("in practice,
racked devices are statically addressed, so every device creation was followed by
hand-editing each port"), and left the switch side untouched. The production data makes the
cost concrete: every rack has a primary switch in slot 1 and most have a redundant switch
in slot 2, each addressed on Control, Dante Primary, and Dante Secondary. That is roughly
126 address rows to type by hand for a site that is already fully described by its rack
ranges and slot assignments.

## Decision

A `NetworkSwitch` created in a rack materializes one `NetworkSwitchAddress` per
`RackVlanRange` on its rack, each address filled by the existing suggestion path.

Mechanically this mirrors ADR 0013 as closely as the two models allow:

1. **A transient `address_materialization` choice**, never stored — a writable `@property`
   over a private class-level default, for the same mechanical reason ADR 0013 gives:
   Django's `Model.__init__` accepts an unknown keyword only when the name is a field or a
   `property` (`opts._property_names`), so a plain class attribute would make
   `NetworkSwitch.objects.create(..., address_materialization=...)` raise `TypeError`. The
   setter rejects out-of-domain values with a `ValidationError` rather than silently
   falling through. The materialized rows are the only record of what was chosen; a stored
   field would go stale the moment an operator adds or removes an address by hand.

2. **Default is materialize, everywhere** — the admin add form and programmatic
   `objects.create()` alike, per ADR 0013's "a domain rule, not a UI quirk that only the
   UI knows about."

3. **Unracked switches materialize nothing**, whatever the choice says. The spare pool is
   DHCP-configured by definition (`CONTEXT.md`), and `NetworkSwitchAddress.clean()`
   (`:2206`) already rejects a static address on an unracked switch, so this is enforced
   twice over — as ADR 0013 decision 3 does for devices.

4. **Each address row is constructed blank, `full_clean()`ed, then saved**, inside the
   `transaction.atomic()` that `NetworkSwitch.save()` already opens. This is the same
   construct-blank → `full_clean()` → `save()` sequence ADR 0013 specifies for device
   ports and ADR 0014 decision 7 specifies for template-seeded `RackVlanRange` rows. It
   matters for the same reason in all three places: `save()`/`objects.create()` never call
   `clean()`, so a directly-saved row would persist a blank address instead of triggering
   the suggestion. Here it reuses `NetworkSwitchAddress.clean()`'s existing blank-fill
   (`:2211`) and `_validate_static_address()` call verbatim — this ADR adds no address
   arithmetic of its own.

5. **VLANs are taken in `vlan__vlan_id` order**, matching ADR 0014's `_apply_template()`,
   so a failure reports the same VLAN on every replay rather than varying with row order.

6. **Failure rolls the whole switch back.** If any address can't be allocated, the switch
   and every address materialized before it are gone — ADR 0013 decision 5's "never a
   half-configured device," applied to switches.

## The trade-off this ADR exists to record: where the VLAN list comes from

A device's VLAN list is a **hardware** fact. Its `NetworkDeviceType` declares one port per
physical interface, each with its VLAN, and materialization reads that list. An IK-42 with
a Dante card has three interfaces on three VLANs no matter which rack it sits in.

A switch's is not. A switch has one management stack that can hold an address on any VLAN
it trunks, and which VLANs it *should* be managed on is a site decision, not a property of
the hardware. So the two halves of this codebase now derive their VLAN lists from
different places, deliberately: devices from their type, switches from their rack.

The alternative — a VLAN list on `NetworkSwitchType`, giving switches the same
type-declares-it shape as devices — is declined. `NetworkSwitchType` is a *purpose profile
of a hardware model* (ADR 0010, `CONTEXT.md`); its identity is
`(manufacturer, model, name)` and it is meant to be reusable across racks and across
sites. Putting a site's VLAN numbering into it would make every type site-specific,
forcing a new profile per VLAN set and breaking the one thing type profiles exist to do.
The rack's `RackVlanRange` set is the best available statement of "the networks present in
this rack," and it is already required to exist before any address in that rack can be
computed at all.

## Consequences

**A switch gets an address on every VLAN its rack has a range for, whether or not it needs
one.** In the production data the `FLOATSWITCH` rack's TP-Link switches carry Control and
Dante Primary only. Under this decision they will also get a Dante Secondary address if
that rack has a Dante Secondary range. Either the rack legitimately carries only the two
ranges, or the extra address is harmless and can be deleted. This is accepted rather than
solved: the alternative is a per-switch VLAN selection at creation time, which reintroduces
the hand-entry this ADR removes.

**Zero ranges on the rack means zero addresses, and that is not an error.** This is where
switches are *simpler* than devices, and it is why this ADR needs no equivalent of ADR
0013's `_check_static_materialization_possible()` pre-flight. A device type port demands a
specific VLAN, so a missing `RackVlanRange` for it is a genuine contradiction worth a
deliberate error message. Here the rack's ranges *are* the list, so an empty list is
trivially satisfied — a rack with no ranges yet simply produces a switch with no addresses,
exactly as today. The only remaining failure is a collision, and
`_validate_static_address()` already reports those clearly, so there is nothing a pre-flight
would improve.

**The choice is not static-vs-DHCP.** ADR 0013's enum is `PortAddressing.STATIC|DHCP`
because `NetworkDevicePort` has an `is_dhcp` field. `NetworkSwitchAddress` has no such
field, so the switch-side choice is materialize-or-don't and is named accordingly
(`SwitchAddressing.STATIC|MANUAL`, where `MANUAL` means "I will add addresses myself," not
"this switch has no addresses"). Reusing `PortAddressing` would import a `DHCP` value with
nothing to represent it.

## Known gap, pre-existing: a switch cannot be marked DHCP-managed

Because `NetworkSwitchAddress` has no `is_dhcp` field, "this racked switch takes its
management address from DHCP" is representable only as the absence of address rows —
indistinguishable from "nobody has recorded this switch's addresses yet." That asymmetry
against `NetworkDevicePort` predates this ADR and is not introduced by it, but this ADR
makes the absence *meaningful* for the first time (it is now the result of a choice rather
than the only possible outcome), which is what makes it worth recording. Closing it means
an `is_dhcp` field on `NetworkSwitchAddress` and a DHCP-shaped materialization path;
out of scope here.

The cross-table switch/device address race (#5) is likewise untouched — no DB constraint
spans `NetworkSwitchAddress` and `NetworkDevicePort`, so concurrent allocations can still
both validate and collide. As ADR 0013 says of the same gap: this work exercises it more
often without introducing it or worsening it in kind.

## Follow-up

Implementation is a separate, independently reviewed plan, per this project's convention.
It must cover: the default path materializing one address per rack range with correct
`base + slot` values; `MANUAL` materializing none; an unracked switch materializing none
under either choice; a rack with no ranges producing a switch with no addresses and no
error; and a collision rolling back the switch and every address created before it.
