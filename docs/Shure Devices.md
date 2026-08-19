# Shure Device Addressing

There has already been some discussion but I wanted to clarify that after learning some new things today!

There are two primary concerns:
1. Dante networking
2. Wireless Workbench, Yamaha integration, and Avantis integration (control network)

Shure has an [excellent writeup](https://data.yamaha.com/files/download/other_assets/8/1198998/Shure_Wireless_with_Yamaha_CL_en.pdf) that discusses how to configure the Yamaha integration for Shure wireless receivers that should be consulted as part of this design.

The key takeaway is that for Yamaha integration to work correctly, the Shure control address **must** be in the same subnet as the Yamaha Device Control address. Therefore, the control address **must** also be in the same subnet as the Dante Primary address. This carries the same constraints (and non-constraints) as setting the Yamaha Device Control address on a Yamaha console.

## Dante Networking

Devices must be configured in either `Switched` mode or `Redundant` mode (see DESIGN.md). Configuring devices in `Split` mode would require that both ports be connected to the same switch for integration to work correctly. This is tracked but not strictly enforced in the tooling presently. The "Name" field is a free text field that is used to track with/without Dante card and also the Dante operating mode. We have previousLy discussed changing how we track Dante networking configurations but have not discussed this specifically.

## Shure Networking

As noted above, the Shure control IP address must be in the same subnet as the Yamaha Device Control address. Since the latter's traffic always rides on the Dante Primary network, so will the former's traffic.

## Why we address these statically

Worth stating explicitly, because **the Shure/Yamaha document recommends the opposite** and a reader
who checks the source will otherwise think we have contradicted it. It calls "Automatic" the method
"used most frequently, due to convenience of setup and easy management", and describes Manual as
"rarely used, though offers the potential for tighter security policies in a carefully managed
network". On the Dante side it likewise says to use Automatic "unless your Dante networking strategy
specifically requires manual IP address setting. However, this would be unusual."

We are deliberately the carefully-managed-network case, for three operational reasons:

1. **There is not always a DHCP server on the network.** Automatic addressing cannot be relied on
   when the thing it depends on may not be present.
2. **Link-local convergence is slow.** Falling back to `169.254.*.*` takes long enough to matter
   when you are trying to get a system up.
3. **Automatic addresses are not stable across a restart.** A device that comes back with a
   different address breaks anything that referenced the old one — which is the whole problem this
   tool exists to remove.

Both documents permit the manual path explicitly; we are taking the option they describe rather
than departing from them.

Additionally, the receivers need another setting changed that we don't currently track. As discussed in the linked document:

> The device’s Dante name needs to be edited so it begins with the format “Y0**”, where ** is a hexadecimal number between 01 and FF"

We do not currently track this setting. If this tool is strictly viewed as a network address assignment tool, then this value is outside its scope. However we don't really have a better place to track it right now. The device's hostname sequence number - converted to hexadecimal - could be used as this value by convention. Or we could track it separately. In my opinion, at least presenting the recommdned Device ID value in this tool is useful and appropriate.
