"""Template helpers for the read-only UI (phase 15, ADR 0020).

Kept to the one thing templates can't do on their own — deriving a stable
display hue from a VLAN id — rather than growing into a second place
business logic lives. Everything else (address formatting, encodings,
query shaping) belongs in ``views.py``, not here.
"""

from django import template

register = template.Library()


@register.filter
def vlan_hue(vlan_id: int) -> int:
    """A stable ``0-359`` hue for a VLAN's chip underline (decision 2's
    "mono chip with a 2px hue underline").

    Deterministic and pure — the same ``vlan_id`` always gets the same
    hue, with no stored color field and no palette to run out of. Not
    cryptographic, just spread: a fixed multiplier taken modulo 360 scatters
    consecutive VLAN ids (201, 202, 203 — exactly how production names
    them) across the color wheel instead of clustering them at one end of
    it, so adjacent VLANs read as visually distinct chips.
    """
    return (int(vlan_id) * 47) % 360
