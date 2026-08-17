"""Tests for the read-only UI (phase 15, ADR 0020, Stage A).

Lives in its own module rather than growing ``tests.py`` past 6000 lines,
following ``test_prod_import.py``'s precedent (plan decision 9). Fixture
helpers below mirror ``tests.py``'s ``_make_switch_type``/``_make_device_type``
shape exactly, duplicated rather than imported — ``tests.py`` doesn't
export them for reuse, and this module's fixtures are deliberately UI-
shaped (elevation encodings, query budgets) rather than model-invariant-
shaped, so sharing a single helper module would couple two different
concerns for no real benefit.

Four groups of tests, matching the plan's own structure:

* ``UIAccessControlTests`` / ``WritesNothingTests`` — the decorator stack
  (``login_required`` outermost, ``require_GET``, ``permission_required``)
  and ADR 0020's central "writes nothing" claim, proved rather than
  asserted in prose.
* ``ElevationEncodingTests`` — the four encodings, asserted by coordinate
  and with negative controls (review note 7): a wrong-cell pass is exactly
  what a presence-only assertion would miss.
* ``RobustnessTests`` — legal-but-awkward stored data (a malformed range,
  an L2-only VLAN, an ordinal past a block's own address-space capacity)
  must render 200, not 500 (review note 3).
* ``QueryBudgetTests`` — equal query counts across occupancy, which is
  what actually catches an N+1; a single recorded number would only bless
  whatever the implementation happened to do (review note 2).
* ``DeepLinkTests`` — a deep link's target form is actually prefilled, not
  merely reachable (review note 9).
"""

import io
import ipaddress
import re

from auditlog.context import set_actor
from auditlog.models import LogEntry
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.models import User as DjangoUser
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection
from django.db.models import Q
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse

from . import urls as inventory_urls
from .models import (
    VLAN,
    Department,
    NetworkDevice,
    NetworkDevicePort,
    NetworkDeviceType,
    NetworkDeviceTypePort,
    NetworkSwitch,
    NetworkSwitchType,
    NetworkSwitchTypePort,
    Owner,
    PortAddressing,
    PortAddressSource,
    PortMode,
    PortType,
    Rack,
    RackTemplate,
    RackVlanRange,
    SwitchPortVlanProfile,
    switch_port_profile_summary,
)
from .suggestions import suggest_rack_vlan_range, suggest_slot_address
from .views import (
    REGISTRY,
    _content_type_for_model_no_create,
    _object_audit_panel_context,
    resolve_slot_spans,
    safe_slot_address,
)

User = get_user_model()


def _make_switch_type(port_count: int = 0, **kwargs) -> NetworkSwitchType:
    """See ``tests.py``'s helper of the same name — identical shape."""
    kwargs.setdefault("manufacturer", "Cisco")
    kwargs.setdefault("model", "SG300")
    kwargs.setdefault("name", "Default")
    switch_type = NetworkSwitchType.objects.create(port_count=port_count, **kwargs)
    for n in range(1, port_count + 1):
        NetworkSwitchTypePort.objects.create(
            switch_type=switch_type, port_number=n, port_type=PortType.GBE_RJ45
        )
    return switch_type


def _make_device_type(port_count: int = 0, vlan: VLAN | None = None, **kwargs) -> NetworkDeviceType:
    """See ``tests.py``'s helper of the same name — identical shape."""
    kwargs.setdefault("manufacturer", "Martin Audio")
    kwargs.setdefault("model", "IK-42")
    kwargs.setdefault("name", "Default")
    device_type = NetworkDeviceType.objects.create(port_count=port_count, **kwargs)
    for n in range(1, port_count + 1):
        assert vlan is not None, "vlan is required when port_count > 0"
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description=f"Port {n}", port_type=PortType.GBE_RJ45, vlan=vlan
        )
    return device_type


_ALLOWED_READONLY_SQL_VERBS = frozenset({"SELECT", "SAVEPOINT", "ROLLBACK", "SET"})


def _is_allowed_readonly_sql(sql: str) -> bool:
    """Whether ``sql`` is one of the statements a read-only GET request
    cycle may legitimately issue: a ``SELECT``, or one of Django's own
    transaction-bookkeeping statements (``SAVEPOINT``/``RELEASE
    SAVEPOINT``/``ROLLBACK``/``SET ...``) — never a data-mutating one.

    An **allowlist**, not a blocklist of mutating verbs (Codex review
    round 3, finding 1 — the previous revision's blocklist of
    ``INSERT``/``UPDATE``/``DELETE`` had false negatives: MariaDB's
    ``REPLACE ...`` mutates rows under a verb the list never named, and a
    comment-prefixed statement like ``/* tag */ UPDATE ...`` doesn't
    start with any of them either, so both would have silently passed a
    blocklist check while genuinely mutating data). A blocklist is always
    one dialect quirk behind; this fails closed instead — deny by
    default, name only the specific, known-safe shapes a read-only page's
    request cycle can produce, and require an exact match after stripping
    leading whitespace and any leading ``/* ... */`` comment.

    Exists because a row-count sweep alone can't distinguish "nothing
    happened" from "every row was updated in place": an accidental
    ``QuerySet.update()`` leaves every count identical *and* bypasses
    ``Model.save()``, so it never reaches auditlog's signals either
    (Codex review round 2, finding 3). Checking the actual SQL Django
    sent is a check straight off the wire, not an inference from state
    after the fact.
    """
    stripped = sql.strip()
    stripped = re.sub(r"^(?:/\*.*?\*/\s*)+", "", stripped, flags=re.DOTALL).strip()
    if not stripped:
        return True  # nothing to check; an empty capture can't mutate anything
    first_word = stripped.split(None, 1)[0].upper()
    if first_word in _ALLOWED_READONLY_SQL_VERBS:
        return True
    return stripped.upper().startswith("RELEASE SAVEPOINT")


def _row_html(content: str, ordinal: int) -> str:
    """The raw markup for one elevation row, by ordinal — lets a test
    assert an encoding at a specific coordinate (review note 7) instead of
    merely somewhere on the page. Raises if the rack has no such row,
    which is itself a useful failure (a wrong fixture, not a wrong test).
    """
    blocks = re.split(r'(?=<tr id="slot-\d+")', content)
    marker = f'<tr id="slot-{ordinal}"'
    for block in blocks:
        if block.startswith(marker):
            return block
    raise AssertionError(f"no elevation row found for ordinal {ordinal}")


def _cell_states(row_html: str) -> list[str]:
    """The ``cell-<state>`` class of every VLAN column in one row's
    markup, in column order — lets a test assert *which* column carries
    an encoding, not just that the encoding appears somewhere in the row.
    The trailing ``[^"]*`` tolerates ``cell-taken`` (issue #60) riding
    alongside the state class in the same attribute.
    """
    return re.findall(r'<td class="cell cell-(\w+)[^"]*"', row_html)


def _cell_html(row_html: str, column_index: int) -> str:
    """The full ``<td class="cell ...">...</td>`` markup for one VLAN
    column (0-based, in column order) in one elevation row's markup — lets
    a test assert on a cell's *content* (issue #60's taken-by marker
    text), not just its state class.
    """
    cells = re.findall(r'<td class="cell[^"]*">.*?</td>', row_html, re.DOTALL)
    return cells[column_index]


def _clean_text(raw_html: str) -> str:
    """Inner text of an HTML fragment — tags stripped, whitespace
    collapsed — so a rendered value can be compared exactly whether the
    template wrapped it in ``<a>`` or not.
    """
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", raw_html)).strip()


def _detail_field_text(content: str, label: str) -> str:
    """The rendered value for one ``model_detail.html`` "Fields" row, keyed
    by its ``<th>`` label — coordinates, not presence (review note 7):
    proves *which* field rendered *what*, so a column silently dropped
    from ``ModelSpec.detail_fields`` fails loudly here instead of a
    presence check elsewhere passing by accident. Raises if no such field
    row exists, which is itself the failure a dropped column should
    produce.
    """
    match = re.search(rf"<th>{re.escape(label)}</th>\s*<td>(.*?)</td>", content, re.DOTALL)
    if match is None:
        raise AssertionError(f"no detail field row found for label {label!r}")
    return _clean_text(match.group(1))


def _vlan_map_department_line(content: str) -> tuple[str, str]:
    """The ``vlan_map.html`` header's department line — ``(href, text)`` —
    for an exact-value assertion rather than a bare substring/
    ``assertContains`` check (Codex review: a presence-only check on the
    department's name wouldn't have caught the line disappearing from two
    of the page's three states, since the name could still appear
    elsewhere via the fixture). Raises if no department line is present.
    """
    match = re.search(r'<p class="tile__meta">Department:\s*<a href="([^"]+)">([^<]+)</a>', content)
    if match is None:
        raise AssertionError("no department line found in vlan_map header")
    return match.group(1), _clean_text(match.group(2))


def _list_row_cells(content: str, row_marker: str) -> list[str]:
    """Every ``<td>`` in the ``model_list.html`` row containing
    ``row_marker`` (a value unique to that row), stripped and in column
    order — the trailing "Details" link cell is *not* stripped off here;
    callers compare against ``column_labels`` plus one.
    """
    for row in re.findall(r"<tr>(.*?)</tr>", content, re.DOTALL):
        if row_marker in row:
            return [_clean_text(cell) for cell in re.findall(r"<td>(.*?)</td>", row, re.DOTALL)]
    raise AssertionError(f"no list row found containing {row_marker!r}")


def _inline_row_cells(content: str, panel_heading: str, row_marker: str) -> list[str]:
    """Every ``<td>`` in the row containing ``row_marker``, scoped to the
    ``model_detail.html`` inline panel titled ``panel_heading`` — scoped
    so two inlines with similarly-shaped rows (e.g. both an "Addresses"
    and a "Ports" panel showing a VLAN column) can't be confused for one
    another.
    """
    panel_match = re.search(
        rf'<h2 class="panel__heading">{re.escape(panel_heading)}</h2>(.*?)</table>', content, re.DOTALL
    )
    if panel_match is None:
        raise AssertionError(f"no inline panel found for heading {panel_heading!r}")
    for row in re.findall(r"<tr>(.*?)</tr>", panel_match.group(1), re.DOTALL):
        if row_marker in row:
            return [_clean_text(cell) for cell in re.findall(r"<td>(.*?)</td>", row, re.DOTALL)]
    raise AssertionError(f"no row found in inline panel {panel_heading!r} containing {row_marker!r}")


def _audit_row_cells(content: str, row_marker: str) -> list[str]:
    """Cells of one ``audit.html``/``_audit_panel.html`` row, the row
    containing ``row_marker`` — like ``_list_row_cells``, but matching
    ``<tr data-logentry-pk="...">`` (every audit row carries that
    attribute; ``_list_row_cells``'s bare ``<tr>`` wouldn't match it).
    Column order: Timestamp, Actor, Action, Model, Object, Changes.
    """
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", content, re.DOTALL):
        if row_marker in row:
            return [_clean_text(cell) for cell in re.findall(r"<td>(.*?)</td>", row, re.DOTALL)]
    raise AssertionError(f"no audit row found containing {row_marker!r}")


def _permission_for(codename: str) -> Permission:
    """``codename`` is an ``"app_label.codename"`` string, exactly the
    shape every ``ModelSpec.list_permissions``/``detail_permissions``
    entry is written in — the same lookup ``registry_permission_required``
    itself performs via ``has_perms()``.
    """
    app_label, name = codename.split(".", 1)
    return Permission.objects.get(codename=name, content_type__app_label=app_label)


def _user_missing_codename(codename: str) -> DjangoUser:
    """A staff user holding every ``inventory.view_*`` codename plus
    ``auditlog.view_logentry``, except ``codename`` — the minimal way to
    prove a page's declared codename set is a real floor (Stage A's
    ``PartialGrantAccessTests._user_missing`` does the same for the four
    shaped views; this is its Stage B counterpart, generalised over both
    apps' view codenames since the registry's ``detail_permissions`` sets
    span both).
    Idempotent by ``codename`` — several registry entries share the same
    required codename (``view_vlan`` alone appears in five of them), and
    ``test_detail_requires_every_declared_codename`` calls this once per
    (spec, codename) pair, so a second call for the same codename reuses
    rather than re-creates the user (which is also the *correct* fixture:
    the same missing-exactly-this-codename grant must 403 every spec that
    declares it).
    """
    all_view_perms = Permission.objects.filter(
        Q(content_type__app_label="inventory", codename__startswith="view_")
        | Q(content_type__app_label="auditlog", codename="view_logentry")
    )
    missing = _permission_for(codename)
    user, _ = User.objects.get_or_create(username=f"missing-{missing.codename}", defaults={"is_staff": True})
    user.set_password("testpass123")
    user.save()
    user.user_permissions.set(all_view_perms.exclude(pk=missing.pk))
    return user


