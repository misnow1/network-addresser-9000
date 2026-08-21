# Device Model Description

There presently exists no way to describe what the base version of a device actually does. This can lead to confusion for users who aren't intimately familiar with what devices are. For example, here are four (4) Amphenol devices that do *something* but the the model number doesn't make it obvious:

* Amphenol RJD1212-0050
* Amphenol RJD2203-0050
* Amphenol RJD32A3-0050
* Amphenol RJD32U1-0050

What they actually are:

* Amphenol RJD1212-0050: Dante Interface with 2x Analogue XLR Output
* Amphenol RJD2203-0050: Dante Interface with 2x Analogue XLR Input
* Amphenol RJD32A3-0050: Dante Interface with AES3 I/O
* Amphenol RJD32U1-0050: Dante Interface with USB-A

There are devices where the model number is somewhat instructive such as a the Audinate AVIO-AO2 which has 2x Analogue XLR outputs. Or the Lab Gruppen PLM20000Q which a 4-channel 20,000-Watt Power Amplifier with Lake processing and Dante I/O. The latter isn't entirely informed by the model number. But I digress...

Crucially, the description is an attribute of the model, not of the model profile. The description could be set on the profile but that could lead to drift when a user creates a new profile of an existing device. Using the description to describe a profile should be considered an anti-pattern.

For example, consider the Lab Gruppen LM26. It is a 2-input, 6-output Loudspeaker Processor with Dante. By virtue of having multiple Dante ports, it comes with 2 profiles: Redundant and Switched. The profiles are a property of the deployment but they are both LM26's with the same description.

What we don't want is for users to be able to create two LM26 profiles like this:

| Manufacturer | Model | Description                                                  | Profile   |
| ------------ | ----- | ------------------------------------------------------------ | --------- |
| Lab Gruppen  | LM26  | 2-input, 6-output Loudspeaker Processor with Switched Dante  | Switched  |
| Lab Gruppen  | LM26  | 2-input, 6-output Loudspeaker Processor with Redundant Dante | Redundant |
