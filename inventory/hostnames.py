"""ADR 0023 / PLAN-hostname-computation.md PR 2 — hostname assembly and
collision resolution.

Pure functions only, plus the one predicate they share. Nothing in this
module is called from anywhere yet — that arrives in PR 3 (the add forms'
``clean()`` and the recompute admin action), which is what keeps that PR's
diff readable in isolation from this one.

Two functions, deliberately not one (settled decision 2, plan review note
4): ``assemble_hostname()`` is a pure join over already-known component
values — no queries at all — and ``choose_sequence()`` is the numbering
rule, which does query. ``hostname_diverges`` (PR 4) uses only the former,
so rendering it on every row of a read-only list costs no collision query
— the same objection ADR 0023's rejected alternatives raise against
assembly in ``Model.save()``.

Everything that computes here excludes the object being computed, by pk
and by model, in both ``choose_sequence()`` and ``hostname_is_taken()``.
Without that a recompute would treat a device's own stored name as a
sibling of itself and rename it on every run — settled decision 3,
recompute must be idempotent.
"""

from typing import NamedTuple

from django.db.models import Q, Value
from django.db.models.functions import Concat

from .models import NetworkDevice, NetworkDevicePort, NetworkSwitch


class HostnameComponents(NamedTuple):
    """The five ADR 0023 components, already resolved to plain values —
    never a ``NetworkSwitch``/``NetworkDevice`` instance. On an add form,
    ``self.instance`` is still empty when ``clean()`` runs
    (``ModelForm._post_clean()`` calls ``construct_instance()`` *after*
    ``clean()``), so the only thing that can be passed in is whatever the
    caller already has to hand — ``cleaned_data`` on a form, or plain
    attribute reads on an already-populated instance (the recompute
    action). Building that adapter is each call site's job (PR 3); this
    module doesn't know or care where the values came from.
    """

    #: Component 1 — ``Owner.slug``. Blocking: ``None`` here means
    #: ``assemble_hostname()`` returns ``None`` outright.
    owner_slug: str | None
    #: Component 2 — ``Rack.location_slug``, or ``None`` for a spare-pool
    #: item (no rack) or a rack with no location name. Skipped, not
    #: blocking.
    location_slug: str | None
    #: Component 3 — the Type's ``hostname_slug``. Blocking, same as
    #: ``owner_slug``.
    type_slug: str | None
    #: Component 4 — ``hostname_purpose``. Skipped when blank.
    purpose: str
    #: Component 5 — ``hostname_sequence``. Skipped when ``None``.
    sequence: int | None


def assemble_hostname(components: HostnameComponents) -> str | None:
    """Dash-joins the non-blank components, lowercased inputs assumed
    (every component field is already stripped/lowercased on write —
    ``Owner.slug``, ``Rack.location_slug``, the Types' ``hostname_slug``,
    ``hostname_purpose`` — so no normalisation happens here).

    Returns ``None`` when a **blocking** component — owner or the type's
    ``hostname_slug`` — is missing (ADR 0023 decision 1): a name whose
    first component has silently become the location is worse than no
    name at all. Location, purpose and sequence are simply skipped when
    absent, and dropping every optional component still yields a name —
    the bare ``owner-typeslug`` shape.

    No queries. This is the half ``hostname_diverges`` (PR 4) is allowed
    to call on every row of a read-only list.
    """
    if not components.owner_slug or not components.type_slug:
        return None
    parts = [
        components.owner_slug,
        components.location_slug or None,
        components.type_slug,
        components.purpose or None,
        None if components.sequence is None else str(components.sequence),
    ]
    return "-".join(part for part in parts if part)


def hostname_is_taken(
    name: str, *, exclude_switch_pk: int | None = None, exclude_device_pk: int | None = None
) -> bool:
    """Whether ``name`` is already in use anywhere ``full_clean()``'s
    rename-only uniqueness check or the sequence bump needs to care about
    — ``NetworkSwitch.hostname``, ``NetworkDevice.hostname``, **and** the
    derived ``NetworkDevicePort.hostname`` (ADR 0022 decision 4). Blank is
    never taken — the spare pool and every pre-phase-18 row need no
    backfill.

    Including port hostnames is not tidiness: the collision is reachable
    through the *computed* path. A console named ``mps-avio-sd12`` with an
    ``engine``-suffixed port derives ``mps-avio-sd12-engine``, and a
    separate device with purpose ``engine`` can compute exactly that
    string — purpose is free-form operator input, so nothing else stops
    it. Without ports in this predicate, the claim that computation always
    yields a free name would simply be false.

    Plain ``=``, not ``__iexact`` — every input is already lowercased on
    write, and the columns share ``utf8mb4_uca1400_ai_ci`` collation (ADR
    0023 decision 7, confirmed against the live schema), so an equality
    comparison is already case-insensitive at the database level and can
    use the column's ordinary index. That argument does **not** extend to
    the ``Concat`` annotation below — an annotated expression can't use an
    index at all — but the derived-port table is small enough to accept
    the scan (ADR 0023 decision 7, amended).

    ``exclude_switch_pk``/``exclude_device_pk`` are **not** interchangeable
    with each other: a device being renamed must exclude its own
    ``NetworkDevice`` row, not a switch of the same pk. The port branch
    excludes on ``exclude_device_pk`` alone — a rename must not be blocked
    by its own ports' derived names, which change together with it.
    """
    if not name:
        return False
    switches = NetworkSwitch.objects.filter(hostname=name)
    if exclude_switch_pk is not None:
        switches = switches.exclude(pk=exclude_switch_pk)
    if switches.exists():
        return True
    devices = NetworkDevice.objects.filter(hostname=name)
    if exclude_device_pk is not None:
        devices = devices.exclude(pk=exclude_device_pk)
    if devices.exists():
        return True
    ports = NetworkDevicePort.objects.filter(source_type_port__hostname_suffix__gt="").exclude(
        device__hostname=""  # Concat would yield "-suffix"; the property returns None, never that
    )
    if exclude_device_pk is not None:
        ports = ports.exclude(device_id=exclude_device_pk)
    ports = ports.annotate(
        derived=Concat("device__hostname", Value("-"), "source_type_port__hostname_suffix")
    ).filter(derived=name)
    return ports.exists()


