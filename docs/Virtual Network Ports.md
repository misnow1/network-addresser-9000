# Virtual Network Ports

One thing that the Shure receivers and Yamaha and Digico consoles implementation reveals is a gap in our design. That is, we presently assume that all ports are physical ports on a device (or card) (TODO: phrase this better). However these devices have multiple addresses per physical port. The port for each address can be inferred from the VLAN assignment but this may not hold in the future when we have devices that natively support VLAN tagging like a computer.

Let's consider an example using a Shure ULXD4Q receiver. The device type has 3 defined ports:
* 1: Dante Primary (VLAN 201)
* 2: Dante Secondary (VLAN 202)
* -: Shure Control (VLAN 201, offset address)

We currently track this by not assigning a physical port number to the control address. The same pattern applies for the Yahama Device Control address and the Digico SD12 engine address. Those addresses ride on the same *physical* port as another address on the device. We can infer that since both are VLAN 201 addresses. But there's nothing that explicitly tells a user that they're both physical port 1 on the receiver.

If we want to consider a more complex example, let's take the case of a computer that supports VLAN tagging. In the above example, both ports are untagged and are assumed to be on access ports[^1] on their respective switches. However if we introduce a device that supports native VLAN tagging, things start to get super weird. A physical port can then belong to multiple VLANs and, in that case, the connected switch port would need to have VLAN tagging enabled and configured appropriately for that port.

Is this a huge deal right now? No. Most users won't be connecting directly to equipment like Shure receivers or be concerned with the intricacies of network management. But it would be nice to have in the future.

[^1]: Or on trunk port that only has a Primary VLAN ID (untagged) and no additional VLANs assigned.