class ParityFixtureMixin:
    """One of everything the Stage B registry covers, with distinctive
    values throughout (review note 7 — every assertion below names a
    value that could only come from the right place, never a bare
    ``assertContains(response, "VLAN")``). Shared by every Stage B test
    class below rather than rebuilt per class, following Stage A's own
    per-class ``setUp`` pattern but factored out since Stage B's fixture
    is large and used by five different test classes.
    """

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        call_command("sync_roles", stdout=io.StringIO())

        # vlan_id/port_number/rack_slot values are deliberately chosen so
        # none of them is a substring of any other rendered value on the
        # same page (review note 7, sharpened by Codex review's "presence-
        # only" finding) — e.g. vlan_id 4077 shares no digits with subnet
        # "10.210.0.0/21" or default_gateway "10.210.0.1", so a test can
        # assert the VLAN ID column specifically rendered rather than
        # merely that *a* string containing similar digits appears.
        self.department = Department.objects.create(
            name="StageB Grillework", description="StageB Grillework Description"
        )
        self.owner = Owner.objects.create(slug="stageb-owner", name="StageB Ownership")
        self.vlan_native = VLAN.objects.create(
            name="StageB Native",
            vlan_id=4077,
            subnet="10.210.0.0/21",
            default_gateway="10.210.0.1",
            dhcp_range_start="10.210.0.50",
            dhcp_range_end="10.210.0.99",
            department=self.department,
        )
        self.vlan_allowed_1 = VLAN.objects.create(
            name="StageB Allowed One", vlan_id=4078, subnet="10.211.0.0/21"
        )
        self.vlan_allowed_2 = VLAN.objects.create(
            name="StageB Allowed Two", vlan_id=4079, subnet="10.212.0.0/21"
        )

        self.profile = SwitchPortVlanProfile.objects.create(
            name="StageB Profile", port_mode=PortMode.TRUNK, native_vlan=self.vlan_native
        )
        self.profile.allowed_vlans.set([self.vlan_allowed_1, self.vlan_allowed_2])

        self.rack_template = RackTemplate.objects.create(name="StageB Template", slot_count=12)
        self.rack_template.vlans.set([self.vlan_allowed_1, self.vlan_allowed_2])

        self.rack = Rack.objects.create(
            name="StageB Rack", slot_count=10, owner=self.owner, location_slug="stageb-location"
        )
        self.rack_vlan_range = RackVlanRange.objects.create(
            rack=self.rack, vlan=self.vlan_native, address_range="10.210.1.0/27"
        )

        self.switch_type = NetworkSwitchType.objects.create(
            manufacturer="StageB Switch Mfr",
            model="SBSwitchModel",
            name="StageB Switch Type",
            port_count=1,
            hostname_slug="sbswtype",
        )
        NetworkSwitchTypePort.objects.create(
            switch_type=self.switch_type,
            # port_number must be a contiguous 1..port_count sequence
            # (_validate_switch_type_port_profile) — unlike device type
            # ports, this can't be an arbitrary distinctive number, so
            # the description carries the distinctiveness instead.
            port_number=1,
            description="StageB Switch Port Desc",
            port_type=PortType.GBE_RJ45,
            profile=self.profile,
        )
        self.switch = NetworkSwitch.objects.create(
            switch_type=self.switch_type,
            rack=self.rack,
            rack_slot=3,
            hostname="stageb-switch1",
            serial_number="SBSW001",
            dhcp_server_enabled=True,
            owner=self.owner,
            hostname_purpose="stageb-switch-purpose",
            hostname_sequence=42,
        )
        self.switch_port = self.switch.ports.get()
        self.switch_address = self.switch.addresses.get()

        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="StageB Device Mfr",
            model="SBDeviceModel",
            name="StageB Device Type",
            port_count=1,
            hostname_slug="sbdevtype",
        )
        NetworkDeviceTypePort.objects.create(
            device_type=self.device_type,
            port_number=7,
            description="StageB Device Port",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_native,
            slot_offset=0,
        )
        self.device = NetworkDevice.objects.create(
            device_type=self.device_type,
            rack=self.rack,
            rack_slot=5,
            hostname="stageb-device1",
            serial_number="SBDEV001",
            owner=self.owner,
        )
        self.device_port = self.device.ports.get()
        self.device_port.switch_port = self.switch_port
        self.device_port.save()

        # Unracked — materializes DHCP (ADR 0013), for the "default_gateway
        # renders — for a DHCP port" assertion.
        self.dhcp_device = NetworkDevice.objects.create(
            device_type=self.device_type, hostname="stageb-dhcp-device"
        )
        self.dhcp_device_port = self.dhcp_device.ports.get()

        self.admin_user = User.objects.create_user("stageb-admin", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.viewer = User.objects.create_user("stageb-viewer", password="testpass123", is_staff=True)
        self.viewer.groups.add(Group.objects.get(name="Viewer"))
        self.editor = User.objects.create_user("stageb-editor", password="testpass123", is_staff=True)
        self.editor.groups.add(Group.objects.get(name="Editor"))
        self.no_group = User.objects.create_user("stageb-nogroup", password="testpass123", is_staff=False)
        # Stage C's fixture: a Viewer provisioned exactly the way ADR 0020
        # decision 7 says Viewers provision — is_staff=False — so the
        # admin-lockout test can prove this specific, real shape reaches
        # every UI route rather than a staff Viewer that merely happens to
        # be in the right group.
        self.non_staff_viewer = User.objects.create_user(
            "stageb-viewer-nonstaff", password="testpass123", is_staff=False
        )
        self.non_staff_viewer.groups.add(Group.objects.get(name="Viewer"))

        self.pk_by_slug = {
            "vlan": self.vlan_native.pk,
            "department": self.department.pk,
            "owner": self.owner.pk,
            "switchportvlanprofile": self.profile.pk,
            "racktemplate": self.rack_template.pk,
            "rack": self.rack.pk,
            "networkswitchtype": self.switch_type.pk,
            "networkswitch": self.switch.pk,
            "networkdevicetype": self.device_type.pk,
            "networkdevice": self.device.pk,
        }

    def _list_url(self, slug: str) -> str:
        return reverse("inventory:model_list", args=[slug])

    def _detail_url(self, slug: str) -> str:
        return reverse("inventory:model_detail", args=[slug, self.pk_by_slug[slug]])


# ---------------------------------------------------------------------------
# The route enumeration — one source of truth, derived from
# ``inventory.urls.urlpatterns`` (Stage C).
#
# Before this, two hand-maintained route lists existed independently:
# ``UIAccessControlTests._routes()`` (the five Stage A paths) and
# ``ParityAccessTests._routes()`` (audit plus the eighteen registry
# list/detail paths). Together they happened to cover all eight URL
# patterns in ``urls.py``, but "happened to" is exactly the failure mode
# Stage C's admin-lockout test cannot tolerate: that test's entire value is
# being *exhaustive* — it is what certifies that flipping a Viewer to
# ``is_staff=False`` costs them nothing, and a stale list wouldn't fail, it
# would silently certify something false.
#
# ``UrlconfCoverageTests`` (below, in the Stage C section) is the guard —
# but it must check what ``_shaped_routes``/``_parity_routes`` actually
# *produce*, not a fourth hand-written name list sitting next to them
# (Codex review: an earlier revision of this comment introduced exactly
# that — two frozensets that never touched the functions below at all, so
# deleting a route from ``_shaped_routes()`` would leave the frozensets,
# and the guard, unchanged). ``_covered_route_names()`` closes that gap by
# resolving every route the builders actually emit and reading back the
# ``url_name`` Django itself assigns each one.
def _shaped_routes(*, rack_pk: int, vlan_pk: int, device_pk: int) -> list[str]:
    """The five Stage A shaped-view routes."""
    return [
        reverse("inventory:index"),
        reverse("inventory:rack", args=[rack_pk]),
        reverse("inventory:vlan_map", args=[vlan_pk]),
        reverse("inventory:device", args=[device_pk]),
        reverse("inventory:spares"),
    ]


def _parity_routes(*, pk_by_slug: dict[str, int]) -> list[str]:
    """``/audit/`` plus the eighteen Stage B list/detail routes — one pair
    per ``REGISTRY`` slug, so this stays exhaustive as the registry grows
    without anyone touching this function.
    """
    routes = [reverse("inventory:audit")]
    for slug in REGISTRY:
        routes.append(reverse("inventory:model_list", args=[slug]))
        routes.append(reverse("inventory:model_detail", args=[slug, pk_by_slug[slug]]))
    return routes


def _all_ui_routes(*, rack_pk: int, vlan_pk: int, device_pk: int, pk_by_slug: dict[str, int]) -> list[str]:
    """Every concrete route this app serves outside ``/admin/`` and the two
    auth routes — the shaped views plus read-parity plus the audit trail.
    This is what the Stage C admin-lockout test sweeps: the claim it proves
    is that nothing on this list is unreachable once ``/admin/`` is gone.
    """
    return _shaped_routes(rack_pk=rack_pk, vlan_pk=vlan_pk, device_pk=device_pk) + _parity_routes(
        pk_by_slug=pk_by_slug
    )


def _covered_route_names(
    *, rack_pk: int, vlan_pk: int, device_pk: int, pk_by_slug: dict[str, int]
) -> set[str]:
    """The distinct ``url_name`` Django resolves each of
    ``_all_ui_routes()``'s routes to — what the shared builders *actually*
    cover, as opposed to a second hand-written set of names that could
    silently drift from what the builders build (Codex review). Used by
    ``UrlconfCoverageTests`` to check the builders' real output against
    ``inventory.urls.urlpatterns``, rather than checking one hand-written
    list against another.
    """
    routes = _all_ui_routes(rack_pk=rack_pk, vlan_pk=vlan_pk, device_pk=device_pk, pk_by_slug=pk_by_slug)
    names: set[str] = set()
    for route in routes:
        url_name = resolve(route).url_name
        assert url_name is not None, f"resolved route has no url_name: {route}"
        names.add(url_name)
    return names


class UIAccessControlTests(TestCase):
    """The decorator stack on every route: ``login_required`` outermost
    (a logged-out visitor must be redirected to the login page, not 403'd
    — review note 5), ``require_GET`` (POST/PUT/PATCH/DELETE all refused),
    and ``permission_required`` with the view's full codename list, not
    one token model (a partial-privilege user is refused, not just an
    unprivileged one).
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.switch_type = _make_switch_type(port_count=1)
        self.device_type = _make_device_type(port_count=1, vlan=self.vlan)
        self.rack = Rack.objects.create(name="Rack 1", slot_count=10)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.1.0/27")
        self.switch = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=1)
        self.device = NetworkDevice.objects.create(
            device_type=self.device_type, rack=self.rack, rack_slot=2, hostname="dev1"
        )

        self.viewer = User.objects.create_user("viewer", password="testpass123", is_staff=True)
        self.viewer.groups.add(Group.objects.get(name="Viewer"))
        self.editor = User.objects.create_user("editor", password="testpass123", is_staff=True)
        self.editor.groups.add(Group.objects.get(name="Editor"))
        self.admin_user = User.objects.create_user("adminrole", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.no_group = User.objects.create_user("nogroup", password="testpass123", is_staff=False)

        rack_view_perm = Permission.objects.get(
            codename="view_rack", content_type__app_label="inventory", content_type__model="rack"
        )
        self.rack_only_user = User.objects.create_user("rackonly", password="testpass123", is_staff=True)
        self.rack_only_user.user_permissions.add(rack_view_perm)

        self.non_staff_viewer = User.objects.create_user("viewer2", password="testpass123", is_staff=False)
        self.non_staff_viewer.groups.add(Group.objects.get(name="Viewer"))

    def _routes(self) -> list[str]:
        # Delegates to the shared enumeration (Stage C) rather than
        # hand-listing these five paths a second time — see the comment
        # above ``_shaped_routes``.
        return _shaped_routes(rack_pk=self.rack.pk, vlan_pk=self.vlan.pk, device_pk=self.device.pk)

    def test_every_route_200_for_all_three_roles(self) -> None:
        for username in ["viewer", "editor", "adminrole"]:
            self.client.login(username=username, password="testpass123")
            for url in self._routes():
                self.assertEqual(self.client.get(url).status_code, 200, f"{username} @ {url}")
            self.client.logout()

    def test_unprivileged_user_gets_403(self) -> None:
        self.client.login(username="nogroup", password="testpass123")
        for url in self._routes():
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_logged_out_redirects_to_login(self) -> None:
        for url in self._routes():
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, url)
            location = resp["Location"]
            self.assertTrue(location.startswith("/accounts/login/"), location)

    def test_view_rack_only_user_refused_index_and_rack(self) -> None:
        self.client.login(username="rackonly", password="testpass123")
        self.assertEqual(self.client.get("/").status_code, 403)
        self.assertEqual(self.client.get(f"/racks/{self.rack.pk}/").status_code, 403)

    def test_post_put_patch_delete_return_405_for_every_route(self) -> None:
        self.client.login(username="adminrole", password="testpass123")
        for url in self._routes():
            self.assertEqual(self.client.post(url).status_code, 405, f"POST {url}")
            self.assertEqual(self.client.put(url).status_code, 405, f"PUT {url}")
            self.assertEqual(self.client.patch(url).status_code, 405, f"PATCH {url}")
            self.assertEqual(self.client.delete(url).status_code, 405, f"DELETE {url}")

    def test_admin_link_present_for_staff_absent_for_non_staff(self) -> None:
        self.client.login(username="adminrole", password="testpass123")
        self.assertContains(self.client.get("/"), 'href="/admin/"')
        self.client.logout()

        self.client.login(username=self.non_staff_viewer.username, password="testpass123")
        self.assertNotContains(self.client.get("/"), 'href="/admin/"')


class PartialGrantAccessTests(TestCase):
    """Each view's codename list is the exact set of models its template
    actually reads, not one token model per view (Codex review round 2,
    finding 2 — ``device_detail`` rendered the device's type, its rack,
    full port rows, and the connected switch while declaring only
    ``view_networkdevice``/``view_vlan``; the same gap existed, to a
    smaller degree, on the other four views). A user missing even one
    declared codename must be refused every one of them.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.switch_type = _make_switch_type(port_count=1)
        self.device_type = _make_device_type(port_count=1, vlan=self.vlan)
        self.rack = Rack.objects.create(name="Rack 1", slot_count=10)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.1.0/27")
        self.switch = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=1)
        self.device = NetworkDevice.objects.create(
            device_type=self.device_type, rack=self.rack, rack_slot=2, hostname="dev1"
        )

    def _user_missing(self, codename: str) -> DjangoUser:
        """A staff user holding every ``inventory.view_*`` permission
        except ``codename`` — the minimal way to prove a view's codename
        list is a real floor, not decoration: this user would pass any
        *smaller* declared set but must be refused by the view that
        actually declares ``codename``.
        """
        all_view_perms = Permission.objects.filter(
            content_type__app_label="inventory", codename__startswith="view_"
        )
        user = User.objects.create_user(f"missing-{codename}", password="testpass123", is_staff=True)
        user.user_permissions.set(all_view_perms.exclude(codename=codename))
        return user

    def _assert_403_when_missing_each(self, url: str, codenames: list[str]) -> None:
        for codename in codenames:
            user = self._user_missing(codename)
            self.client.login(username=user.username, password="testpass123")
            self.assertEqual(self.client.get(url).status_code, 403, f"{url} without {codename}")
            self.client.logout()

    def test_index_requires_every_declared_codename(self) -> None:
        self._assert_403_when_missing_each(
            "/", ["view_rack", "view_vlan", "view_networkswitch", "view_networkdevice"]
        )

    def test_rack_detail_requires_every_declared_codename(self) -> None:
        self._assert_403_when_missing_each(
            f"/racks/{self.rack.pk}/",
            [
                "view_rack",
                "view_vlan",
                "view_networkswitch",
                "view_networkdevice",
                "view_rackvlanrange",
                "view_networkswitchaddress",
                "view_networkdeviceport",
                "view_networkdevicetypeport",
                "view_owner",
            ],
        )

    def test_vlan_map_requires_every_declared_codename(self) -> None:
        self._assert_403_when_missing_each(
            f"/vlans/{self.vlan.pk}/",
            [
                "view_vlan",
                "view_rack",
                "view_networkswitch",
                "view_networkdevice",
                "view_rackvlanrange",
                "view_networkswitchaddress",
                "view_networkdeviceport",
                "view_department",
            ],
        )

    def test_device_detail_requires_every_declared_codename(self) -> None:
        self._assert_403_when_missing_each(
            f"/devices/{self.device.pk}/",
            [
                "view_networkdevice",
                "view_vlan",
                "view_networkdevicetype",
                "view_rack",
                "view_networkdeviceport",
                "view_networkswitch",
                # Stage B's port table renders the connected switch port,
                # not just the switch (Codex review, Stage B pass).
                "view_networkswitchport",
                # ADR 0022 — a port's derived hostname reads its
                # source_type_port, a Network Device Type Port.
                "view_networkdevicetypeport",
                "view_owner",
            ],
        )

    def test_spare_pool_requires_every_declared_codename(self) -> None:
        self._assert_403_when_missing_each(
            "/spares/",
            ["view_networkswitch", "view_networkdevice", "view_networkswitchtype", "view_networkdevicetype"],
        )


