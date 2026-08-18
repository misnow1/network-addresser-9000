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

Self-exclusion alone is not sufficient for the *bare*-named member of a
numbered group, though (code review finding 1): excluding that object
from the sibling scan makes the highest *remaining* sibling look like
the group's own top, so ``choose_sequence()`` also takes the object's
current ``hostname`` (``current_name``) and honours it outright when it
already fits the stem being computed and nothing else holds it.
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


def _exact_digit_suffix(suffix: str) -> int | None:
    """Parses ``suffix`` (the text after a hostname's trailing ``-``) as
    the integer it must be to safely stand for a ``hostname_sequence``,
    or ``None`` if it isn't one — used everywhere a ``stem-<suffix>``
    hostname's trailing text gets turned into an int (code review round
    2, finding 2). Two hazards, both closed here:

    - ``str.isdigit()`` is ``True`` for non-ASCII digit characters (e.g.
      ``"²"``) that ``int()`` then raises on — ``isascii()`` first closes
      that.
    - ``int()`` is not a round-trip: ``int("03")`` is ``3``, but
      re-assembling ``f"{stem}-{3}"`` from that produces a *different*
      string (``stem-3``) than the one being validated (``stem-03``),
      and nothing re-checks the reassembled string against what's
      actually stored — so a row holding ``stem-03`` could be "honoured"
      as sequence ``3`` and, if something else already holds ``stem-3``,
      persist a silent, uncaught duplicate; the whole point of
      ``hostname_is_taken()`` checks elsewhere in this module. Requiring
      ``str(value) == suffix`` closes that: only an exact, canonical
      round-trip (no leading zero, no leading ``+``, ordinary ASCII
      digits) is accepted as a numbered sibling or an honourable current
      name at all — a non-canonical suffix like ``"03"`` is treated the
      same as any other non-numeric one.
    """
    if not suffix.isascii() or not suffix.isdigit():
        return None
    value = int(suffix)
    if str(value) != suffix:
        return None
    return value


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
    unindexed scan elsewhere) and a Python check (``_exact_digit_suffix()``)
    avoids relying on MySQL/MariaDB's regex dialect matching Python's.
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
            value = _exact_digit_suffix(hostname[len(prefix) :])
            if value is not None and (highest is None or value > highest):
                highest = value
    return bare_exists, highest


def _bump_until_free(
    stem: str, sequence: int | None, *, exclude_switch_pk: int | None, exclude_device_pk: int | None
) -> int | None:
    """Increments ``sequence`` (``None`` meaning the bare stem) until
    ``hostname_is_taken()`` says the assembled name is free. Shared tail
    for both ``choose_sequence()`` (which computes the *starting* value
    from the sibling scan) and ``resolve_explicit_sequence()`` (which
    starts from whatever the operator already set) — the free-check loop
    itself is identical either way.
    """
    while True:
        candidate = stem if sequence is None else f"{stem}-{sequence}"
        if not hostname_is_taken(
            candidate, exclude_switch_pk=exclude_switch_pk, exclude_device_pk=exclude_device_pk
        ):
            return sequence
        # Restart at 2 (the same "first bump" value a colliding bare
        # sibling gets, not 1 — reserved for the advisory an operator acts
        # on by hand) only coming *from* the bare stem; an already-numbered
        # start just keeps counting up from itself.
        sequence = 2 if sequence is None else sequence + 1


