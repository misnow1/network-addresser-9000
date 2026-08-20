"""ADR 0024 / PLAN-adr-0024.md PR 1 — Dante device names and the Yamaha
unit ID prefix.

Pure functions only, importing nothing from the app (plan settled decision
3). ``inventory/hostnames.py`` imports from ``models.py`` and so cannot be
imported back from it, forcing ``models.py`` to carry a duplicate of
``assemble_hostname()`` (``_assemble_hostname_stem``, ``models.py:586``).
This module does not repeat that trap: the Dante derivation rule lives
here, ``models.py`` imports it directly, and there is exactly one copy.
The suggester stays pure by taking the assigned IDs as an argument (the
``lowest_free_run()`` pattern, ``suggestions.py:89``) — the admin does the
query and hands the result in.

No ADR references appear in any string this module builds for display —
those are for operators, not developers; rationale stays in comments like
this one.
"""

from collections.abc import Iterable
from typing import NamedTuple

#: Rio: Y000-Y07F is 0-127; Shure: hex 01-FF is 1-255. The floor is 1, not
#: 0, because Rio's own default is Y001 and nothing in either vendor's
#: documentation treats 0 as a real, assignable ID.
DANTE_UNIT_ID_MIN = 1
#: The intersection of the two vendors' ranges (ADR 0024, "The ranges
#: disagree") — Rio's ceiling, since it's the tighter of the two.
DANTE_UNIT_ID_MAX = 127
#: Audinate's published Dante device-name limit.
DANTE_NAME_MAX_LENGTH = 31
#: The length of "Y0##-" — never used to derive a hostname cap directly
#: (that would silently go stale if the prefix length ever changed), only
#: to explain one arithmetically in an error/advisory message.
DANTE_UNIT_ID_PREFIX_LENGTH = 5


class UnitIdSuggestion(NamedTuple):
    """``suggest_unit_id()``'s result: the suggested value, and whether it
    is a reclaimed (previously-used, now-gap) ID rather than a fresh one.
    """

    value: int
    reclaimed: bool


def dante_device_name(unit_id: int | None, hostname: str) -> str | None:
    """The name to type into Dante Controller (ADR 0024 decision 1's
    table), checked in the order that table requires: a blank ``hostname``
    returns ``None`` **first**, before ``unit_id`` is consulted at all.
    Checking ``unit_id`` first would emit ``Y001-`` for an unnamed device
    — a hyphen-terminated name, which Audinate's own rules forbid — for
    the one case this ordering exists to avoid.

    Otherwise: the bare ``hostname`` where ``unit_id`` is ``None``, or
    ``Y0{unit_id:02X}-{hostname}`` where it is set. ``:02X`` is uppercase
    hex per both vendors' own examples (``Y01B``, never ``Y01b``) — Dante
    name comparison is case-insensitive, so this is presentation, not
    correctness.
    """
    if not hostname:
        return None
    if unit_id is None:
        return hostname
    return f"Y0{unit_id:02X}-{hostname}"


def over_length_advisory(hostname: str) -> str | None:
    """The non-blocking message for a ``hostname`` over
    ``DANTE_NAME_MAX_LENGTH``, ``None`` otherwise.

    Callers own the "only when the unit ID is null" condition (ADR 0024
    plan settled decision 6) — this function knows nothing about unit
    IDs at all, only about a hostname's own length.
    """
    length = len(hostname)
    if length <= DANTE_NAME_MAX_LENGTH:
        return None
    return (
        f"This hostname is {length} characters. Dante's device-name limit is "
        f"{DANTE_NAME_MAX_LENGTH}, so if this device is on a Dante network its name will be rejected."
    )


