# Device addresses are stored and editable, not immutable, to support future replacement workflows

> **Partially superseded by [ADR 0017](./0017-derived-same-vlan-addresses.md).** The
> stored-not-derived rule below still holds for every port at slot offset 0, which is every
> port that existed before ADR 0017. What doesn't: a `NetworkDevicePort` materialized at a
> non-zero `slot_offset` has a **read-only** address, derived from the offset-0 port on its
> VLAN and recomputed when that address is edited — because the hardware computes it (a
> DiGiCo console's engine address is always its control address plus one) and offers no way
> to change it. The mutability this ADR argues for is preserved where it matters: the
> offset-0 address stays editable, so the device-replacement workflow below is unaffected.
> Nothing recomputes on a rack or slot move under either ADR.

A device's address within a rack defaults to a computed value (rack range base + slot number) but is persisted as an ordinary field rather than derived at read time. The UI should present the computed value and strongly discourage divergence from it, but the field must stay mutable: a later device-replacement workflow (swapping a spare into an already-occupied, already-addressed slot) needs to carry an existing address onto a new device instance, which a fully immutable address would rule out.
