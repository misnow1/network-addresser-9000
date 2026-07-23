# Device addresses are stored and editable, not immutable, to support future replacement workflows

A device's address within a rack defaults to a computed value (rack range base + slot number) but is persisted as an ordinary field rather than derived at read time. The UI should present the computed value and strongly discourage divergence from it, but the field must stay mutable: a later device-replacement workflow (swapping a spare into an already-occupied, already-addressed slot) needs to carry an existing address onto a new device instance, which a fully immutable address would rule out.