class WritesNothingTests(TestCase):
    """ADR 0020's central claim, proved rather than asserted: a full GET
    sweep of every route executes no mutating SQL at all, *and* leaves
    every inventory model's row count, and ``auditlog.LogEntry``'s,
    exactly as it found them. Two independent checks (Codex review round
    2, finding 3) — row counts alone can't tell an in-place ``UPDATE``
    from nothing happening, since the count of rows is identical either
    way and a raw ``QuerySet.update()`` bypasses auditlog's signals too.
    """

    def test_allowlist_rejects_replace_and_comment_prefixed_update(self) -> None:
        # The two shapes that defeated the old blocklist (Codex review
        # round 3, finding 1): a verb the list never named (MariaDB's
        # REPLACE), and a statement that doesn't start with any verb at
        # all because a comment precedes it.
        self.assertFalse(_is_allowed_readonly_sql("REPLACE INTO inventory_vlan (...) VALUES (...)"))
        self.assertFalse(_is_allowed_readonly_sql("/* tag */ UPDATE inventory_vlan SET name = 'x'"))
        self.assertFalse(_is_allowed_readonly_sql("UPDATE inventory_vlan SET name = 'x'"))
        self.assertFalse(_is_allowed_readonly_sql("INSERT INTO inventory_vlan (...) VALUES (...)"))
        self.assertFalse(_is_allowed_readonly_sql("DELETE FROM inventory_vlan"))
        self.assertTrue(_is_allowed_readonly_sql("SELECT * FROM inventory_vlan"))
        self.assertTrue(_is_allowed_readonly_sql("/* tag */ SELECT * FROM inventory_vlan"))
        self.assertTrue(_is_allowed_readonly_sql("SAVEPOINT s1"))
        self.assertTrue(_is_allowed_readonly_sql("RELEASE SAVEPOINT s1"))
        self.assertTrue(_is_allowed_readonly_sql("ROLLBACK TO SAVEPOINT s1"))
        self.assertTrue(_is_allowed_readonly_sql("SET autocommit=0"))

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.switch_type = _make_switch_type(port_count=1)
        self.device_type = _make_device_type(port_count=1, vlan=self.vlan)
        self.rack = Rack.objects.create(name="Rack 1", slot_count=10)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.1.0/27")
        self.switch = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=1)
        self.device = NetworkDevice.objects.create(
            device_type=self.device_type, rack=self.rack, rack_slot=2, hostname="dev1"
        )
        self.spare_device = NetworkDevice.objects.create(device_type=self.device_type, hostname="spare1")

        # Stage B fixtures — one of every other registered model, so the
        # sweep below actually exercises every /models/<slug>/ list page
        # and every non-redirecting detail page, not just the
        # four models Stage A already had fixtures for.
        self.profile = SwitchPortVlanProfile.objects.create(name="WN Profile", native_vlan=self.vlan)
        self.rack_template = RackTemplate.objects.create(name="WN Template", slot_count=5)
        self.department = Department.objects.create(name="WN Department")
        self.owner = Owner.objects.create(slug="wn-owner", name="WN Owner")

        self.admin_user = User.objects.create_user("adminrole", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adminrole", password="testpass123")

    def test_get_sweep_executes_no_mutating_sql_and_changes_no_row_counts(self) -> None:
        # This route list is hand-maintained, not derived from
        # _parity_routes()/_all_ui_routes() (review note 5) — so a future
        # registry addition must be swept in here by hand too, or it goes
        # untested by this class while staying green.
        routes = [
            "/",
            f"/racks/{self.rack.pk}/",
            f"/vlans/{self.vlan.pk}/",
            f"/devices/{self.device.pk}/",
            f"/devices/{self.spare_device.pk}/",
            "/spares/",
            "/audit/",
            "/audit/?actor=&action=&content_type=",
            "/models/vlan/",
            f"/models/vlan/{self.vlan.pk}/",
            "/models/department/",
            f"/models/department/{self.department.pk}/",
            "/models/owner/",
            f"/models/owner/{self.owner.pk}/",
            "/models/switchportvlanprofile/",
            f"/models/switchportvlanprofile/{self.profile.pk}/",
            "/models/racktemplate/",
            f"/models/racktemplate/{self.rack_template.pk}/",
            "/models/rack/",
            f"/models/rack/{self.rack.pk}/",  # redirects — still a GET, still no write
            "/models/networkswitchtype/",
            f"/models/networkswitchtype/{self.switch_type.pk}/",
            "/models/networkswitch/",
            f"/models/networkswitch/{self.switch.pk}/",
            "/models/networkdevicetype/",
            f"/models/networkdevicetype/{self.device_type.pk}/",
            "/models/networkdevice/",
            f"/models/networkdevice/{self.device.pk}/",  # redirects too
        ]
        # ``_default_manager``, not ``.objects`` — every model in this app
        # defines the latter, but iterating over apps.get_models()' generic
        # `type[Model]` results means mypy can't see that; `_default_manager`
        # is what Django's own internals (and this module's own admin.py)
        # already reach for in exactly this situation.
        inventory_models = list(apps.get_app_config("inventory").get_models())
        before = {model: model._default_manager.count() for model in inventory_models}
        log_entries_before = LogEntry.objects.count()
        # ContentType and Permission are Django's own create-on-read
        # tables — ContentType.objects.get_for_model() (and, transitively,
        # auditlog's LogEntry.objects.get_for_object(), which the Stage B
        # audit panel used to call) is documented to create the row on a
        # cache miss. A row-count sweep over just the registered inventory
        # models and LogEntry — the original shape of this test — could
        # not have caught that: every registered model's ContentType row
        # already exists by the time any test runs (Django's post_migrate
        # signal creates one per model), so the create-on-miss branch
        # never actually fired here, and the hole went unnoticed until an
        # independent review found it by reading the library's own
        # docstring rather than by running this suite (Codex review).
        # These two counts are what closes that gap for good.
        content_types_before = ContentType.objects.count()
        permissions_before = Permission.objects.count()

        with CaptureQueriesContext(connection) as ctx:
            for url in routes:
                self.assertIn(self.client.get(url).status_code, (200, 301), url)

        disallowed_statements = [
            q["sql"] for q in ctx.captured_queries if not _is_allowed_readonly_sql(q["sql"])
        ]
        self.assertEqual(
            disallowed_statements,
            [],
            "the read-only sweep executed SQL outside the read-only allowlist — see the list above",
        )

        for model in inventory_models:
            self.assertEqual(model._default_manager.count(), before[model], model.__name__)
        self.assertEqual(LogEntry.objects.count(), log_entries_before)
        self.assertEqual(
            ContentType.objects.count(), content_types_before, "sweep must not create ContentType rows"
        )
        self.assertEqual(
            Permission.objects.count(), permissions_before, "sweep must not create Permission rows"
        )


class ElevationEncodingTests(TestCase):
    """The rack-elevation encodings, each asserted at a specific coordinate
    with a negative control (review note 7) — a wrong-cell pass is exactly
    what a presence-only assertion would miss.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.vlan_a = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.vlan_b = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.rack = Rack.objects.create(name="CONSOLES", slot_count=20)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan_a, address_range="10.200.1.0/27")
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan_b, address_range="10.201.1.0/27")

        # SD12-shaped bracket type (ADR 0017): Control at offset 0, Engine
        # at offset 1, both on vlan_a — the console-with-derived-engine
        # shape the ADR exists to represent. Occupies ordinals 5-6.
        self.bracket_type = NetworkDeviceType.objects.create(
            manufacturer="DiGiCo", model="SD12", name="Default", port_count=2
        )
        NetworkDeviceTypePort.objects.create(
            device_type=self.bracket_type,
            description="Control",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_a,
            slot_offset=0,
        )
        NetworkDeviceTypePort.objects.create(
            device_type=self.bracket_type,
            description="Engine",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_a,
            slot_offset=1,
        )
        self.bracket_device = NetworkDevice.objects.create(
            device_type=self.bracket_type, rack=self.rack, rack_slot=5, hostname="sd12-1"
        )

        # An ordinary single-slot device with no relationship to the span
        # test above — used only as the "before the bracket" negative
        # control's neighbour and a general decoy.
        self.plain_type = _make_device_type(port_count=1, vlan=self.vlan_a, name="Plain")
        self.decoy = NetworkDevice.objects.create(
            device_type=self.plain_type, rack=self.rack, rack_slot=9, hostname="decoy"
        )

        # Two independent devices, deliberately non-adjacent — one with
        # only a vlan_a port, the other with only a vlan_b port — so the
        # em-dash assertion below can't be satisfied by coincidence.
        self.vlan_a_only_type = _make_device_type(
            port_count=1, vlan=self.vlan_a, manufacturer="Yamaha", model="DM7C", name="Default"
        )
        self.vlan_b_only_type = _make_device_type(
            port_count=1, vlan=self.vlan_b, manufacturer="Yamaha", model="DM3", name="Default"
        )
        self.device_on_vlan_a = NetworkDevice.objects.create(
            device_type=self.vlan_a_only_type, rack=self.rack, rack_slot=15, hostname="dm7c-1"
        )
        self.device_on_vlan_b = NetworkDevice.objects.create(
            device_type=self.vlan_b_only_type, rack=self.rack, rack_slot=12, hostname="dm3-1"
        )

        self.admin_user = User.objects.create_user("adminrole", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adminrole", password="testpass123")
        response = self.client.get(f"/racks/{self.rack.pk}/")
        self.assertEqual(response.status_code, 200)
        self.content = response.content.decode()

    def test_bracket_covers_exact_span_and_not_beyond(self) -> None:
        start_row = _row_html(self.content, 5)
        continuation_row = _row_html(self.content, 6)
        before_row = _row_html(self.content, 4)
        after_row = _row_html(self.content, 7)
        self.assertIn("ordinal-cell--span-start", start_row)
        self.assertIn("ordinal-cell--span-continuation", continuation_row)
        self.assertIn("ordinal-cell--span-end", continuation_row)
        # Negative controls: neither neighbour of the span is bracketed.
        self.assertNotIn("ordinal-cell--span", before_row)
        self.assertNotIn("ordinal-cell--span", after_row)

    def test_continuation_has_no_add_link_negative_control_empty_does(self) -> None:
        continuation_row = _row_html(self.content, 6)
        empty_row = _row_html(self.content, 7)
        self.assertNotIn("add-slot-link", continuation_row)
        self.assertIn("add-slot-link", empty_row)

    def test_em_dash_at_specific_intersection_not_where_port_exists(self) -> None:
        # Columns are ordered by vlan__vlan_id: vlan_a (200) then vlan_b (201).
        start_row_states = _cell_states(_row_html(self.content, 5))
        self.assertEqual(start_row_states, ["occupied", "absent"])  # Control present, no Dante port
        continuation_row_states = _cell_states(_row_html(self.content, 6))
        self.assertEqual(continuation_row_states, ["occupied", "absent"])  # Engine present, no Dante port

        vlan_a_row_states = _cell_states(_row_html(self.content, 15))
        self.assertEqual(vlan_a_row_states, ["occupied", "absent"])  # vlan_a port present, no vlan_b port
        vlan_b_row_states = _cell_states(_row_html(self.content, 12))
        self.assertEqual(vlan_b_row_states, ["absent", "occupied"])  # no vlan_a port, vlan_b port present

    def test_empty_ordinal_shows_the_address_suggest_slot_address_returns(self) -> None:
        empty_row = _row_html(self.content, 7)
        expected_a = suggest_slot_address("10.200.1.0/27", 7)
        expected_b = suggest_slot_address("10.201.1.0/27", 7)
        self.assertIn(expected_a, empty_row)
        self.assertIn(expected_b, empty_row)

    def test_resolve_slot_spans_agrees_with_slot_span_property(self) -> None:
        for device in [self.bracket_device, self.decoy, self.device_on_vlan_a, self.device_on_vlan_b]:
            spans = resolve_slot_spans([device])
            self.assertEqual(
                spans[device.device_type_id],
                device.device_type.slot_span,
                device.device_type,
            )


class TakenAddressMarkerTests(TestCase):
    """The elevation's ``taken_by`` marker (issue #60,
    PLAN-consumed-slot-addresses.md) — "this ordinal's would-be address is
    already held by somebody", detected across every static address in the
    rack, not just ``OPERATOR``-sourced ones (decision 3). Both VLAN ranges
    sit on a non-zero network base (``/27`` at ``.32``, review note 5) so an
    implementation deriving the ordinal from the address's last octet fails
    rather than passing by luck.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.admin_user = User.objects.create_user("takenaddr-admin", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="takenaddr-admin", password="testpass123")

        self.vlan_a = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.vlan_b = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.rack = Rack.objects.create(name="Taken", slot_count=20)
        self.range_a = RackVlanRange.objects.create(
            rack=self.rack, vlan=self.vlan_a, address_range="10.200.6.32/27"
        )
        self.range_b = RackVlanRange.objects.create(
            rack=self.rack, vlan=self.vlan_b, address_range="10.201.6.32/27"
        )

    def _make_console_type(self, **kwargs) -> NetworkDeviceType:
        """A Dante Primary (SLOT) + Device Control (OPERATOR) console type
        on ``self.vlan_a`` — the exact shape issue #60 was found on
        (``DM7C-1``/``10.201.6.4`` in production).
        """
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Yamaha", model="DM7C", port_count=2, **kwargs
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Dante Primary",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_a,
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Device Control",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_a,
            address_source=PortAddressSource.OPERATOR,
            hostname_suffix="device-control",
        )
        return device_type

    def test_slot_marked_on_both_axes_neighbours_and_own_row_untouched(self) -> None:
        device_type = self._make_console_type(name="Marker Console")
        held_address = suggest_slot_address(self.range_a.address_range, 4)
        NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=device_type,
            rack=self.rack,
            rack_slot=5,
            hostname="DM7C-1",
            operator_addresses={"Device Control": held_address},
        )
        response = self.client.get(f"/racks/{self.rack.pk}/")
        content = response.content.decode()

        row4 = _row_html(content, 4)
        row5 = _row_html(content, 5)

        # Slot 4: marked on both axes — the cell and the row's ordinal marker.
        self.assertIn("taken-by-label", row4)
        # Lowercase — ADR 0023 decision 8 (amended): hostname is normalised
        # on write, even though "DM7C-1" was typed above.
        self.assertIn("dm7c-1", row4)
        self.assertIn("cell-taken", row4)
        self.assertIn("tag-address-taken", row4)

        # Slot 5 (the holder's own, occupied ordinal): neither axis marked.
        self.assertNotIn("taken-by-label", row5)
        self.assertNotIn("tag-address-taken", row5)

        # Neighbours 1-3 and 6+: neither axis marked.
        for ordinal in [1, 2, 3, 6, 7, 8, 9, 10]:
            row = _row_html(content, ordinal)
            self.assertNotIn("taken-by-label", row, f"ordinal {ordinal}")
            self.assertNotIn("tag-address-taken", row, f"ordinal {ordinal}")

        # Review note 6 / settled decision 1 — the marked ordinal keeps its
        # "+ add device" link, stays state == "empty", and still shows its
        # would_be_address.
        self.assertIn("add-slot-link", row4)
        vlan_a_cell = _cell_html(row4, 0)  # columns ordered by vlan__vlan_id: 200 then 201
        self.assertIn("cell-empty", vlan_a_cell)
        self.assertIn(held_address, vlan_a_cell)

    def test_two_holders_of_one_address_render_both_labels_sorted(self) -> None:
        plain_type = _make_device_type(port_count=1, vlan=self.vlan_a, name="Plain Holder")
        switch_type = _make_switch_type(port_count=0)
        held_address = suggest_slot_address(self.range_a.address_range, 4)

        # "Zeta" holds slot 4's address on an ordinary hand-moved SLOT
        # port (ADR 0003 — the port stays editable after creation).
        zeta = NetworkDevice.objects.create(
            device_type=plain_type, rack=self.rack, rack_slot=8, hostname="Zeta"
        )
        zeta_port = zeta.ports.get()
        zeta_port.address = held_address
        zeta_port.save()

        # "Alpha" holds the *same* address on a switch — device-port and
        # switch-address uniqueness are separate constraints (decision 2),
        # so both can genuinely hold one address at once.
        alpha_switch = NetworkSwitch.objects.create(
            switch_type=switch_type, rack=self.rack, rack_slot=9, hostname="Alpha"
        )
        alpha_address = alpha_switch.addresses.get(vlan=self.vlan_a)
        alpha_address.address = held_address
        alpha_address.save()

        response = self.client.get(f"/racks/{self.rack.pk}/")
        row4 = _row_html(response.content.decode(), 4)
        cell = _cell_html(row4, 0)
        # Sorted regardless of creation order — "Alpha" before "Zeta".
        # Lowercase — ADR 0023 decision 8 (amended); still Alpha-before-Zeta
        # sort order.
        self.assertIn("address used by alpha, zeta", cell)

    def test_hand_moved_ordinary_slot_address_produces_a_marker(self) -> None:
        """The positive test for decision 3: an ordinary ``SLOT`` port is
        not ``OPERATOR``-sourced at all, yet a hand-moved address still
        consumes another ordinal's would-be address identically.
        """
        plain_type = _make_device_type(port_count=1, vlan=self.vlan_a, name="Plain Mover")
        held_address = suggest_slot_address(self.range_a.address_range, 4)
        device = NetworkDevice.objects.create(
            device_type=plain_type, rack=self.rack, rack_slot=8, hostname="Mover-1"
        )
        port = device.ports.get()
        self.assertFalse(port.is_operator_addressed)  # an ordinary SLOT port, not OPERATOR
        self.assertNotEqual(port.address, held_address)  # sanity: starts on its own ordinal
        port.address = held_address
        port.save()

        response = self.client.get(f"/racks/{self.rack.pk}/")
        row4 = _row_html(response.content.decode(), 4)
        self.assertIn("taken-by-label", row4)
        # Lowercase — ADR 0023 decision 8 (amended).
        self.assertIn("mover-1", row4)

    def test_no_markers_when_every_address_sits_on_its_own_ordinal(self) -> None:
        plain_type = _make_device_type(port_count=1, vlan=self.vlan_a, name="Aligned")
        for slot in [1, 2, 3]:
            NetworkDevice.objects.create(
                device_type=plain_type, rack=self.rack, rack_slot=slot, hostname=f"aligned-{slot}"
            )
        response = self.client.get(f"/racks/{self.rack.pk}/")
        content = response.content.decode()
        self.assertNotIn("taken-by-label", content)
        self.assertNotIn("tag-address-taken", content)

    def test_taken_address_on_a_continuation_ordinal_keeps_its_state_and_no_marker(self) -> None:
        bracket_type = NetworkDeviceType.objects.create(
            manufacturer="DiGiCo", model="SD12", name="Continuation Bracket", port_count=2
        )
        NetworkDeviceTypePort.objects.create(
            device_type=bracket_type,
            description="Control",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_a,
            slot_offset=0,
        )
        NetworkDeviceTypePort.objects.create(
            device_type=bracket_type,
            description="Engine",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_a,
            slot_offset=1,
        )
        NetworkDevice.objects.create(
            device_type=bracket_type, rack=self.rack, rack_slot=5, hostname="Bracket-1"
        )  # occupies ordinals 5-6; ordinal 6's Engine address is base_a + 6.

        continuation_address = suggest_slot_address(self.range_a.address_range, 6)
        switch_type = _make_switch_type(port_count=0)
        ghost_switch = NetworkSwitch.objects.create(
            switch_type=switch_type, rack=self.rack, rack_slot=12, hostname="Ghost"
        )
        ghost_address = ghost_switch.addresses.get(vlan=self.vlan_a)
        ghost_address.address = continuation_address
        ghost_address.save()

        response = self.client.get(f"/racks/{self.rack.pk}/")
        row6 = _row_html(response.content.decode(), 6)
        self.assertEqual(_cell_states(row6), ["occupied", "absent"])
        self.assertIn("ordinal-cell--span-continuation", row6)
        self.assertNotIn("taken-by-label", row6)
        self.assertNotIn("cell-taken", row6)
        self.assertNotIn("tag-address-taken", row6)

    def test_taken_address_on_a_blank_cell_keeps_its_state_and_no_marker(self) -> None:
        """``blank`` (``ElevationCell``'s docstring) is only reachable for a
        multi-offset device with ports on the same VLAN at *some but not
        all* of its offsets — a narrower shape than the "occupied"
        continuation ordinal above, and easy to miss (Codex review of
        84ffa17, P2). Built here with a span-2 device whose offset-0 port
        is on ``vlan_a`` and whose offset-1 port is on ``vlan_b``: the
        continuation ordinal's ``vlan_a`` cell is "blank" — the device
        does use ``vlan_a``, just not at this offset — not "absent" and
        not "empty".
        """
        # ADR 0017 requires an offset>0 port's VLAN to also carry an
        # offset-0 port to derive its address from, so vlan_b needs both a
        # Primary (offset 0) and an Engine (offset 1) — vlan_a's Control
        # port sits at offset 0 only, with nothing on vlan_a at offset 1.
        span_type = NetworkDeviceType.objects.create(
            manufacturer="Test", model="Split VLAN Span", name="Blank Cell Span", port_count=3
        )
        NetworkDeviceTypePort.objects.create(
            device_type=span_type,
            description="Control",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_a,
            slot_offset=0,
        )
        NetworkDeviceTypePort.objects.create(
            device_type=span_type,
            description="Primary",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_b,
            slot_offset=0,
        )
        NetworkDeviceTypePort.objects.create(
            device_type=span_type,
            description="Engine",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_b,
            slot_offset=1,
        )
        NetworkDevice.objects.create(
            device_type=span_type, rack=self.rack, rack_slot=5, hostname="BlankSpan-1"
        )  # occupies ordinals 5-6; ordinal 6's vlan_a cell is "blank".

        blank_ordinal_address = suggest_slot_address(self.range_a.address_range, 6)
        switch_type = _make_switch_type(port_count=0)
        holder_switch = NetworkSwitch.objects.create(
            switch_type=switch_type, rack=self.rack, rack_slot=12, hostname="BlankHolder"
        )
        holder_address = holder_switch.addresses.get(vlan=self.vlan_a)
        holder_address.address = blank_ordinal_address
        holder_address.save()

        response = self.client.get(f"/racks/{self.rack.pk}/")
        row6 = _row_html(response.content.decode(), 6)
        self.assertEqual(_cell_states(row6), ["blank", "occupied"])
        self.assertNotIn("taken-by-label", row6)
        self.assertNotIn("cell-taken", row6)
        self.assertNotIn("tag-address-taken", row6)

    def test_taken_address_on_a_switch_occupied_ordinal_keeps_its_state_and_no_marker(self) -> None:
        switch_type = _make_switch_type(port_count=0)
        switch = NetworkSwitch.objects.create(
            switch_type=switch_type, rack=self.rack, rack_slot=10, hostname="Switch-1"
        )
        switch_address = switch.addresses.get(vlan=self.vlan_a).address
        assert switch_address is not None

        plain_type = _make_device_type(port_count=1, vlan=self.vlan_a, name="Switch Duplicator")
        duplicate_device = NetworkDevice.objects.create(
            device_type=plain_type, rack=self.rack, rack_slot=15, hostname="Duplicator-1"
        )
        duplicate_port = duplicate_device.ports.get()
        duplicate_port.address = switch_address
        duplicate_port.save()

        response = self.client.get(f"/racks/{self.rack.pk}/")
        row10 = _row_html(response.content.decode(), 10)
        # A switch materializes an address on every rack VLAN range, so
        # both columns are "occupied" here — not "absent" the way a
        # device's unused-VLAN column would be.
        self.assertEqual(_cell_states(row10), ["occupied", "occupied"])
        self.assertNotIn("taken-by-label", row10)
        self.assertNotIn("cell-taken", row10)
        self.assertNotIn("tag-address-taken", row10)

    def test_taken_address_on_a_conflict_ordinal_keeps_its_state_and_no_marker(self) -> None:
        plain_type = _make_device_type(port_count=1, vlan=self.vlan_a, name="Conflict Plain")
        switch_type = _make_switch_type(port_count=0)
        # Neither call runs full_clean(), so the DB's own unique(rack,
        # rack_slot) is the only thing checked — both land at ordinal 3.
        NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=plain_type,
            rack=self.rack,
            rack_slot=3,
            hostname="ConflictDevice",
            port_addressing="dhcp",
        )
        NetworkSwitch.objects.create(
            switch_type=switch_type, rack=self.rack, rack_slot=3, hostname="ConflictSwitch"
        )

        conflict_address = suggest_slot_address(self.range_a.address_range, 3)
        holder_type = _make_device_type(port_count=1, vlan=self.vlan_a, name="Conflict Holder")
        holder = NetworkDevice.objects.create(
            device_type=holder_type, rack=self.rack, rack_slot=16, hostname="Holder-1"
        )
        holder_port = holder.ports.get()
        holder_port.address = conflict_address
        holder_port.save()

        response = self.client.get(f"/racks/{self.rack.pk}/")
        row3 = _row_html(response.content.decode(), 3)
        self.assertIn("row-conflict", row3)
        self.assertEqual(_cell_states(row3), ["conflict", "conflict"])
        self.assertNotIn("taken-by-label", row3)
        self.assertNotIn("cell-taken", row3)
        self.assertNotIn("tag-address-taken", row3)

    def test_dhcp_port_marks_nothing(self) -> None:
        plain_type = _make_device_type(port_count=1, vlan=self.vlan_a, name="Dhcp Marker")
        device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=plain_type, rack=self.rack, rack_slot=8, hostname="DhcpDevice", port_addressing="dhcp"
        )
        port = device.ports.get()
        self.assertTrue(port.is_dhcp)
        self.assertIsNone(port.address)

        response = self.client.get(f"/racks/{self.rack.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("taken-by-label", response.content.decode())

    def test_port_on_a_vlan_the_rack_has_no_range_for_marks_nothing(self) -> None:
        vlan_c = VLAN.objects.create(name="No Range", vlan_id=202, subnet="10.202.0.0/21")
        # Deliberately equal to vlan_a ordinal 6's would-be address — if
        # the map ever ignored VLAN identity, this would wrongly mark it.
        collision_address = suggest_slot_address(self.range_a.address_range, 6)
        bare_type = NetworkDeviceType.objects.create(
            manufacturer="Test", model="Bare", name="No Range Bare", port_count=0
        )
        device = NetworkDevice.objects.create(
            device_type=bare_type, rack=self.rack, rack_slot=9, hostname="NoRangeDevice"
        )
        NetworkDevicePort.objects.create(
            device=device, description="Hand Wired", vlan=vlan_c, address=collision_address
        )

        response = self.client.get(f"/racks/{self.rack.pk}/")
        self.assertEqual(response.status_code, 200)
        row6 = _row_html(response.content.decode(), 6)
        self.assertNotIn("taken-by-label", row6)

    def test_address_outside_the_racks_range_marks_nothing(self) -> None:
        bare_type = NetworkDeviceType.objects.create(
            manufacturer="Test", model="Bare", name="Outside Range Bare", port_count=0
        )
        device = NetworkDevice.objects.create(
            device_type=bare_type, rack=self.rack, rack_slot=9, hostname="OutsideRangeDevice"
        )
        NetworkDevicePort.objects.create(
            device=device, description="Hand Wired", vlan=self.vlan_a, address="10.250.250.250"
        )

        response = self.client.get(f"/racks/{self.rack.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("taken-by-label", response.content.decode())

    def test_operator_address_equal_to_its_own_ordinal_marks_nothing(self) -> None:
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Test", model="Solo Operator", name="Solo Operator", port_count=1
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Solo",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_a,
            address_source=PortAddressSource.OPERATOR,
        )
        own_address = suggest_slot_address(self.range_a.address_range, 7)
        NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=device_type,
            rack=self.rack,
            rack_slot=7,
            hostname="SoloOperator-1",
            operator_addresses={"Solo": own_address},
        )
        response = self.client.get(f"/racks/{self.rack.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("taken-by-label", response.content.decode())

    def test_taken_address_on_one_vlan_does_not_mark_the_same_ordinal_on_another_vlan(self) -> None:
        plain_type = _make_device_type(port_count=1, vlan=self.vlan_a, name="VlanScoped")
        held_address_a = suggest_slot_address(self.range_a.address_range, 4)
        device = NetworkDevice.objects.create(
            device_type=plain_type, rack=self.rack, rack_slot=5, hostname="VlanScoped-1"
        )
        port = device.ports.get()
        port.address = held_address_a
        port.save()

        response = self.client.get(f"/racks/{self.rack.pk}/")
        row4 = _row_html(response.content.decode(), 4)
        vlan_a_cell = _cell_html(row4, 0)
        vlan_b_cell = _cell_html(row4, 1)
        self.assertIn("taken-by-label", vlan_a_cell)
        self.assertNotIn("taken-by-label", vlan_b_cell)


class OccupancyConflictTests(TestCase):
    """More than one occupant claiming an ordinal must be surfaced, not
    silently resolved to whichever one the occupancy dict happened to
    process last (Codex review round 2, finding 4). Reachable through a
    direct ``objects.create()`` — which never calls ``full_clean()`` and
    so never runs ``RackSlotAssignmentMixin.clean()``'s span-overlap
    check — exactly the documented, un-closed gap ADR 0017 and
    ``ROADMAP.md``'s "rack slot occupancy has no DB-level overlap
    guarantee" item describe.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.vlan_a = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.rack = Rack.objects.create(name="Overlap", slot_count=10)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan_a, address_range="10.200.1.0/27")

        # A span-2 device (offset0 + offset1 on the same VLAN) at slot 5,
        # occupying ordinals 5-6.
        self.bracket_type = NetworkDeviceType.objects.create(
            manufacturer="DiGiCo", model="SD12", name="Default", port_count=2
        )
        NetworkDeviceTypePort.objects.create(
            device_type=self.bracket_type,
            description="Control",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_a,
            slot_offset=0,
        )
        NetworkDeviceTypePort.objects.create(
            device_type=self.bracket_type,
            description="Engine",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_a,
            slot_offset=1,
        )
        self.spanning_device = NetworkDevice.objects.create(
            device_type=self.bracket_type, rack=self.rack, rack_slot=5, hostname="sd12-1"
        )

        # A second, ordinary device placed directly at ordinal 6 —
        # objects.create() never calls full_clean(), so the span-overlap
        # check in RackSlotAssignmentMixin.clean() never runs, and this
        # succeeds despite colliding with the spanning device's
        # continuation ordinal.
        self.plain_type = _make_device_type(port_count=1, vlan=self.vlan_a, name="Plain")
        # DHCP addressing on the collider — it shares ordinal 6 with the
        # spanning device's Engine port, and base+slot arithmetic would
        # otherwise also collide on the *address* (a separate invariant,
        # still enforced even when clean() is bypassed, since it's
        # checked again at materialization time). Only the slot/span
        # overlap is the gap this test targets.
        self.colliding_device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=self.plain_type,
            rack=self.rack,
            rack_slot=6,
            hostname="collider",
            port_addressing="dhcp",
        )

        self.admin_user = User.objects.create_user("adminrole", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adminrole", password="testpass123")
        response = self.client.get(f"/racks/{self.rack.pk}/")
        self.assertEqual(response.status_code, 200)
        self.content = response.content.decode()

    def test_conflicting_ordinal_lists_both_occupants_not_just_one(self) -> None:
        conflict_row = _row_html(self.content, 6)
        self.assertIn("row-conflict", conflict_row)
        # Both claimants must be named — an earlier revision kept only
        # whichever entry the dict-overwrite happened to process last,
        # silently dropping the other.
        self.assertIn(str(self.spanning_device), conflict_row)
        self.assertIn(str(self.colliding_device), conflict_row)

    def test_non_conflicting_ordinals_are_unaffected(self) -> None:
        start_row = _row_html(self.content, 5)
        self.assertNotIn("row-conflict", start_row)
        self.assertIn(str(self.spanning_device), start_row)


class BannerHatchConsistencyTests(TestCase):
    """The address map's next-free-block banner and its hatched region
    must both be driven by *stored* data and must agree with the admin's
    own allocator — not with each other via an invented, unrecorded
    convention (Codex review round 3, finding 4, reversing round 2's
    fix).

    ADR 0002's "bottom /24 is DHCP" sizing-time suggestion was explicitly
    retired by ADR 0011: ``dhcp_range`` stopped being a CIDR block, and
    "no replacement convention was wanted" (ADR 0011's own words) — no
    auto-suggestion of any kind replaced it. Round 2's fix hatched the
    bottom /24 unconditionally (asserting a convention the domain no
    longer tracks as data) and then widened the banner's exclusions to
    agree with that hatch, which made the *banner* diverge from what
    ``RackVlanRange.clean()`` would actually allocate for a blank range
    on this VLAN — a worse problem, since a wrong "next free block" is
    believed. The correct fix removes the false assertion instead of
    building a second one to agree with it: the hatch now shows only a
    VLAN's own recorded ``dhcp_range_start``/``dhcp_range_end`` (nothing,
    if neither is set), and the banner reaches ``suggest_rack_vlan_range``
    with exactly the same inputs the admin's own suggestion would use.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.admin_user = User.objects.create_user("adminrole", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adminrole", password="testpass123")

    def test_no_hatch_and_banner_matches_allocator_with_no_stored_dhcp_range(self) -> None:
        vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        response = self.client.get(f"/vlans/{vlan.pk}/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # Nothing is recorded, so nothing is hatched — the bottom /24 is
        # not assumed unavailable.
        self.assertNotIn("vlan-map-segment--hatched", content)
        # The banner must show exactly what suggest_rack_vlan_range
        # itself computes for this VLAN — the same call
        # RackVlanRange.clean() would make for a blank range here.
        expected = suggest_rack_vlan_range(vlan.subnet, 1, [], None)
        assert expected is not None
        self.assertIn(expected, content)
        self.assertIn("10.200.0.0/27", content)  # the honest first-fit answer with nothing excluded

    def test_hatch_reflects_stored_dhcp_range_and_banner_avoids_it(self) -> None:
        vlan = VLAN.objects.create(
            name="Control",
            vlan_id=200,
            subnet="10.200.0.0/21",
            dhcp_range_start="10.200.0.10",
            dhcp_range_end="10.200.0.200",
        )
        response = self.client.get(f"/vlans/{vlan.pk}/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("vlan-map-segment--hatched", content)
        self.assertIn("10.200.0.10-10.200.0.200", content)
        # The banner must still agree exactly with the allocator, now
        # that a real DHCP range is stored — computed independently here
        # via the same function/inputs RackVlanRange.clean() would use.
        assert vlan.dhcp_range_start is not None
        assert vlan.dhcp_range_end is not None
        expected = suggest_rack_vlan_range(vlan.subnet, 1, [], (vlan.dhcp_range_start, vlan.dhcp_range_end))
        assert expected is not None
        self.assertIn(expected, content)
        # And the allocator's own answer must not itself land inside the
        # recorded DHCP range — otherwise the test fixture, not the code
        # under test, would be the thing that's wrong.
        expected_network = ipaddress.IPv4Network(expected)
        self.assertFalse(
            ipaddress.IPv4Address("10.200.0.10") in expected_network
            or ipaddress.IPv4Address("10.200.0.200") in expected_network
        )


class RobustnessTests(TestCase):
    """Legal-but-awkward stored data must render 200, not 500 — data the
    write path already allows onto these tables via a bare ``save()``
    that bypasses ``clean()`` (review note 3) — and must not render a
    *wrong* answer either (Codex review round 2, finding 1: an undersized
    range doesn't raise, it just computes an address outside the block).
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.admin_user = User.objects.create_user("adminrole", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adminrole", password="testpass123")

    def test_safe_slot_address_rejects_ordinal_outside_undersized_block(self) -> None:
        # Unit-level: a /30 (10.200.1.0-10.200.1.3) has room for ordinal 1
        # but not ordinal 9 — suggest_slot_address's raw arithmetic would
        # happily compute 10.200.1.9 anyway, since it has no notion of the
        # block's own boundary. safe_slot_address must reject the second.
        self.assertEqual(safe_slot_address("10.200.1.0/30", 1), "10.200.1.1")
        self.assertIsNone(safe_slot_address("10.200.1.0/30", 9))

    def test_safe_slot_address_rejects_the_blocks_own_reserved_addresses(self) -> None:
        # Codex review round 3, finding 3: containment alone still admits
        # the block's own reserved addresses. required_block_size()'s
        # docstring is explicit that index 0 (the network address) and
        # index size-1 (the top/broadcast address) are both reserved —
        # for this /30 (indices 0-3), only ordinals 1 and 2 are honest
        # slot addresses; ordinal 3 lands exactly on the reserved top
        # index (10.200.1.3, the block's broadcast address) and ordinal 0
        # lands on the reserved base (10.200.1.0), the network address.
        self.assertEqual(safe_slot_address("10.200.1.0/30", 1), "10.200.1.1")
        self.assertEqual(safe_slot_address("10.200.1.0/30", 2), "10.200.1.2")
        self.assertIsNone(safe_slot_address("10.200.1.0/30", 3))
        self.assertIsNone(safe_slot_address("10.200.1.0/30", 0))

    def test_undersized_range_does_not_render_out_of_block_address(self) -> None:
        rack = Rack.objects.create(name="Undersized", slot_count=10)
        rack_range = RackVlanRange(rack=rack, vlan=self.vlan, address_range="10.200.1.0/30")
        rack_range.save()  # bypasses clean() — a /30 could never pass it for a 10-slot rack

        response = self.client.get(f"/racks/{rack.pk}/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # Ordinal 1 genuinely lands inside the /30, on a non-reserved
        # address, and should show it...
        self.assertIn("10.200.1.1", _row_html(content, 1))
        # ...but ordinal 3 lands exactly on the block's reserved top
        # address (10.200.1.3, the /30's broadcast address) and must
        # render blank, not that reserved value.
        row_3 = _row_html(content, 3)
        self.assertNotIn("10.200.1.3", row_3)
        self.assertNotIn("would-be-address", row_3)
        # ...and ordinal 9 is well outside the block entirely.
        row_9 = _row_html(content, 9)
        self.assertNotIn("10.200.1.9", row_9)
        self.assertNotIn("would-be-address", row_9)

    def test_malformed_stored_range_renders_200_with_blank_cells(self) -> None:
        rack = Rack.objects.create(name="Malformed", slot_count=5)
        rack_range = RackVlanRange(rack=rack, vlan=self.vlan, address_range="not-a-cidr")
        rack_range.save()  # bypasses clean() — a bare save() does not validate

        response = self.client.get(f"/racks/{rack.pk}/")
        self.assertEqual(response.status_code, 200)
        row = _row_html(response.content.decode(), 1)
        self.assertNotIn("would-be-address", row)

    def test_l2_only_vlan_map_renders_200_no_tracked_addressing(self) -> None:
        l2_vlan = VLAN.objects.create(name="L2 Only", vlan_id=999)
        response = self.client.get(f"/vlans/{l2_vlan.pk}/")
        self.assertEqual(response.status_code, 200)
        # Assert the *outcome* — no address map is built, and the page says
        # why in terms of the missing subnet — rather than an exact sentence.
        # This test previously pinned the phrase "no tracked addressing" and
        # broke on a copy edit that changed nothing about behaviour; on-screen
        # wording is product copy and will keep changing (see the template
        # comments about keeping ADR references out of user-facing text).
        self.assertNotContains(response, "Shape of the subnet")
        self.assertContains(response, "no subnet")

    def test_l2_only_vlan_with_department_still_shows_it(self) -> None:
        # Codex review: the department line used to sit inside the
        # subnet-valid branch, beside {{ vlan.subnet }} — a line the
        # l2_only branch never reaches, so a real department silently
        # disappeared from an L2-only VLAN's page. Department and subnet
        # are independent (ADR 0021 doesn't touch ADR 0012's L2-only
        # rule), so this must render regardless of unavailable_reason.
        department = Department.objects.create(name="L2 Department")
        l2_vlan = VLAN.objects.create(name="L2 Only", vlan_id=998, department=department)
        response = self.client.get(f"/vlans/{l2_vlan.pk}/")
        self.assertEqual(response.status_code, 200)
        href, text = _vlan_map_department_line(response.content.decode())
        self.assertEqual(href, f"/models/department/{department.pk}/")
        self.assertEqual(text, "L2 Department")

    def test_department_less_vlan_map_renders_200_with_no_department_line(self) -> None:
        # ADR 0021: department is optional, and a read-only page must never
        # crash on data the write path allows (review note 3's posture,
        # applied to the new field) — self.vlan carries no department.
        response = self.client.get(f"/vlans/{self.vlan.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Department:")

    def test_ordinal_beyond_address_space_renders_blank_not_500(self) -> None:
        # A block near the top of the whole IPv4 address space — an
        # ordinal within slot_count but past the block's own top pushes
        # suggest_slot_address()'s arithmetic past 255.255.255.255,
        # raising ValueError. safe_slot_address() must catch this rather
        # than letting the page 500.
        rack = Rack.objects.create(name="Overflow", slot_count=10)
        rack_range = RackVlanRange(rack=rack, vlan=self.vlan, address_range="255.255.255.252/30")
        rack_range.save()  # bypasses clean()

        response = self.client.get(f"/racks/{rack.pk}/")
        self.assertEqual(response.status_code, 200)
        row = _row_html(response.content.decode(), 9)  # 252 + 9 overflows the address space
        self.assertNotIn("would-be-address", row)


class QueryBudgetTests(TestCase):
    """Equal query counts across occupancy — the test that actually
    catches an N+1 (review note 2); a single number recorded after the
    fact would just bless whatever the implementation happened to do.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.admin_user = User.objects.create_user("adminrole", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adminrole", password="testpass123")

    def test_elevation_query_count_independent_of_device_count(self) -> None:
        vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        device_type = _make_device_type(port_count=1, vlan=vlan)

        small_rack = Rack.objects.create(name="Small", slot_count=60)
        RackVlanRange.objects.create(rack=small_rack, vlan=vlan, address_range="10.200.1.0/26")
        for i in range(1, 3):
            NetworkDevice.objects.create(
                device_type=device_type, rack=small_rack, rack_slot=i, hostname=f"s{i}"
            )

        big_rack = Rack.objects.create(name="Big", slot_count=60)
        RackVlanRange.objects.create(rack=big_rack, vlan=vlan, address_range="10.200.2.0/26")
        for i in range(1, 51):
            NetworkDevice.objects.create(
                device_type=device_type, rack=big_rack, rack_slot=i, hostname=f"b{i}"
            )

        with CaptureQueriesContext(connection) as small_ctx:
            small_response = self.client.get(f"/racks/{small_rack.pk}/")
        with CaptureQueriesContext(connection) as big_ctx:
            big_response = self.client.get(f"/racks/{big_rack.pk}/")

        self.assertEqual(small_response.status_code, 200)
        self.assertEqual(big_response.status_code, 200)
        self.assertEqual(len(small_ctx.captured_queries), len(big_ctx.captured_queries))
        # Issue #60's taken-address map is built entirely from data this
        # view already prefetches (PLAN-consumed-slot-addresses.md decision
        # 4) — it must add exactly zero queries. 12 is the absolute count
        # recorded on the pre-#60 revision of this test (both rack sizes);
        # an implementation that reaches for a fresh
        # NetworkDevicePort.objects.filter(...), or that touches
        # port.source_type_port while building the map (prefetched in
        # device_detail(), not here — see _build_taken_address_map's
        # docstring), would pass the equality assertion above while
        # quietly moving this number.
        self.assertEqual(len(small_ctx.captured_queries), 12)
        self.assertEqual(len(big_ctx.captured_queries), 12)

    def test_vlan_map_query_count_independent_of_address_count(self) -> None:
        vlan_small = VLAN.objects.create(name="Small", vlan_id=200, subnet="10.200.0.0/21")
        vlan_big = VLAN.objects.create(name="Big", vlan_id=201, subnet="10.201.0.0/21")
        device_type_small = _make_device_type(port_count=1, vlan=vlan_small, name="SmallType")
        device_type_big = _make_device_type(port_count=1, vlan=vlan_big, name="BigType")

        rack_small = Rack.objects.create(name="RackSmall", slot_count=10)
        RackVlanRange.objects.create(rack=rack_small, vlan=vlan_small, address_range="10.200.1.0/27")
        for i in range(1, 3):
            NetworkDevice.objects.create(
                device_type=device_type_small, rack=rack_small, rack_slot=i, hostname=f"sm{i}"
            )

        rack_big = Rack.objects.create(name="RackBig", slot_count=60)
        RackVlanRange.objects.create(rack=rack_big, vlan=vlan_big, address_range="10.201.1.0/26")
        for i in range(1, 51):
            NetworkDevice.objects.create(
                device_type=device_type_big, rack=rack_big, rack_slot=i, hostname=f"bg{i}"
            )

        with CaptureQueriesContext(connection) as small_ctx:
            small_response = self.client.get(f"/vlans/{vlan_small.pk}/")
        with CaptureQueriesContext(connection) as big_ctx:
            big_response = self.client.get(f"/vlans/{vlan_big.pk}/")

        self.assertEqual(small_response.status_code, 200)
        self.assertEqual(big_response.status_code, 200)
        self.assertEqual(len(small_ctx.captured_queries), len(big_ctx.captured_queries))

    def test_model_list_query_count_independent_of_row_count(self) -> None:
        """Stage B: every one of the ``/models/<slug>/`` list pages
        must cost the same number of queries whether it lists 2 rows or
        50 — proof the declared ``list_select_related``/
        ``list_prefetch_related`` hints actually eliminate the N+1 a naive
        per-row relation/m2m render would otherwise be (review note 2's
        equal-count discipline, applied to the parity registry). Each
        slug is measured in isolation (its own small/big object sets),
        since REGISTRY entries are independent models.
        """
        native_vlan = VLAN.objects.create(name="QB Native VLAN", vlan_id=290, subnet="10.290.0.0/24")
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="QB", model="M", name="QB Switch Type", port_count=0
        )
        device_type = NetworkDeviceType.objects.create(
            manufacturer="QB", model="M", name="QB Device Type", port_count=0
        )
        # A non-null owner and rack for the switch/device rows below (code
        # review finding 5c) — hostname_diverges (#54) reads both, and a
        # null FK short-circuits with no query at all regardless of
        # list_select_related, so rows with neither set would let this
        # budget test pass even if "owner" were dropped from
        # list_select_related entirely. slot_count=50 covers the biggest
        # batch either factory below creates.
        qb_relations_owner = Owner.objects.create(slug="qb-relations-owner", name="QB Relations Owner")
        qb_relations_rack = Rack.objects.create(
            name="QB Relations Rack",
            slot_count=50,
            owner=qb_relations_owner,
            location_slug="qb-relations-loc",
        )

        factories = {
            "vlan": lambda i: VLAN.objects.create(name=f"QB VLAN {i}", vlan_id=2000 + i),
            "switchportvlanprofile": lambda i: SwitchPortVlanProfile.objects.create(
                name=f"QB Profile {i}", native_vlan=native_vlan
            ),
            "racktemplate": lambda i: RackTemplate.objects.create(name=f"QB Template {i}"),
            "department": lambda i: Department.objects.create(name=f"QB Department {i}"),
            "owner": lambda i: Owner.objects.create(slug=f"qb-owner-{i}", name=f"QB Owner {i}"),
            "rack": lambda i: Rack.objects.create(name=f"QB Rack {i}", slot_count=1),
            "networkswitchtype": lambda i: NetworkSwitchType.objects.create(
                manufacturer="QB", model="M", name=f"QB SwitchType {i}", port_count=0
            ),
            "networkswitch": lambda i: NetworkSwitch.objects.create(
                switch_type=switch_type,
                hostname=f"qb-switch-{i}",
                owner=qb_relations_owner,
                rack=qb_relations_rack,
                rack_slot=i + 1,
            ),
            "networkdevicetype": lambda i: NetworkDeviceType.objects.create(
                manufacturer="QB", model="M", name=f"QB DeviceType {i}", port_count=0
            ),
            "networkdevice": lambda i: NetworkDevice.objects.create(
                device_type=device_type,
                hostname=f"qb-device-{i}",
                owner=qb_relations_owner,
                rack=qb_relations_rack,
                rack_slot=i + 1,
            ),
        }
        self.assertEqual(set(factories), set(REGISTRY), "every registry slug needs a query-budget factory")

        for slug, make in factories.items():
            for i in range(2):
                make(i)
            with CaptureQueriesContext(connection) as small_ctx:
                small_response = self.client.get(f"/models/{slug}/")
            for i in range(2, 50):
                make(i)
            with CaptureQueriesContext(connection) as big_ctx:
                big_response = self.client.get(f"/models/{slug}/")

            self.assertEqual(small_response.status_code, 200, slug)
            self.assertEqual(big_response.status_code, 200, slug)
            self.assertEqual(len(small_ctx.captured_queries), len(big_ctx.captured_queries), slug)

    def test_networkswitchtype_detail_query_count_independent_of_inline_row_count(self) -> None:
        """Stage B: the inline row count on a parity detail page must not
        change the query count either — the same discipline as the
        elevation/vlan-map tests above, now for
        ``ModelSpec.detail_prefetch_related``.
        """
        small_type = NetworkSwitchType.objects.create(
            manufacturer="QB", model="M", name="QB Small Switch Type", port_count=2
        )
        for n in range(1, 3):
            NetworkSwitchTypePort.objects.create(
                switch_type=small_type, port_number=n, port_type=PortType.GBE_RJ45
            )

        big_type = NetworkSwitchType.objects.create(
            manufacturer="QB", model="M", name="QB Big Switch Type", port_count=60
        )
        for n in range(1, 61):
            NetworkSwitchTypePort.objects.create(
                switch_type=big_type, port_number=n, port_type=PortType.GBE_RJ45
            )

        with CaptureQueriesContext(connection) as small_ctx:
            small_response = self.client.get(f"/models/networkswitchtype/{small_type.pk}/")
        with CaptureQueriesContext(connection) as big_ctx:
            big_response = self.client.get(f"/models/networkswitchtype/{big_type.pk}/")

        self.assertEqual(small_response.status_code, 200)
        self.assertEqual(big_response.status_code, 200)
        self.assertEqual(len(small_ctx.captured_queries), len(big_ctx.captured_queries))

    def test_device_detail_query_count_independent_of_port_count(self) -> None:
        """ADR 0022 — ``NetworkDevicePort.hostname`` reads ``source_type_
        port`` on every row; without it in the canonical device page's own
        ``Prefetch``, this is an N+1 across the port table. Extended for
        ADR 0022 PR 3 (review note 8): ``installed_cards`` (the "Cards
        fitted" panel) must be prefetched too, or it's an N+1 across
        however many cards are fitted to a device.
        """
        vlan = VLAN.objects.create(name="QB Hostname VLAN", vlan_id=291, subnet="10.209.0.0/21")

        card_type = NetworkDeviceType.objects.create(
            manufacturer="QB", model="M", name="QB Card Type", port_count=0, is_add_in_card=True
        )

        small_type = NetworkDeviceType.objects.create(
            manufacturer="QB", model="M", name="QB Small Device Type", port_count=1
        )
        NetworkDeviceTypePort.objects.create(
            device_type=small_type,
            description="Port 1",
            port_type=PortType.GBE_RJ45,
            vlan=vlan,
            hostname_suffix="port1",
        )
        small_device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=small_type, hostname="qb-small", port_addressing=PortAddressing.DHCP
        )
        NetworkDevice.objects.create(device_type=card_type, hostname="qb-small-card-1", host=small_device)

        big_type = NetworkDeviceType.objects.create(
            manufacturer="QB", model="M", name="QB Big Device Type", port_count=40
        )
        for n in range(1, 41):
            NetworkDeviceTypePort.objects.create(
                device_type=big_type,
                description=f"Port {n}",
                port_type=PortType.GBE_RJ45,
                vlan=vlan,
                hostname_suffix=f"port{n}",
            )
        big_device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=big_type, hostname="qb-big", port_addressing=PortAddressing.DHCP
        )
        for i in range(10):
            NetworkDevice.objects.create(device_type=card_type, hostname=f"qb-big-card-{i}", host=big_device)

        with CaptureQueriesContext(connection) as small_ctx:
            small_response = self.client.get(f"/devices/{small_device.pk}/")
        with CaptureQueriesContext(connection) as big_ctx:
            big_response = self.client.get(f"/devices/{big_device.pk}/")

        self.assertEqual(small_response.status_code, 200)
        self.assertEqual(big_response.status_code, 200)
        self.assertEqual(len(small_ctx.captured_queries), len(big_ctx.captured_queries))

    def test_device_detail_query_count_independent_of_whether_host_is_set(self) -> None:
        """ADR 0022 PR 3 review note 8 — ``host`` must be ``select_related``,
        or rendering a card's "Fitted to" line costs one extra query. A
        to-one relation's absence doesn't scale with row count the way an
        N+1 does, so this needs its own pair (hostless vs. fitted) rather
        than the small-vs-big idiom above.
        """
        card_type = NetworkDeviceType.objects.create(
            manufacturer="QB", model="M", name="QB Host-select Card Type", port_count=0, is_add_in_card=True
        )
        anchor_type = NetworkDeviceType.objects.create(
            manufacturer="QB", model="M", name="QB Host-select Anchor Type", port_count=0
        )
        anchor_host = NetworkDevice.objects.create(device_type=anchor_type, hostname="qb-anchor-host")
        hostless_card = NetworkDevice.objects.create(device_type=card_type, hostname="qb-hostless-card")
        fitted_card = NetworkDevice.objects.create(
            device_type=card_type, hostname="qb-fitted-card", host=anchor_host
        )

        with CaptureQueriesContext(connection) as hostless_ctx:
            hostless_response = self.client.get(f"/devices/{hostless_card.pk}/")
        with CaptureQueriesContext(connection) as fitted_ctx:
            fitted_response = self.client.get(f"/devices/{fitted_card.pk}/")

        self.assertEqual(hostless_response.status_code, 200)
        self.assertEqual(fitted_response.status_code, 200)
        self.assertEqual(len(hostless_ctx.captured_queries), len(fitted_ctx.captured_queries))

    def test_audit_query_count_independent_of_total_entry_count(self) -> None:
        """Stage B decision 16: a fixed number of queries per audit page,
        independent of how many total ``LogEntry`` rows exist — the
        ``Paginator``'s own ``COUNT(*)`` cost scales with row count in
        runtime, not in query *count*, which is what this locks in.
        """
        vlan = VLAN.objects.create(name="QB Audit VLAN", vlan_id=291, subnet="10.291.0.0/24")
        for i in range(10):
            vlan.name = f"QB Audit VLAN rename {i}"
            vlan.save()

        with CaptureQueriesContext(connection) as small_ctx:
            small_response = self.client.get("/audit/")
        self.assertEqual(small_response.status_code, 200)

        for i in range(10, 60):
            vlan.name = f"QB Audit VLAN rename {i}"
            vlan.save()

        with CaptureQueriesContext(connection) as big_ctx:
            big_response = self.client.get("/audit/")
        self.assertEqual(big_response.status_code, 200)
        self.assertEqual(len(small_ctx.captured_queries), len(big_ctx.captured_queries))


class DeepLinkTests(TestCase):
    """A deep link's target form is actually prefilled, not merely
    reachable (review note 9) — Django's own admin ``get_changeform_
    initial_data`` reads matching keys off ``request.GET``, which the
    rack elevation's empty-ordinal links rely on for ADR 0020 decision 3's
    "picks up ADR 0019's ordinal suggestion for free".
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.rack = Rack.objects.create(name="Rack 1", slot_count=10)
        self.editor = User.objects.create_user("editor", password="testpass123", is_staff=True)
        self.editor.groups.add(Group.objects.get(name="Editor"))
        self.client.login(username="editor", password="testpass123")

    def test_add_device_deep_link_prefills_rack_and_slot(self) -> None:
        url = f"{reverse('admin:inventory_networkdevice_add')}?rack={self.rack.pk}&rack_slot=6"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        initial = response.context["adminform"].form.initial
        self.assertEqual(str(initial.get("rack")), str(self.rack.pk))
        self.assertEqual(str(initial.get("rack_slot")), "6")


# ---------------------------------------------------------------------------
# Stage B — read-parity and the audit trail
# ---------------------------------------------------------------------------


class ParityAccessTests(ParityFixtureMixin, TestCase):
    """The same access-control discipline Stage A proved for the four
    shaped views — ``login_required`` outermost (a logged-out visitor
    redirects, doesn't 403), ``require_GET`` (every write verb refused),
    and a real permission floor — now for the three Stage B routes.
    """

    def _routes(self) -> list[str]:
        # Delegates to the shared enumeration (Stage C) rather than
        # hand-listing these paths a second time — see the comment above
        # ``_parity_routes``.
        return _parity_routes(pk_by_slug=self.pk_by_slug)

    def test_every_route_200_or_redirect_for_all_three_roles(self) -> None:
        canonical_slugs = {slug for slug, spec in REGISTRY.items() if spec.canonical_detail_view}
        for username in ["stageb-viewer", "stageb-editor", "stageb-admin"]:
            self.client.login(username=username, password="testpass123")
            for slug in REGISTRY:
                self.assertEqual(
                    self.client.get(self._list_url(slug)).status_code, 200, f"{username} {slug} list"
                )
                expected = 301 if slug in canonical_slugs else 200
                self.assertEqual(
                    self.client.get(self._detail_url(slug)).status_code, expected, f"{username} {slug} detail"
                )
            self.assertEqual(self.client.get("/audit/").status_code, 200, f"{username} audit")
            self.client.logout()

    def test_unprivileged_user_gets_403(self) -> None:
        self.client.login(username="stageb-nogroup", password="testpass123")
        for url in self._routes():
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_logged_out_redirects_to_login(self) -> None:
        for url in self._routes():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertTrue(response["Location"].startswith("/accounts/login/"), response["Location"])

    def test_post_put_patch_delete_return_405_for_every_route(self) -> None:
        self.client.login(username="stageb-admin", password="testpass123")
        for url in self._routes():
            self.assertEqual(self.client.post(url).status_code, 405, f"POST {url}")
            self.assertEqual(self.client.put(url).status_code, 405, f"PUT {url}")
            self.assertEqual(self.client.patch(url).status_code, 405, f"PATCH {url}")
            self.assertEqual(self.client.delete(url).status_code, 405, f"DELETE {url}")

    def test_unknown_slug_is_404_not_500(self) -> None:
        self.client.login(username="stageb-admin", password="testpass123")
        self.assertEqual(self.client.get("/models/nonexistent/").status_code, 404)
        self.assertEqual(self.client.get("/models/nonexistent/1/").status_code, 404)

    def test_unknown_pk_is_404_not_500(self) -> None:
        self.client.login(username="stageb-admin", password="testpass123")
        for slug in REGISTRY:
            self.assertEqual(self.client.get(f"/models/{slug}/999999/").status_code, 404, slug)

    def test_rack_and_networkdevice_model_detail_redirect_to_shaped_view(self) -> None:
        # Decision 13 — assert the *target*, not merely a 3xx (review note 3).
        self.client.login(username="stageb-admin", password="testpass123")
        rack_response = self.client.get(self._detail_url("rack"))
        self.assertEqual(rack_response.status_code, 301)
        self.assertEqual(rack_response["Location"], reverse("inventory:rack", args=[self.rack.pk]))

        device_response = self.client.get(self._detail_url("networkdevice"))
        self.assertEqual(device_response.status_code, 301)
        self.assertEqual(device_response["Location"], reverse("inventory:device", args=[self.device.pk]))

    def test_audit_nav_link_present_only_with_permission(self) -> None:
        self.client.login(username="stageb-viewer", password="testpass123")
        self.assertContains(self.client.get("/"), 'href="/audit/"')
        self.client.logout()

        no_audit_user = _user_missing_codename("auditlog.view_logentry")
        self.client.login(username=no_audit_user.username, password="testpass123")
        self.assertNotContains(self.client.get("/"), 'href="/audit/"')

    def test_missing_audit_permission_403s_audit_but_not_rack_elevation(self) -> None:
        # The regression guard review note 8 exists for: a missing audit
        # grant must lose only the panel, never the whole page it's
        # included on.
        no_audit_user = _user_missing_codename("auditlog.view_logentry")
        self.client.login(username=no_audit_user.username, password="testpass123")
        self.assertEqual(self.client.get("/audit/").status_code, 403)
        rack_response = self.client.get(f"/racks/{self.rack.pk}/")
        self.assertEqual(rack_response.status_code, 200)
        self.assertNotContains(rack_response, "Audit history")

    def test_content_type_lookup_never_creates_a_missing_row(self) -> None:
        """Codex review: ``ContentType.objects.get_for_model()`` — and,
        transitively, ``LogEntry.objects.get_for_object()``, which the
        audit panel used to call — creates the row on a cache miss. Every
        registered model's row already exists by the time any test runs
        (``post_migrate`` creates one per model), so the writes-nothing
        sweep's row-count check never actually exercised that branch —
        this test forces the miss directly.

        Deliberately a direct call, not a hit against a live page: a
        page's own model's ``ContentType`` row backs that model's
        ``Permission`` rows too (``Permission.content_type`` is
        ``on_delete=CASCADE``), so deleting Rack's row to force the miss
        would also delete ``view_rack`` and 403 the request for an
        unrelated reason — masking exactly the behaviour this test exists
        to check.
        """
        content_type_pk = ContentType.objects.get_for_model(Rack).pk
        ContentType.objects.filter(pk=content_type_pk).delete()
        ContentType.objects.clear_cache()
        before = ContentType.objects.count()

        self.assertIsNone(_content_type_for_model_no_create(Rack))
        self.assertEqual(ContentType.objects.count(), before, "a pure lookup must never create a row")

        entries, resolved_content_type_pk = _object_audit_panel_context(self.rack, self.admin_user)
        self.assertEqual(entries, [])
        self.assertIsNone(resolved_content_type_pk)
        self.assertEqual(
            ContentType.objects.count(), before, "the panel context builder must not create a row"
        )


class PartialGrantParityAccessTests(ParityFixtureMixin, TestCase):
    """For every registry entry, a user granted every codename in
    ``detail_permissions`` except one is refused that entry's detail page
    — the test that proves the declared sets are real, not decorative
    (Stage A's ``PartialGrantAccessTests`` does the same for the four
    shaped views).
    """

    def test_detail_requires_every_declared_codename(self) -> None:
        for slug, spec in REGISTRY.items():
            for codename in spec.detail_permissions:
                user = _user_missing_codename(codename)
                self.client.login(username=user.username, password="testpass123")
                response = self.client.get(self._detail_url(slug))
                self.assertEqual(response.status_code, 403, f"{slug} without {codename}")
                self.client.logout()


class OwnerAccessTests(ParityFixtureMixin, TestCase):
    """Plan PR2 Tests: a Viewer with ``view_owner`` sees the Owners list and
    detail; without it, both 403.
    """

    def test_viewer_with_view_owner_sees_list_and_detail(self) -> None:
        self.client.login(username="stageb-viewer", password="testpass123")
        self.assertEqual(self.client.get(self._list_url("owner")).status_code, 200)
        self.assertEqual(self.client.get(self._detail_url("owner")).status_code, 200)

    def test_user_without_view_owner_403s_on_list_and_detail(self) -> None:
        user = _user_missing_codename("inventory.view_owner")
        self.client.login(username=user.username, password="testpass123")
        self.assertEqual(self.client.get(self._list_url("owner")).status_code, 403)
        self.assertEqual(self.client.get(self._detail_url("owner")).status_code, 403)


class ParityContentTests(ParityFixtureMixin, TestCase):
    """Read-parity content, asserted with distinctive fixture values
    (review note 7) — never a bare ``assertContains(response, "VLAN")``.

    Every test below checks the *exact* rendered cell/field sequence for
    one row, via ``_list_row_cells``/``_detail_field_text``/
    ``_inline_row_cells``, rather than merely that some expected substring
    appears anywhere on the page. That distinction is load-bearing (Codex
    review): a presence-only check can't tell "this column rendered its
    real value" from "this column was silently dropped and something else
    on the page happens to contain a similar string" — booleans especially
    (a missing boolean column and a ``False`` one read identically under
    ``assertContains``).
    """

    def setUp(self) -> None:
        super().setUp()
        self.client.login(username="stageb-admin", password="testpass123")

    def test_vlan_list_renders_every_declared_column(self) -> None:
        response = self.client.get(self._list_url("vlan"))
        cells = _list_row_cells(response.content.decode(), "StageB Native")
        self.assertEqual(
            cells,
            [
                "StageB Native",
                "4077",
                "10.210.0.0/21",
                "10.210.0.1",
                "10.210.0.50",
                "10.210.0.99",
                "StageB Grillework",
                "Details",
            ],
        )

    def test_vlan_detail_renders_every_declared_field(self) -> None:
        response = self.client.get(self._detail_url("vlan"))
        content = response.content.decode()
        self.assertEqual(_detail_field_text(content, "Name"), "StageB Native")
        self.assertEqual(_detail_field_text(content, "VLAN ID"), "4077")
        self.assertEqual(_detail_field_text(content, "Subnet"), "10.210.0.0/21")
        self.assertEqual(_detail_field_text(content, "Default gateway"), "10.210.0.1")
        self.assertEqual(_detail_field_text(content, "DHCP start"), "10.210.0.50")
        self.assertEqual(_detail_field_text(content, "DHCP end"), "10.210.0.99")
        self.assertEqual(_detail_field_text(content, "Department"), "StageB Grillework")
        # Links to the department's own detail page — _linked_text_for()
        # (ADR 0021 review note 3), not a template special-case.
        self.assertIn(f'href="/models/department/{self.department.pk}/"', content)

    def test_vlan_map_shows_department_in_header(self) -> None:
        response = self.client.get(f"/vlans/{self.vlan_native.pk}/")
        href, text = _vlan_map_department_line(response.content.decode())
        self.assertEqual(href, f"/models/department/{self.department.pk}/")
        self.assertEqual(text, "StageB Grillework")

    def test_department_renders_every_declared_column(self) -> None:
        list_response = self.client.get(self._list_url("department"))
        self.assertEqual(
            _list_row_cells(list_response.content.decode(), "StageB Grillework"),
            ["StageB Grillework", "StageB Grillework Description", "Details"],
        )

        detail_response = self.client.get(self._detail_url("department"))
        content = detail_response.content.decode()
        self.assertEqual(_detail_field_text(content, "Name"), "StageB Grillework")
        self.assertEqual(_detail_field_text(content, "Description"), "StageB Grillework Description")
        # The VLANs inline — this registry's first inline with no admin
        # counterpart (ADR 0021 decision 6) — names vlan_native, which is
        # assigned to this department, and not vlan_allowed_1/2, which
        # belong to no department at all.
        self.assertEqual(
            _inline_row_cells(content, "VLANs", "StageB Native"),
            ["StageB Native", "4077", "10.210.0.0/21"],
        )
        self.assertNotIn("StageB Allowed One", content)
        self.assertNotIn("StageB Allowed Two", content)

    def test_owner_renders_every_declared_column(self) -> None:
        list_response = self.client.get(self._list_url("owner"))
        self.assertEqual(
            _list_row_cells(list_response.content.decode(), "StageB Ownership"),
            ["StageB Ownership", "stageb-owner", "Details"],
        )

        detail_response = self.client.get(self._detail_url("owner"))
        content = detail_response.content.decode()
        self.assertEqual(_detail_field_text(content, "Name"), "StageB Ownership")
        self.assertEqual(_detail_field_text(content, "Slug"), "stageb-owner")

    def test_switchportvlanprofile_renders_every_declared_column(self) -> None:
        # The value a naive `_meta.fields` walk would have dropped
        # entirely — allowed_vlans is a form field in the admin, not a
        # model field or an inline (review note 2) — and the two booleans
        # Codex flagged as under-tested (a missing boolean column and a
        # False one look identical under a presence check).
        allowed_text = "StageB Allowed One (VLAN 4078), StageB Allowed Two (VLAN 4079)"
        list_response = self.client.get(self._list_url("switchportvlanprofile"))
        self.assertEqual(
            _list_row_cells(list_response.content.decode(), "StageB Profile"),
            ["StageB Profile", "Trunk", "StageB Native (VLAN 4077)", "No", allowed_text, "No", "Details"],
        )

        detail_response = self.client.get(self._detail_url("switchportvlanprofile"))
        content = detail_response.content.decode()
        self.assertEqual(_detail_field_text(content, "Name"), "StageB Profile")
        self.assertEqual(_detail_field_text(content, "Port mode"), "Trunk")
        self.assertEqual(_detail_field_text(content, "Native VLAN"), "StageB Native (VLAN 4077)")
        self.assertEqual(_detail_field_text(content, "All VLANs allowed"), "No")
        self.assertEqual(_detail_field_text(content, "Allowed VLANs"), allowed_text)
        self.assertEqual(_detail_field_text(content, "System profile"), "No")

    def test_racktemplate_renders_every_declared_column(self) -> None:
        vlans_text = "StageB Allowed One (VLAN 4078), StageB Allowed Two (VLAN 4079)"
        list_response = self.client.get(self._list_url("racktemplate"))
        self.assertEqual(
            _list_row_cells(list_response.content.decode(), "StageB Template"),
            ["StageB Template", "12", vlans_text, "Details"],
        )

        detail_response = self.client.get(self._detail_url("racktemplate"))
        content = detail_response.content.decode()
        self.assertEqual(_detail_field_text(content, "Name"), "StageB Template")
        self.assertEqual(_detail_field_text(content, "Slot count"), "12")
        self.assertEqual(_detail_field_text(content, "VLANs"), vlans_text)

    def test_rack_list_renders_every_declared_column_and_detail_redirects(self) -> None:
        list_response = self.client.get(self._list_url("rack"))
        self.assertEqual(
            _list_row_cells(list_response.content.decode(), "StageB Rack"),
            ["StageB Rack", "10", "StageB Ownership (stageb-owner)", "stageb-location", "Details"],
        )
        detail_response = self.client.get(self._detail_url("rack"))
        self.assertEqual(detail_response.status_code, 301)

    def test_networkswitchtype_renders_every_declared_column_and_inline(self) -> None:
        list_response = self.client.get(self._list_url("networkswitchtype"))
        self.assertEqual(
            _list_row_cells(list_response.content.decode(), "StageB Switch Type"),
            ["StageB Switch Mfr", "SBSwitchModel", "StageB Switch Type", "1", "sbswtype", "Details"],
        )

        detail_response = self.client.get(self._detail_url("networkswitchtype"))
        content = detail_response.content.decode()
        self.assertEqual(_detail_field_text(content, "Manufacturer"), "StageB Switch Mfr")
        self.assertEqual(_detail_field_text(content, "Model"), "SBSwitchModel")
        self.assertEqual(_detail_field_text(content, "Name"), "StageB Switch Type")
        self.assertEqual(_detail_field_text(content, "Port count"), "1")
        self.assertEqual(_detail_field_text(content, "Hostname slug"), "sbswtype")
        self.assertEqual(
            _inline_row_cells(content, "Type ports", "StageB Switch Port Desc"),
            ["1", "StageB Switch Port Desc", "1GbE RJ45 (copper)", "StageB Profile"],
        )

    def test_networkswitch_renders_every_declared_column_and_both_inlines(self) -> None:
        switch_type_text = "StageB Switch Mfr SBSwitchModel — StageB Switch Type"
        list_response = self.client.get(self._list_url("networkswitch"))
        self.assertEqual(
            _list_row_cells(list_response.content.decode(), "stageb-switch1"),
            [
                "stageb-switch1",
                switch_type_text,
                "SBSW001",
                "StageB Rack",
                "3",
                "Yes",
                "StageB Ownership (stageb-owner)",
                "stageb-switch-purpose",
                "42",
                # #54 — this fixture's hostname is hand-typed ("stageb-switch1"),
                # unrelated to its own owner/location/type/purpose/sequence, so
                # it genuinely diverges.
                "Yes",
                "Details",
            ],
        )

        detail_response = self.client.get(self._detail_url("networkswitch"))
        content = detail_response.content.decode()
        self.assertEqual(_detail_field_text(content, "Hostname"), "stageb-switch1")
        self.assertEqual(_detail_field_text(content, "Type"), switch_type_text)
        self.assertEqual(_detail_field_text(content, "Serial number"), "SBSW001")
        self.assertEqual(_detail_field_text(content, "Rack"), "StageB Rack")
        self.assertEqual(_detail_field_text(content, "Rack slot"), "3")
        self.assertEqual(_detail_field_text(content, "DHCP server"), "Yes")
        self.assertEqual(_detail_field_text(content, "Owner"), "StageB Ownership (stageb-owner)")
        self.assertEqual(_detail_field_text(content, "Hostname purpose"), "stageb-switch-purpose")
        self.assertEqual(_detail_field_text(content, "Hostname sequence"), "42")
        self.assertEqual(_detail_field_text(content, "Diverges"), "Yes")

        # Addresses inline — the switch's materialized static address on the racked VLAN.
        assert self.switch_address.address is not None  # materialized (rack + RackVlanRange), never DHCP
        self.assertEqual(
            _inline_row_cells(content, "Addresses", self.switch_address.address),
            ["StageB Native (VLAN 4077)", self.switch_address.address],
        )
        # Ports inline — profile_summary must match switch_port_profile_summary()
        # exactly (review note 2's "reuse, do not reimplement the formatting twice").
        expected_summary = switch_port_profile_summary(self.switch_port)
        self.assertEqual(
            _inline_row_cells(content, "Ports", "StageB Switch Port Desc"),
            ["1", "1GbE RJ45 (copper)", "StageB Switch Port Desc", "StageB Profile", expected_summary],
        )

    def test_networkdevicetype_renders_every_declared_column_and_inline(self) -> None:
        list_response = self.client.get(self._list_url("networkdevicetype"))
        self.assertEqual(
            _list_row_cells(list_response.content.decode(), "StageB Device Type"),
            ["StageB Device Mfr", "SBDeviceModel", "StageB Device Type", "1", "No", "sbdevtype", "Details"],
        )

        detail_response = self.client.get(self._detail_url("networkdevicetype"))
        content = detail_response.content.decode()
        self.assertEqual(_detail_field_text(content, "Manufacturer"), "StageB Device Mfr")
        self.assertEqual(_detail_field_text(content, "Model"), "SBDeviceModel")
        self.assertEqual(_detail_field_text(content, "Name"), "StageB Device Type")
        self.assertEqual(_detail_field_text(content, "Port count"), "1")
        self.assertEqual(_detail_field_text(content, "Add-in card"), "No")
        self.assertEqual(_detail_field_text(content, "Hostname slug"), "sbdevtype")
        self.assertEqual(
            _inline_row_cells(content, "Type ports", "StageB Device Port"),
            [
                "7",
                "StageB Device Port",
                "1GbE RJ45 (copper)",
                "StageB Native (VLAN 4077)",
                "0",
                "From the device&#x27;s rack slot",
                "—",
            ],
        )

    def test_networkdevice_list_renders_every_declared_column_and_detail_redirects(self) -> None:
        device_type_text = "StageB Device Mfr SBDeviceModel — StageB Device Type"
        list_response = self.client.get(self._list_url("networkdevice"))
        self.assertEqual(
            _list_row_cells(list_response.content.decode(), "stageb-device1"),
            [
                "stageb-device1",
                device_type_text,
                "SBDEV001",
                "StageB Rack",
                "5",
                "—",
                "StageB Ownership (stageb-owner)",
                # #54 — hand-typed hostname, unrelated to its own components.
                "Yes",
                "Details",
            ],
        )
        detail_response = self.client.get(self._detail_url("networkdevice"))
        self.assertEqual(detail_response.status_code, 301)

    def test_device_shaped_page_renders_every_port_column(self) -> None:
        # The three columns review note 3 found missing from Stage A's
        # device page — port number, the numeric slot_offset, and the
        # connected switch *port*, not just the switch — checked here by
        # exact cell value, not merely that the column header exists.
        response = self.client.get(f"/devices/{self.device.pk}/")
        self.device_port.refresh_from_db()
        cells = _list_row_cells(response.content.decode(), "StageB Device Port")
        self.assertEqual(
            cells,
            [
                "StageB Device Port",
                "7",
                "VLAN 4077",
                "1GbE RJ45 (copper)",
                "0",
                self.device_port.address,
                "10.210.0.1",
                str(self.switch_port),
            ],
        )

    def test_default_gateway_renders_and_dash_for_dhcp(self) -> None:
        racked_response = self.client.get(f"/devices/{self.device.pk}/")
        racked_cells = _list_row_cells(racked_response.content.decode(), "StageB Device Port")
        self.assertEqual(racked_cells[5], self.device_port.address)  # Address column
        self.assertEqual(racked_cells[6], "10.210.0.1")  # Gateway column

        dhcp_response = self.client.get(f"/devices/{self.dhcp_device.pk}/")
        self.assertTrue(self.dhcp_device_port.is_dhcp)
        dhcp_cells = _list_row_cells(dhcp_response.content.decode(), "StageB Device Port")
        self.assertEqual(dhcp_cells[5], "DHCP")  # Address column — DHCP-configured
        self.assertEqual(dhcp_cells[6], "—")  # Gateway column — None while DHCP

    def test_relation_without_codename_renders_as_text_not_link(self) -> None:
        # Every registry list/detail page requires the codename of every
        # relation it renders (so a user who can reach the page always
        # has permission to link from it) — the one place the "degrade to
        # plain text" branch is actually reachable is the audit trail,
        # whose own required permission (auditlog.view_logentry) is
        # independent of any inventory model's view_ codename.
        self.vlan_native.name = "StageB Native Renamed"
        self.vlan_native.save()
        user = _user_missing_codename("inventory.view_vlan")
        self.client.login(username=user.username, password="testpass123")
        response = self.client.get("/audit/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("StageB Native Renamed", content)
        self.assertNotIn(f'href="/models/vlan/{self.vlan_native.pk}/"', content)

    def test_spare_pool_switch_link_targets_model_detail(self) -> None:
        spare_switch = NetworkSwitch.objects.create(
            switch_type=self.switch_type, hostname="stageb-spare-switch"
        )
        response = self.client.get("/spares/")
        self.assertContains(response, f'href="/models/networkswitch/{spare_switch.pk}/"')


class HostnameIngredientCanonicalPageTests(TestCase):
    """ADR 0023 / plan PR2 "the canonical_detail_view trap": rack_detail.html
    renders location_slug and owner; device_detail.html renders owner,
    hostname_purpose and hostname_sequence — checked through the shaped
    view URLs, since the registry's own detail_fields never render for
    either model (decision 13's redirect). ``PartialGrantAccessTests``
    already proves a Viewer without ``view_owner`` is 403'd on both — that
    codename lives in both views' own declared codename lists — so this
    class does not duplicate that check.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.owner = Owner.objects.create(slug="hi-owner", name="HI Owner")
        self.rack = Rack.objects.create(
            name="HI Rack", slot_count=4, owner=self.owner, location_slug="hi-location"
        )
        self.device_type = _make_device_type()
        self.device = NetworkDevice.objects.create(
            device_type=self.device_type,
            rack=self.rack,
            rack_slot=1,
            hostname="hi-device",
            owner=self.owner,
            hostname_purpose="hi-purpose",
            hostname_sequence=7,
        )
        self.admin_user = User.objects.create_user("hi-admin", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="hi-admin", password="testpass123")

    def test_rack_detail_renders_owner_and_location_slug(self) -> None:
        response = self.client.get(f"/racks/{self.rack.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "HI Owner")
        self.assertContains(response, "hi-location")

    def test_device_detail_renders_owner_purpose_and_sequence(self) -> None:
        response = self.client.get(f"/devices/{self.device.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "HI Owner")
        self.assertContains(response, "hi-purpose")
        self.assertContains(response, "Hostname sequence: 7")


class HostnameDivergesMarkerTests(TestCase):
    """PLAN-hostname-computation.md PR 4 "Surfacing" — the #54 marker
    renders through the canonical redirect for Rack and Device, and
    through the registry for Switch (which has no canonical page of its
    own). Also proves the query budget stays flat with a mix of
    diverging and non-diverging occupants, and covers the explicit
    decision that spare_pool.html carries the marker too.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.owner = Owner.objects.create(slug="mps", name="MPS")
        self.rack = Rack.objects.create(
            name="Diverge Rack", slot_count=10, owner=self.owner, location_slug="wpcsrl"
        )
        self.device_type = _make_device_type(hostname_slug="ik42", name="Diverge Device Type")
        self.switch_type = _make_switch_type(hostname_slug="sg300", name="Diverge Switch Type")
        self.admin_user = User.objects.create_user("diverge-admin", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="diverge-admin", password="testpass123")

    def test_rack_detail_marks_a_diverging_device(self) -> None:
        device = NetworkDevice.objects.create(
            device_type=self.device_type,
            rack=self.rack,
            rack_slot=1,
            owner=self.owner,
            hostname="hand-typed",
        )
        self.assertTrue(device.hostname_diverges)
        response = self.client.get(f"/racks/{self.rack.pk}/")
        row1 = _row_html(response.content.decode(), 1)
        self.assertIn("badge--diverges", row1)

    def test_rack_detail_does_not_mark_a_matching_switch(self) -> None:
        NetworkSwitch.objects.create(
            switch_type=self.switch_type,
            rack=self.rack,
            rack_slot=2,
            owner=self.owner,
            hostname="mps-wpcsrl-sg300",
        )
        response = self.client.get(f"/racks/{self.rack.pk}/")
        row2 = _row_html(response.content.decode(), 2)
        self.assertNotIn("badge--diverges", row2)

    def test_device_detail_marks_a_diverging_device(self) -> None:
        device = NetworkDevice.objects.create(
            device_type=self.device_type,
            rack=self.rack,
            rack_slot=1,
            owner=self.owner,
            hostname="hand-typed",
        )
        response = self.client.get(f"/devices/{device.pk}/")
        self.assertContains(response, "badge--diverges")

    def test_switch_registry_list_marks_a_diverging_switch(self) -> None:
        """NetworkSwitch has no canonical page — the registry list is the
        only place a Viewer can see this at all.

        Asserted on the specific row/column (code review finding 5b), not
        a bare ``assertContains(response, "Yes")`` — the list also has a
        "DHCP server" boolean column, so a page-wide "Yes" search would
        pass even if the "Diverges" column rendered nothing at all, as
        long as some *other* row's DHCP server happened to be enabled.
        """
        NetworkSwitch.objects.create(
            switch_type=self.switch_type,
            rack=self.rack,
            rack_slot=1,
            owner=self.owner,
            hostname="hand-typed",
        )
        response = self.client.get("/models/networkswitch/")
        cells = _list_row_cells(response.content.decode(), "hand-typed")
        # ... Hostname, Type, Serial number, Rack, Rack slot, DHCP server,
        # Owner, Hostname purpose, Hostname sequence, Diverges, Details.
        self.assertEqual(cells[-2], "Yes")  # "Diverges" — the second-to-last column, before "Details"

    def test_switch_registry_detail_marks_a_diverging_switch(self) -> None:
        switch = NetworkSwitch.objects.create(
            switch_type=self.switch_type,
            rack=self.rack,
            rack_slot=1,
            owner=self.owner,
            hostname="hand-typed",
        )
        response = self.client.get(f"/models/networkswitch/{switch.pk}/")
        self.assertEqual(_detail_field_text(response.content.decode(), "Diverges"), "Yes")

    def test_spare_pool_marks_a_diverging_unracked_device(self) -> None:
        device = NetworkDevice.objects.create(
            device_type=self.device_type, owner=self.owner, hostname="hand-typed"
        )
        self.assertIsNone(device.rack)
        self.assertTrue(device.hostname_diverges)
        response = self.client.get("/spares/")
        self.assertContains(response, "badge--diverges")

    def test_spare_pool_does_not_mark_a_matching_switch(self) -> None:
        NetworkSwitch.objects.create(switch_type=self.switch_type, owner=self.owner, hostname="mps-sg300")
        response = self.client.get("/spares/")
        self.assertNotContains(response, "badge--diverges")

    def test_elevation_query_count_independent_of_divergence_mix(self) -> None:
        """The marker itself must not turn the elevation's flat query
        budget into an N+1 across occupants — some diverging, some not.
        """
        for slot in range(1, 3):
            NetworkDevice.objects.create(
                device_type=self.device_type,
                rack=self.rack,
                rack_slot=slot,
                owner=self.owner,
                hostname="hand-typed" if slot % 2 else "mps-wpcsrl-ik42",
            )
        with CaptureQueriesContext(connection) as small_ctx:
            small_response = self.client.get(f"/racks/{self.rack.pk}/")
        for slot in range(3, 10):
            NetworkDevice.objects.create(
                device_type=self.device_type,
                rack=self.rack,
                rack_slot=slot,
                owner=self.owner,
                hostname="hand-typed" if slot % 2 else "mps-wpcsrl-ik42",
            )
        with CaptureQueriesContext(connection) as big_ctx:
            big_response = self.client.get(f"/racks/{self.rack.pk}/")
        self.assertEqual(small_response.status_code, 200)
        self.assertEqual(big_response.status_code, 200)
        self.assertEqual(len(small_ctx.captured_queries), len(big_ctx.captured_queries))


class DerivedPortHostnameRenderingTests(TestCase):
    """ADR 0022 — the canonical device page renders a port's derived
    hostname where it has one (``NetworkDevicePort.hostname``), and
    renders nothing extra for a port with no ``hostname_suffix``.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.admin_user = User.objects.create_user("hostnameadmin", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="hostnameadmin", password="testpass123")
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="DiGiCo", model="SD12", name="SD12 UI", port_count=2
        )
        NetworkDeviceTypePort.objects.create(
            device_type=self.device_type, description="Control", port_type=PortType.GBE_RJ45, vlan=self.vlan
        )
        NetworkDeviceTypePort.objects.create(
            device_type=self.device_type,
            description="Engine",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
            slot_offset=1,
            hostname_suffix="engine",
        )
        self.device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=self.device_type, hostname="sd12-96-1", port_addressing=PortAddressing.DHCP
        )

    def test_derived_hostname_renders_for_suffixed_port(self) -> None:
        response = self.client.get(f"/devices/{self.device.pk}/")
        self.assertContains(response, "sd12-96-1-engine")

    def test_no_derived_hostname_rendered_for_unsuffixed_port(self) -> None:
        response = self.client.get(f"/devices/{self.device.pk}/")
        cells = _list_row_cells(response.content.decode(), "Control")
        self.assertEqual(cells[0], "Control")  # no parenthetical hostname, unlike the Engine row


class OperatorSetTagRenderingTests(TestCase):
    """ADR 0022 / issue #60 — the canonical device page's operator-set tag
    tracks ``NetworkDevicePort.is_operator_addressed`` exactly (Property
    tests mirrored at the rendering layer, PLAN-consumed-slot-addresses.md
    Tests section): present only for a static ``OPERATOR``-sourced port,
    absent for every case the property returns ``False`` for.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.admin_user = User.objects.create_user("optag-admin", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="optag-admin", password="testpass123")

        self.vlan = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.rack = Rack.objects.create(name="OpTag Rack", slot_count=10)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.201.6.0/27")
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="Yamaha", model="DM7C", name="OpTag Console", port_count=2
        )
        NetworkDeviceTypePort.objects.create(
            device_type=self.device_type,
            description="Dante Primary",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
        )
        NetworkDeviceTypePort.objects.create(
            device_type=self.device_type,
            description="Device Control",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
            address_source=PortAddressSource.OPERATOR,
            hostname_suffix="device-control",
        )

    def test_tag_present_for_static_operator_port_absent_for_ordinary_slot_port(self) -> None:
        device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=self.device_type,
            rack=self.rack,
            rack_slot=5,
            hostname="dm7c-optag",
            operator_addresses={"Device Control": "10.201.6.4"},
        )
        response = self.client.get(f"/devices/{device.pk}/")
        content = response.content.decode()
        control_cells = _list_row_cells(content, "Device Control")
        primary_cells = _list_row_cells(content, "Dante Primary")
        self.assertIn("operator-set", control_cells[0])
        self.assertNotIn("operator-set", primary_cells[0])

    def test_tag_absent_for_dhcp_ports_both_slot_and_operator_sourced(self) -> None:
        # Unracked -> every port materializes DHCP, including the
        # OPERATOR-sourced one — it typed no address and consumed
        # nothing, so it must not carry the tag either.
        device = NetworkDevice.objects.create(device_type=self.device_type, hostname="dm7c-optag-dhcp")
        response = self.client.get(f"/devices/{device.pk}/")
        content = response.content.decode()
        control_cells = _list_row_cells(content, "Device Control")
        primary_cells = _list_row_cells(content, "Dante Primary")
        self.assertNotIn("operator-set", control_cells[0])
        self.assertNotIn("operator-set", primary_cells[0])

    def test_tag_absent_for_port_with_no_source_type_port(self) -> None:
        bare_type = NetworkDeviceType.objects.create(
            manufacturer="Test", model="Bare", name="OpTag Bare", port_count=0
        )
        bare_device = NetworkDevice.objects.create(device_type=bare_type, hostname="optag-bare")
        NetworkDevicePort.objects.create(
            device=bare_device, description="Hand Wired", vlan=self.vlan, address="10.201.6.4"
        )
        response = self.client.get(f"/devices/{bare_device.pk}/")
        cells = _list_row_cells(response.content.decode(), "Hand Wired")
        self.assertNotIn("operator-set", cells[0])


class AddInCardRenderingTests(TestCase):
    """ADR 0022 PR 3 — the canonical device page's "Fitted to …" line on a
    card and "Cards fitted" list on a host.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.admin_user = User.objects.create_user("cardui-admin", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="cardui-admin", password="testpass123")

        self.host_type = NetworkDeviceType.objects.create(
            manufacturer="CardUI", model="Console", name="Default", port_count=0
        )
        self.card_type = NetworkDeviceType.objects.create(
            manufacturer="CardUI", model="Card", name="Default", port_count=0, is_add_in_card=True
        )
        self.host = NetworkDevice.objects.create(device_type=self.host_type, hostname="cardui-host")
        self.card = NetworkDevice.objects.create(
            device_type=self.card_type, hostname="cardui-card", host=self.host
        )
        self.hostless_card = NetworkDevice.objects.create(
            device_type=self.card_type, hostname="cardui-hostless"
        )

    def test_fitted_to_line_renders_on_the_card(self) -> None:
        response = self.client.get(f"/devices/{self.card.pk}/")
        self.assertContains(response, "Fitted to")
        self.assertContains(response, f'href="/devices/{self.host.pk}/"')
        self.assertContains(response, "cardui-host")

    def test_no_fitted_to_line_for_a_hostless_card(self) -> None:
        response = self.client.get(f"/devices/{self.hostless_card.pk}/")
        self.assertNotContains(response, "Fitted to")

    def test_cards_fitted_panel_renders_on_the_host(self) -> None:
        response = self.client.get(f"/devices/{self.host.pk}/")
        self.assertContains(response, "Cards fitted")
        self.assertContains(response, f'href="/devices/{self.card.pk}/"')
        self.assertContains(response, "cardui-card")
        self.assertNotContains(response, "cardui-hostless")

    def test_no_cards_fitted_panel_for_a_device_with_no_cards(self) -> None:
        response = self.client.get(f"/devices/{self.hostless_card.pk}/")
        self.assertNotContains(response, "Cards fitted")


class AuditTrailTests(TestCase):
    """One test per live crash path (decision 15) — each of these is a
    real ``django-auditlog`` shape this view must survive without a 500.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.admin_user = User.objects.create_user("audit-admin", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="audit-admin", password="testpass123")

    def test_scalar_change_renders_old_arrow_new(self) -> None:
        vlan = VLAN.objects.create(name="Audit Original Name", vlan_id=220, subnet="10.220.0.0/24")
        vlan.name = "Audit Renamed"
        vlan.save()
        response = self.client.get("/audit/")
        self.assertContains(response, "Audit Original Name → Audit Renamed")

    def test_m2m_change_renders_operation_and_objects(self) -> None:
        vlan_a = VLAN.objects.create(name="Audit M2M A", vlan_id=221, subnet="10.221.0.0/24")
        template = RackTemplate.objects.create(name="Audit Template")
        template.vlans.add(vlan_a)
        response = self.client.get("/audit/")
        # One combined, exact string — "add" and str(vlan_a) checked
        # separately would each pass even if the m2m renderer only ever
        # produced one of the two ("add" is also common English word risk
        # on a page with an "All" filter option).
        self.assertContains(response, f"add: {vlan_a}")

    def test_null_or_empty_changes_renders_no_field_changes_recorded(self) -> None:
        content_type = ContentType.objects.get_for_model(VLAN)
        LogEntry.objects.create(
            content_type=content_type,
            object_pk="1",
            object_id=1,
            object_repr="Audit Empty Changes",
            action=LogEntry.Action.ACCESS,
            changes=None,
        )
        response = self.client.get("/audit/")
        self.assertContains(response, "No field changes recorded")

    def test_deleted_actor_falls_back_to_actor_email_then_system(self) -> None:
        actor = User.objects.create_user("audit-actor-to-delete", password="testpass123")
        content_type = ContentType.objects.get_for_model(VLAN)
        entry_with_email = LogEntry.objects.create(
            content_type=content_type,
            object_pk="2",
            object_id=2,
            object_repr="Audit Deleted Actor With Email",
            action=LogEntry.Action.UPDATE,
            actor=actor,
            actor_email="deleted-actor@example.com",
        )
        entry_without_email = LogEntry.objects.create(
            content_type=content_type,
            object_pk="3",
            object_id=3,
            object_repr="Audit Deleted Actor No Email",
            action=LogEntry.Action.UPDATE,
            actor=actor,
        )
        actor.delete()  # actor is SET_NULL — both rows survive with actor_id now NULL
        entry_with_email.refresh_from_db()
        entry_without_email.refresh_from_db()
        self.assertIsNone(entry_with_email.actor_id)

        response = self.client.get("/audit/")
        content = response.content.decode()
        # Tied to each row specifically (not just "somewhere on the
        # page") — a renderer that always fell back to one of the two
        # strings regardless of which row it was rendering would still
        # pass a bare assertContains of both strings.
        self.assertEqual(
            _audit_row_cells(content, "Audit Deleted Actor With Email")[1], "deleted-actor@example.com"
        )
        self.assertEqual(
            _audit_row_cells(content, "Audit Deleted Actor No Email")[1], "system or deleted actor"
        )

    def test_unregistered_content_type_renders_200_not_the_changes_display_dict_crash(self) -> None:
        # LogEntry.changes_display_dict() would raise AttributeError here
        # (model_class() is None, then .content_type.model_class()._meta
        # fails) — this view's own renderer must not.
        ghost_content_type = ContentType.objects.create(app_label="inventory", model="ghostmodel")
        LogEntry.objects.create(
            content_type=ghost_content_type,
            object_pk="4",
            object_id=4,
            object_repr="A Ghost Object",
            action=LogEntry.Action.CREATE,
            changes={"name": ["old", "new"]},
        )
        response = self.client.get("/audit/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A Ghost Object")

    def test_field_scoped_model_shows_only_tracked_fields_and_coverage_note(self) -> None:
        # NetworkSwitch is tracked with include_fields=["hostname", "rack",
        # "rack_slot", "owner", "hostname_purpose", "hostname_sequence",
        # "created_at"] (settings.py) — hostname joined this list in ADR
        # 0023/phase 18 PR 3, but serial_number never did, and a
        # serial_number-only edit is still not tracked at all, which the
        # page must say plainly.
        switch_type = _make_switch_type(port_count=0)
        switch = NetworkSwitch.objects.create(switch_type=switch_type, serial_number="SN-ORIGINAL")
        switch_content_type = ContentType.objects.get_for_model(NetworkSwitch)
        count_after_create = LogEntry.objects.filter(
            content_type=switch_content_type, object_id=switch.pk
        ).count()

        switch.serial_number = "SN-RENAMED"
        switch.save()

        # serial_number isn't in NetworkSwitch's include_fields, so this
        # edit produces no *new* LogEntry at all (auditlog only writes an
        # UPDATE row when a tracked field actually changed) — the coverage
        # note on the page is what keeps that silence from reading as
        # "nothing changed" rather than "this field isn't tracked".
        count_after_rename = LogEntry.objects.filter(
            content_type=switch_content_type, object_id=switch.pk
        ).count()
        self.assertEqual(count_after_create, count_after_rename)

        response = self.client.get("/audit/")
        self.assertContains(response, "Tracked fields differ by model")


class AuditPaginationFilterTests(TestCase):
    """Pagination boundaries (decision 16's ``-pk`` tiebreak), the three
    filters, and per-object history's own narrowing.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.admin_user = User.objects.create_user("audit-pg-admin", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="audit-pg-admin", password="testpass123")

    def test_51_entries_across_two_pages_no_duplicate_no_missing(self) -> None:
        vlan = VLAN.objects.create(name="Pagination VLAN", vlan_id=230, subnet="10.230.0.0/24")
        for i in range(51):
            vlan.name = f"Pagination VLAN rename {i}"
            vlan.save()
        all_entries = list(
            LogEntry.objects.filter(content_type=ContentType.objects.get_for_model(VLAN), object_id=vlan.pk)
        )
        self.assertEqual(len(all_entries), 52)  # the create + 51 renames

        page1 = self.client.get("/audit/")
        page2 = self.client.get("/audit/?page=2")
        self.assertEqual(page1.status_code, 200)
        self.assertEqual(page2.status_code, 200)
        pks_1 = set(re.findall(r"data-logentry-pk=\"(\d+)\"", page1.content.decode()))
        pks_2 = set(re.findall(r"data-logentry-pk=\"(\d+)\"", page2.content.decode()))
        self.assertEqual(pks_1 & pks_2, set())
        self.assertEqual(len(pks_1), 50)
        self.assertGreaterEqual(len(pks_2), 2)

    def test_each_filter_narrows_correctly(self) -> None:
        # A negative control per filter (Codex review — the previous
        # version of this test only ever checked that the wanted entry was
        # present, which the *unfiltered* page would satisfy too; removing
        # the filter entirely would have kept it green): a second actor,
        # and a different content type, each of which must disappear when
        # filtered against.
        other_user = User.objects.create_user("audit-pg-other", password="testpass123", is_staff=True)

        vlan_a = VLAN.objects.create(name="Filter VLAN A", vlan_id=231, subnet="10.231.0.0/24")
        VLAN.objects.create(name="Filter VLAN B", vlan_id=232, subnet="10.232.0.0/24")
        # auditlog only attaches an actor via its middleware, which reads
        # the current request's user — a plain save() outside a request
        # cycle has no request to read, so the actor filter needs
        # set_actor() to get a non-null actor onto this LogEntry at all.
        with set_actor(actor=self.admin_user):
            vlan_a.name = "Filter VLAN Renamed By Admin"
            vlan_a.save()
        with set_actor(actor=other_user):
            vlan_a.name = "Filter VLAN Renamed By Other"
            vlan_a.save()
        Rack.objects.create(name="Filter Rack Decoy", slot_count=3)  # a different content type entirely

        actor_response = self.client.get(f"/audit/?actor={self.admin_user.pk}")
        self.assertContains(actor_response, "Filter VLAN Renamed By Admin")
        self.assertNotContains(actor_response, "Filter VLAN Renamed By Other")  # a different actor

        action_response = self.client.get(f"/audit/?action={LogEntry.Action.CREATE}")
        self.assertContains(action_response, "Filter VLAN B")
        self.assertNotContains(action_response, "Filter VLAN Renamed By Admin")  # an UPDATE, not a CREATE
        self.assertNotContains(action_response, "Filter VLAN Renamed By Other")

        content_type_pk = ContentType.objects.get_for_model(VLAN).pk
        content_type_response = self.client.get(f"/audit/?content_type={content_type_pk}")
        self.assertContains(content_type_response, "Filter VLAN Renamed By Admin")
        self.assertContains(content_type_response, "Filter VLAN B")
        self.assertNotContains(content_type_response, "Filter Rack Decoy")  # a different content type

    def test_unparseable_filter_value_renders_200_with_note(self) -> None:
        response = self.client.get("/audit/?action=banana")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ignored unrecognised filter value")

    def test_per_object_history_returns_only_that_objects_entries(self) -> None:
        switch_type = _make_switch_type(port_count=1)
        rack = Rack.objects.create(name="Panel Rack", slot_count=5)
        switch = NetworkSwitch.objects.create(
            switch_type=switch_type, rack=rack, rack_slot=1, hostname="panel-switch"
        )
        decoy = NetworkSwitch.objects.create(
            switch_type=switch_type, rack=rack, rack_slot=2, hostname="panel-decoy"
        )
        switch.rack_slot = 1  # touch a tracked field so a fresh UPDATE LogEntry exists
        switch.dhcp_server_enabled = True
        switch.save()
        decoy.dhcp_server_enabled = True
        decoy.save()

        response = self.client.get(f"/models/networkswitch/{switch.pk}/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("panel-switch", content)
        # The panel is per-object — the decoy's own LogEntry pk must not
        # leak onto this page even though it shares the same content type.
        decoy_entry = LogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(NetworkSwitch), object_id=decoy.pk
        ).latest("timestamp")
        self.assertNotIn(f'data-logentry-pk="{decoy_entry.pk}"', content)


# ---------------------------------------------------------------------------
# Stage C — Viewers leave the admin
# ---------------------------------------------------------------------------


class UrlconfCoverageTests(ParityFixtureMixin, TestCase):
    """Guards the shared route enumeration itself (``_shaped_routes``/
    ``_parity_routes``/``_all_ui_routes`` above) against silent drift.

    Codex review found the first revision of this guard checked
    ``inventory.urls.urlpatterns`` against ``_SHAPED_ROUTE_NAMES`` /
    ``_PARITY_ROUTE_NAMES`` — two hand-written frozensets sitting next to
    the builders, not the builders' own output. That is a **third**
    hand-maintained list, and it demonstrably doesn't guard what it
    claims: delete the ``device`` line from ``_shaped_routes()`` and the
    frozensets still say ``"device"``, ``urlpatterns`` still says
    ``"device"``, and the guard stays green while every sweep built on the
    enumeration — including the Stage C admin-lockout test — silently
    narrows. ``_covered_route_names()`` fixes this by resolving every URL
    the builders actually emit and reading back the ``url_name`` Django
    itself assigns, so what's under test is the builders' real output.
    """

    def test_route_builders_cover_every_urlconf_pattern(self) -> None:
        covered_names = _covered_route_names(
            rack_pk=self.rack.pk,
            vlan_pk=self.vlan_native.pk,
            device_pk=self.device.pk,
            pk_by_slug=self.pk_by_slug,
        )
        actual_names = {pattern.name for pattern in inventory_urls.urlpatterns}
        self.assertEqual(covered_names, actual_names)

        # A name-set comparison alone can't catch two different patterns
        # sharing one name (Codex review) — this app's reverse()-by-name
        # usage throughout this module assumes every name in ``urls.py``
        # is used by exactly one pattern, so assert the pattern count
        # against the distinct-name count too: if they ever diverge, some
        # name in ``urlpatterns`` is no longer unique.
        self.assertEqual(len(inventory_urls.urlpatterns), len(actual_names))

    def test_guard_is_not_vacuous_a_dropped_route_would_fail_it(self) -> None:
        """Proves the assertion above would actually catch the failure
        Codex review demonstrated — a route silently dropped from one of
        the builders — rather than passing regardless of what they
        produce. Simulates the drop (removing ``"device"`` from the
        covered-names set) instead of editing ``_shaped_routes()`` itself,
        since editing the real builder would also break every other test
        that depends on it.
        """
        covered_names = _covered_route_names(
            rack_pk=self.rack.pk,
            vlan_pk=self.vlan_native.pk,
            device_pk=self.device.pk,
            pk_by_slug=self.pk_by_slug,
        )
        actual_names = {pattern.name for pattern in inventory_urls.urlpatterns}
        self.assertIn("device", covered_names)  # sanity: the route this simulation drops is really there
        self.assertNotEqual(covered_names - {"device"}, actual_names)


class AdminLockoutTests(ParityFixtureMixin, TestCase):
    """Stage C's real gate (ADR 0020 decision 7, plan Stage C). No code
    flips a Viewer's ``is_staff`` — existing Viewers are flipped by hand in
    the admin — so nothing here exercises ``sync_roles.py``. What this
    proves is the *consequence* of that flip: ``AdminSite.has_permission()``
    gates on ``is_active and is_staff``, so ``is_staff=False`` is a total
    lockout from every admin page, and that is only safe because Stages A
    and B brought the read-only UI to parity with what a Viewer could
    previously see in the admin. This test is what certifies that parity
    is real — it reaches every route this app serves outside ``/admin/``
    itself, as the exact user shape ADR 0020 decision 7 describes, and
    separately proves the admin refuses that same user precisely (not just
    "not 200").
    """

    def test_non_staff_viewer_reaches_every_ui_route(self) -> None:
        """Status codes alone can't tell "this route serves the real page"
        from "this route serves an empty or wrong one" (Codex review) — a
        200 with an error-shaped or blank body, or a 301 to the login page,
        would both pass a status-only sweep. Every 200 route below is
        checked against a fixture value that could only render from that
        route's own data (``ParityFixtureMixin``'s own docstring already
        chose these values so none is a substring of another rendered value
        on the same page); every 301 is checked against its exact redirect
        target, not merely a 3xx.
        """
        self.client.login(username=self.non_staff_viewer.username, password="testpass123")

        # Rack and NetworkDevice's model_detail routes redirect (decision
        # 13) to their canonical shaped page — checked against the *exact*
        # target, since any 3xx (including one to the login page) would
        # otherwise pass.
        canonical_targets = {
            reverse("inventory:model_detail", args=[slug, self.pk_by_slug[slug]]): reverse(
                spec.canonical_detail_view, args=[self.pk_by_slug[slug]]
            )
            for slug, spec in REGISTRY.items()
            if spec.canonical_detail_view
        }

        # A route-specific marker for every other route — reusing the
        # exact distinctive fixture values ``ParityContentTests`` already
        # proved render in these positions, rather than inventing a second
        # set of expected values.
        markers = {
            reverse("inventory:index"): "StageB Rack",
            reverse("inventory:rack", args=[self.rack.pk]): "stageb-device1",
            reverse("inventory:vlan_map", args=[self.vlan_native.pk]): "StageB Native",
            reverse("inventory:device", args=[self.device.pk]): "StageB Device Port",
            reverse("inventory:spares"): "stageb-dhcp-device",
            reverse("inventory:audit"): "stageb-switch1",
            self._list_url("vlan"): "StageB Native",
            self._detail_url("vlan"): "StageB Native",
            self._list_url("department"): "StageB Grillework",
            self._detail_url("department"): "StageB Grillework",
            self._list_url("owner"): "StageB Ownership",
            self._detail_url("owner"): "StageB Ownership",
            self._list_url("switchportvlanprofile"): "StageB Profile",
            self._detail_url("switchportvlanprofile"): "StageB Profile",
            self._list_url("racktemplate"): "StageB Template",
            self._detail_url("racktemplate"): "StageB Template",
            self._list_url("rack"): "StageB Rack",
            self._list_url("networkswitchtype"): "StageB Switch Type",
            self._detail_url("networkswitchtype"): "StageB Switch Type",
            self._list_url("networkswitch"): "stageb-switch1",
            self._detail_url("networkswitch"): "stageb-switch1",
            self._list_url("networkdevicetype"): "StageB Device Type",
            self._detail_url("networkdevicetype"): "StageB Device Type",
            self._list_url("networkdevice"): "stageb-device1",
        }

        routes = _all_ui_routes(
            rack_pk=self.rack.pk,
            vlan_pk=self.vlan_native.pk,
            device_pk=self.device.pk,
            pk_by_slug=self.pk_by_slug,
        )
        self.assertGreater(len(routes), 0)  # a broken enumeration must not vacuously pass
        # Every route the enumeration produces must be accounted for by
        # exactly one of the two dicts above — proof this test's own
        # expectations are as exhaustive as the enumeration, not a subset
        # of it that would silently stop growing.
        self.assertEqual(set(routes), set(canonical_targets) | set(markers))

        for route in routes:
            if route in canonical_targets:
                response = self.client.get(route)
                self.assertEqual(response.status_code, 301, route)
                self.assertEqual(response["Location"], canonical_targets[route], route)
            else:
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200, route)
                self.assertContains(response, markers[route], msg_prefix=f"{route}: ")

    def test_non_staff_viewer_refused_by_admin_precisely(self) -> None:
        # A bare assertNotEqual(status, 200) would also pass if "/admin/"
        # simply didn't resolve, which proves nothing about the lockout.
        # Django's admin redirects an unauthorised user to its own login
        # page rather than 403ing, so assert that specific redirect target,
        # then follow it and assert the Viewer does not end up logged into
        # the admin.
        self.client.login(username=self.non_staff_viewer.username, password="testpass123")
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/admin/login/"), response["Location"])

        # AdminSite.login() re-checks has_permission() on the way in and,
        # since it's still False, renders its own "authenticated but not
        # authorized" notice (admin/login.html) rather than the "Site
        # administration" index — the precise proof that an authenticated,
        # non-staff Viewer never actually gets past the door, not merely
        # that the URL resolved to *something*.
        login_page = self.client.get(response["Location"])
        self.assertEqual(login_page.status_code, 200)
        self.assertContains(login_page, "not authorized to access this page")
        self.assertContains(login_page, self.non_staff_viewer.username)