def _sibling_state(
    stem: str, *, exclude_switch_pk: int | None, exclude_device_pk: int | None
) -> tuple[bool, int | None]:
    """Scans ``NetworkSwitch.hostname``/``NetworkDevice.hostname`` (only —
    unlike ``hostname_is_taken()``, this never looks at derived port
    names; a numbered *sibling* is specifically another switch/device
    sharing this stem, whatever its own origin) for whether the bare
    ``stem`` exists and, separately, the highest numeric ``stem-<digits>``
    suffix in use. Returns ``(bare_exists, highest_sequence)``, with
    ``highest_sequence`` ``None`` when no numbered sibling exists at all.

    Filters candidates with an indexed ``startswith`` and does the actual
    digit check in Python, rather than a database regex — this repo's
    columns are small (ADR 0023 decision 7's own argument for accepting an
    unindexed scan elsewhere) and a Python ``str.isdigit()`` check avoids
    relying on MySQL/MariaDB's regex dialect matching Python's.
    """
    bare_exists = False
    highest: int | None = None
    prefix = f"{stem}-"
    for model, exclude_pk in ((NetworkSwitch, exclude_switch_pk), (NetworkDevice, exclude_device_pk)):
        candidates = model._default_manager.filter(Q(hostname=stem) | Q(hostname__startswith=prefix))
        if exclude_pk is not None:
            candidates = candidates.exclude(pk=exclude_pk)
        for hostname in candidates.values_list("hostname", flat=True):
            if hostname == stem:
                bare_exists = True
                continue
            suffix = hostname[len(prefix) :]
            if suffix.isdigit():
                value = int(suffix)
                if highest is None or value > highest:
                    highest = value
    return bare_exists, highest


def choose_sequence(
    stem: str, *, exclude_switch_pk: int | None = None, exclude_device_pk: int | None = None
) -> int | None:
    """The numbering rule (ADR 0023 decision 7, amended): where to *start*
    ``hostname_sequence``, chosen before any free-check runs, then bumped
    until ``hostname_is_taken()`` says the assembled name is free.

    | State of ``stem`` | Start at |
    |---|---|
    | nothing exists | ``None`` — take the bare name |
    | bare name exists, no numbered siblings | **2**, leaving 1 for the advisory |
    | any numbered sibling exists | **highest + 1** |

    Highest + 1, never lowest-free, so a gap left by a deleted device's
    hostname is never handed to different hardware — a retired hostname
    may still be referenced by DNS, switch configs or the label on the
    box, none of which this system can see.

    Only called when ``hostname_sequence`` is not already explicitly set
    on the object (settled decision 6) — that check is the caller's, not
    this function's, since this function has no way to know whether a
    given integer came from an operator or a previous computation.

    Self-exclusion (``exclude_switch_pk``/``exclude_device_pk``) applies
    throughout, including the free-check loop — computing for an object
    that already holds a name in its own stem must return that same name,
    not bump past it (settled decision 3, idempotence).
    """
    bare_exists, highest = _sibling_state(
        stem, exclude_switch_pk=exclude_switch_pk, exclude_device_pk=exclude_device_pk
    )
    if highest is not None:
        sequence: int | None = highest + 1
    elif bare_exists:
        sequence = 2
    else:
        sequence = None
    while True:
        candidate = stem if sequence is None else f"{stem}-{sequence}"
        if not hostname_is_taken(
            candidate, exclude_switch_pk=exclude_switch_pk, exclude_device_pk=exclude_device_pk
        ):
            return sequence
        # Only reachable if the sibling scan above missed a collision —
        # e.g. the bare stem is taken by a *derived port name* rather than
        # a sibling switch/device. Restart at 2, the same "first bump"
        # value the table gives a colliding bare sibling, rather than 1
        # (reserved for the advisory an operator acts on by hand).
        sequence = 2 if sequence is None else sequence + 1