def choose_sequence(
    stem: str,
    *,
    purpose: str,
    current_name: str | None = None,
    exclude_switch_pk: int | None = None,
    exclude_device_pk: int | None = None,
) -> int | None:
    """The numbering rule (ADR 0023 decision 7, twice amended): where to
    *start* ``hostname_sequence``, chosen before any free-check runs, then
    bumped until ``hostname_is_taken()`` says the assembled name is free.

    ``purpose`` is the component being assembled alongside this sequence
    (``stem_components.purpose``/``obj.hostname_purpose``) — **not**
    optional, because the starting value depends on whether it's blank:

    | State of ``stem`` | ``purpose`` blank | ``purpose`` set |
    |---|---|---|
    | nothing exists | **1** | ``None`` — take the bare name |
    | bare name exists, no numbered siblings | **1** | **2**, leaving 1 for the advisory |
    | any numbered sibling exists | **highest + 1** | **highest + 1** |

    Measured against all 52 production hostnames, the purpose-set column
    (numbering from 2, leaving a bare name in reserve) reproduces 42; the
    blank-purpose column (numbering from 1 unconditionally) reproduces 49
    — every one of the 10 misses under the old, single-table rule was the
    same shape, a purpose-less group like ``mps-avio-amph-output`` whose
    first member production names ``…-output-1``, not bare. Applying
    "start from 1" to a *purpose-carrying* stem instead would turn
    ``mps-wpc1sru-ik42-sub`` into ``…-sub-1`` and break the 30
    purpose-carrying production rows that are correctly bare today, which
    is why the two columns diverge only when ``purpose`` is blank.

    Highest + 1, never lowest-free, so a gap left by a deleted device's
    hostname is never handed to different hardware — a retired hostname
    may still be referenced by DNS, switch configs or the label on the
    box, none of which this system can see. This is why a blank-purpose
    stem with a bare name already sitting on it still starts at 1 rather
    than treating the bare name itself as "1 taken": nothing yet holds
    the literal ``stem-1`` string, so 1 is genuinely free, and the bare
    row is expected to renumber to ``-1`` (or further, if 1 turns out
    already taken by a numbered sibling — see the recompute idempotence
    note below) on its own next recompute rather than being treated as
    if it already occupied that slot.

    Only called when ``hostname_sequence`` is not already explicitly set
    on the object (settled decision 6) — that check is the caller's, not
    this function's, since this function has no way to know whether a
    given integer came from an operator or a previous computation.

    ``current_name`` — the object's own currently-stored ``hostname``, if
    any (code review finding 1). Self-exclusion alone is not enough to
    make recompute idempotent for the *bare*-named member of a numbered
    group: excluding that object from the sibling scan makes the highest
    *remaining* sibling look like the group's top, so a bare device would
    be bumped to a numbered suffix on every subsequent run, then bumped
    again past whatever it was bumped to the time before. Before
    deriving a start from the sibling scan at all, this checks whether
    ``current_name`` already fits this exact stem's shape (bare, or
    ``stem-<digits>``) and — excluding this object itself — is not held
    by anything else; if so, it is honoured as-is (the bare name stays
    ``None``, a numbered one yields its own suffix) rather than
    re-derived. Only reachable when the caller already found
    ``hostname_sequence`` null (settled decision 6) but the *hostname
    text* nonetheless already matches this stem — exactly the bare-name
    case, since a numbered name normally carries its number in the field
    too.

    **The bare-name half of that honouring is itself conditional on
    ``purpose`` now.** A bare ``current_name`` is only honoured when
    ``purpose`` is set — a legitimate answer there, unchanged from
    before. When ``purpose`` is blank, the numbering rule above says this
    row should carry ``1``, not stay bare, so a bare ``current_name`` is
    *not* honoured and falls through to the ordinary sibling-scan
    derivation instead — the very case the table's first two rows exist
    to renumber. A *numbered* ``current_name`` (``stem-<digits>``) is
    still honoured either way; that's what keeps recompute idempotent
    once a blank-purpose row has taken its ``-1`` (or higher) on a prior
    run — the second run's sibling scan would otherwise see its own
    current suffix excluded and derive a *different*, higher number
    every time, the same failure mode this parameter was added to close.
    """
    if current_name is not None:
        if current_name == stem:
            if purpose and not hostname_is_taken(
                stem, exclude_switch_pk=exclude_switch_pk, exclude_device_pk=exclude_device_pk
            ):
                return None
        elif current_name.startswith(f"{stem}-"):
            value = _exact_digit_suffix(current_name[len(stem) + 1 :])
            if value is not None and not hostname_is_taken(
                current_name, exclude_switch_pk=exclude_switch_pk, exclude_device_pk=exclude_device_pk
            ):
                return value

    bare_exists, highest = _sibling_state(
        stem, exclude_switch_pk=exclude_switch_pk, exclude_device_pk=exclude_device_pk
    )
    sequence: int | None
    if highest is not None:
        sequence = highest + 1
    elif not purpose:
        sequence = 1
    elif bare_exists:
        sequence = 2
    else:
        sequence = None
    return _bump_until_free(
        stem, sequence, exclude_switch_pk=exclude_switch_pk, exclude_device_pk=exclude_device_pk
    )


def resolve_explicit_sequence(
    stem: str,
    sequence: int,
    *,
    exclude_switch_pk: int | None = None,
    exclude_device_pk: int | None = None,
) -> int:
    """An explicitly-set ``hostname_sequence`` is honoured, not overridden
    (settled decision 4/6): assembly joins it as-is and only bumps if
    ``hostname_is_taken()`` says that exact name is occupied (ADR 0023
    decision 7; code review finding 3) — never asserted as unique
    outright, since "the computed path cannot collide" is the load-
    bearing argument for exempting *creation* from uniqueness at all
    (ADR 0023 decision 6's amendment), and two operators independently
    typing the same ``hostname_sequence`` on twin devices is exactly a
    case that argument has to cover.

    Unlike ``choose_sequence()``, this never consults the sibling-scan
    starting-value table — the operator's own value already *is* the
    starting point; bumping only ever moves forward from it, using the
    same free-check loop.
    """
    resolved = _bump_until_free(
        stem, sequence, exclude_switch_pk=exclude_switch_pk, exclude_device_pk=exclude_device_pk
    )
    # _bump_until_free()'s "None" case is the bare-stem candidate, only
    # ever reached by starting it at None — this function always starts
    # it at the caller's own (non-None) int, so that branch is
    # unreachable here; the assert just satisfies mypy about it.
    assert resolved is not None
    return resolved