def length_error(unit_id: int, hostname: str) -> str | None:
    """The blocking message for a ``unit_id``-prefixed name that would
    exceed ``DANTE_NAME_MAX_LENGTH``, ``None`` when the assembled name
    fits. States the arithmetic — assembled length, the limit, the
    prefix's length and what's left for the hostname — rather than just
    asserting a cap, so an operator can see exactly why.
    """
    assembled_length = DANTE_UNIT_ID_PREFIX_LENGTH + len(hostname)
    if assembled_length <= DANTE_NAME_MAX_LENGTH:
        return None
    budget = DANTE_NAME_MAX_LENGTH - DANTE_UNIT_ID_PREFIX_LENGTH
    return (
        f"With Dante unit ID {unit_id} this device's Dante name would be {assembled_length} "
        f"characters. Dante allows {DANTE_NAME_MAX_LENGTH}, and the `Y0{unit_id:02X}-` prefix "
        f"uses {DANTE_UNIT_ID_PREFIX_LENGTH}, leaving {budget} for the hostname."
    )


def rename_warning(
    label: str,
    *,
    old_unit_id: int | None,
    new_unit_id: int | None,
    old_name: str | None,
    new_name: str | None,
) -> str:
    """The audio-outage warning for a change that alters a unit-ID
    device's Dante name (ADR 0024 plan settled decision 8) — one helper,
    so the change form and the recompute action cannot drift apart in
    wording.

    ``old_unit_id`` is accepted for symmetry with the other three values
    (a caller has the full before/after state to hand and shouldn't have
    to work out which parts matter) but isn't itself consulted: every
    wording branch this function needs is already fully determined by
    ``new_unit_id`` (is this device still Dante-controlled?) and whether
    ``old_name``/``new_name`` are ``None`` (was there a previous name? is
    there a name now?).

    - ``new_unit_id`` is ``None`` — the device no longer carries one; the
      first sentence says so instead of naming an ID.
    - ``new_name`` is ``None`` — the hostname is now blank, so nothing can
      be typed into Dante Controller yet; the second sentence names the
      old name as unclaimed rather than describing a name that doesn't
      exist.
    - ``old_name`` is ``None`` — there was no previous Dante name (a blank
      hostname before this change), so the ``(was …)`` clause is omitted
      rather than printing ``(was None)``.
    """
    if new_unit_id is None:
        subject = f"{label} no longer carries a Dante unit ID."
    else:
        subject = f"{label} is a Dante device (unit ID {new_unit_id})."
    if new_name is None:
        detail = (
            f"It has no Dante name until it has a hostname — its old name `{old_name}` is now "
            "unclaimed in Dante Controller."
        )
    else:
        was_clause = f" (was `{old_name}`)" if old_name is not None else ""
        detail = (
            f"Its Dante name is now `{new_name}`{was_clause} — update it in Dante Controller and "
            "rebuild its subscriptions, or audio will not route."
        )
    return f"{subject} {detail}"


def suggest_unit_id(assigned: Iterable[int]) -> UnitIdSuggestion | None:
    """Highest assigned + 1 while the highest assigned ID is below
    ``DANTE_UNIT_ID_MAX`` (ADR 0024 decision 4) — never lowest-free before
    that point, since Dante routes to whatever currently holds a name and
    an automatically-reused ID could silently pull audio from the wrong
    box. ``None`` for an empty ``assigned`` iterable's-not-a-thing case is
    unreachable; an empty ``assigned`` means nothing is taken, so 1 is
    always free and always suggested.

    Once the highest assigned ID reaches ``DANTE_UNIT_ID_MAX`` (127), this
    falls back to the lowest unused value in range, flagged
    ``reclaimed=True`` so the caller can say what's being reclaimed
    (decision 4's "degrading loudly rather than refusing"). ``None`` only
    when every one of the 127 IDs is in use.
    """
    assigned_set = set(assigned)
    if not assigned_set:
        return UnitIdSuggestion(DANTE_UNIT_ID_MIN, False)
    highest = max(assigned_set)
    if highest < DANTE_UNIT_ID_MAX:
        return UnitIdSuggestion(highest + 1, False)
    free = set(range(DANTE_UNIT_ID_MIN, DANTE_UNIT_ID_MAX + 1)) - assigned_set
    if not free:
        return None
    return UnitIdSuggestion(min(free), True)
