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
from django.urls import reverse

from .models import (
    VLAN,
    NetworkDevice,
    NetworkDeviceType,
    NetworkDeviceTypePort,
    NetworkSwitch,
    NetworkSwitchType,
    NetworkSwitchTypePort,
    PortMode,
    PortType,
    Rack,
    RackTemplate,
    RackVlanRange,
    SwitchPortVlanProfile,
    switch_port_profile_summary,
)
from .suggestions import suggest_rack_vlan_range, suggest_slot_address
from .views import REGISTRY, resolve_slot_spans, safe_slot_address

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
    """
    return re.findall(r'<td class="cell cell-(\w+)"', row_html)


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

        self.vlan_native = VLAN.objects.create(
            name="StageB Native", vlan_id=210, subnet="10.210.0.0/21", default_gateway="10.210.0.1"
        )
        self.vlan_allowed_1 = VLAN.objects.create(
            name="StageB Allowed One", vlan_id=211, subnet="10.211.0.0/21"
        )
        self.vlan_allowed_2 = VLAN.objects.create(
            name="StageB Allowed Two", vlan_id=212, subnet="10.212.0.0/21"
        )

        self.profile = SwitchPortVlanProfile.objects.create(
            name="StageB Profile", port_mode=PortMode.TRUNK, native_vlan=self.vlan_native
        )
        self.profile.allowed_vlans.set([self.vlan_allowed_1, self.vlan_allowed_2])

        self.rack_template = RackTemplate.objects.create(name="StageB Template", slot_count=12)
        self.rack_template.vlans.set([self.vlan_allowed_1, self.vlan_allowed_2])

        self.rack = Rack.objects.create(name="StageB Rack", slot_count=10)
        self.rack_vlan_range = RackVlanRange.objects.create(
            rack=self.rack, vlan=self.vlan_native, address_range="10.210.1.0/27"
        )

        self.switch_type = NetworkSwitchType.objects.create(
            manufacturer="StageB Switch Mfr", model="SBSwitchModel", name="StageB Switch Type", port_count=1
        )
        NetworkSwitchTypePort.objects.create(
            switch_type=self.switch_type, port_number=1, port_type=PortType.GBE_RJ45, profile=self.profile
        )
        self.switch = NetworkSwitch.objects.create(
            switch_type=self.switch_type,
            rack=self.rack,
            rack_slot=1,
            hostname="stageb-switch1",
            serial_number="SBSW001",
            dhcp_server_enabled=True,
        )
        self.switch_port = self.switch.ports.get()
        self.switch_address = self.switch.addresses.get()

        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="StageB Device Mfr", model="SBDeviceModel", name="StageB Device Type", port_count=1
        )
        NetworkDeviceTypePort.objects.create(
            device_type=self.device_type,
            description="StageB Device Port",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_native,
            slot_offset=0,
        )
        self.device = NetworkDevice.objects.create(
            device_type=self.device_type,
            rack=self.rack,
            rack_slot=2,
            hostname="stageb-device1",
            serial_number="SBDEV001",
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

        self.pk_by_slug = {
            "vlan": self.vlan_native.pk,
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
        return [
            "/",
            f"/racks/{self.rack.pk}/",
            f"/vlans/{self.vlan.pk}/",
            f"/devices/{self.device.pk}/",
            "/spares/",
        ]

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

        self.client.login(username="viewer2", password="testpass123")
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
        # sweep below actually exercises all eight /models/<slug>/ list
        # pages and the six non-redirecting detail pages, not just the
        # four models Stage A already had fixtures for.
        self.profile = SwitchPortVlanProfile.objects.create(name="WN Profile", native_vlan=self.vlan)
        self.rack_template = RackTemplate.objects.create(name="WN Template", slot_count=5)

        self.admin_user = User.objects.create_user("adminrole", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adminrole", password="testpass123")

    def test_get_sweep_executes_no_mutating_sql_and_changes_no_row_counts(self) -> None:
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


class ElevationEncodingTests(TestCase):
    """The four rack-elevation encodings, each asserted at a specific
    coordinate with a negative control (review note 7) — a wrong-cell pass
    is exactly what a presence-only assertion would miss.
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

        # ADR 0018 companion pair: host (Control, vlan_a) at slot 15,
        # companion (Dante Primary, vlan_b) at slot 12 — deliberately
        # non-adjacent, with a second decoy sitting between them, so the
        # tether can only be proven correct by pk, never by proximity.
        self.companion_type = _make_device_type(
            port_count=1,
            vlan=self.vlan_b,
            manufacturer="Yamaha",
            model="DM7C",
            name="Device Control Interface",
        )
        self.host_type = _make_device_type(
            port_count=1, vlan=self.vlan_a, manufacturer="Yamaha", model="DM7C", name="Default"
        )
        self.host_type.companion_type = self.companion_type
        self.host_type.save()
        self.host = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=self.host_type,
            rack=self.rack,
            rack_slot=15,
            companion_rack_slot=12,
            hostname="dm7c-1",
        )
        self.companion = self.host.companion
        self.between_decoy = NetworkDevice.objects.create(
            device_type=self.plain_type, rack=self.rack, rack_slot=13, hostname="between-decoy"
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

    def test_tether_joins_the_actual_pair_not_the_decoy_between_them(self) -> None:
        host_row = _row_html(self.content, 15)
        companion_row = _row_html(self.content, 12)
        between_decoy_row = _row_html(self.content, 13)
        self.assertIn(f'data-tether-pk="{self.companion.pk}"', host_row)
        self.assertIn(f'data-tether-pk="{self.host.pk}"', companion_row)
        self.assertNotIn("tether", between_decoy_row)
        self.assertNotIn(f'data-tether-pk="{self.between_decoy.pk}"', self.content)

    def test_em_dash_at_specific_intersection_not_where_port_exists(self) -> None:
        # Columns are ordered by vlan__vlan_id: vlan_a (200) then vlan_b (201).
        start_row_states = _cell_states(_row_html(self.content, 5))
        self.assertEqual(start_row_states, ["occupied", "absent"])  # Control present, no Dante port
        continuation_row_states = _cell_states(_row_html(self.content, 6))
        self.assertEqual(continuation_row_states, ["occupied", "absent"])  # Engine present, no Dante port

        host_row_states = _cell_states(_row_html(self.content, 15))
        self.assertEqual(host_row_states, ["occupied", "absent"])  # Control present, no Dante port
        companion_row_states = _cell_states(_row_html(self.content, 12))
        self.assertEqual(companion_row_states, ["absent", "occupied"])  # no Control port, Dante present

    def test_empty_ordinal_shows_the_address_suggest_slot_address_returns(self) -> None:
        empty_row = _row_html(self.content, 7)
        expected_a = suggest_slot_address("10.200.1.0/27", 7)
        expected_b = suggest_slot_address("10.201.1.0/27", 7)
        self.assertIn(expected_a, empty_row)
        self.assertIn(expected_b, empty_row)

    def test_resolve_slot_spans_agrees_with_slot_span_property(self) -> None:
        for device in [self.bracket_device, self.decoy, self.host, self.companion, self.between_decoy]:
            spans = resolve_slot_spans([device])
            self.assertEqual(
                spans[device.device_type_id],
                device.device_type.slot_span,
                device.device_type,
            )


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


class UnrackedCompanionTetherTests(TestCase):
    """The companion tether (ADR 0018) must render for an unracked pair
    too — the link is existence and lifecycle, not addressing, so it
    doesn't come and go with placement (Codex review round 2, finding 6).
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.dante_vlan = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.control_vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")

        self.companion_type = _make_device_type(
            port_count=1,
            vlan=self.dante_vlan,
            manufacturer="Yamaha",
            model="DM7C",
            name="Device Control Interface",
        )
        self.host_type = _make_device_type(
            port_count=1, vlan=self.control_vlan, manufacturer="Yamaha", model="DM7C", name="Default"
        )
        self.host_type.companion_type = self.companion_type
        self.host_type.save()

        # Unracked — no rack/rack_slot on the host, so none on the
        # materialized companion either (both spare-pool, per ADR 0018).
        self.host = NetworkDevice.objects.create(device_type=self.host_type, hostname="unracked-dm7c-1")
        self.companion = self.host.companion

        self.admin_user = User.objects.create_user("adminrole", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adminrole", password="testpass123")

    def test_host_detail_shows_tether_to_unracked_companion(self) -> None:
        response = self.client.get(f"/devices/{self.host.pk}/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(f'data-tether-pk="{self.companion.pk}"', content)
        self.assertIn("spare pool", content)

    def test_companion_detail_shows_tether_to_unracked_host(self) -> None:
        response = self.client.get(f"/devices/{self.companion.pk}/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(f'data-tether-pk="{self.host.pk}"', content)
        self.assertIn("spare pool", content)


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
        """Stage B: every one of the eight ``/models/<slug>/`` list pages
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

        factories = {
            "vlan": lambda i: VLAN.objects.create(name=f"QB VLAN {i}", vlan_id=2000 + i),
            "switchportvlanprofile": lambda i: SwitchPortVlanProfile.objects.create(
                name=f"QB Profile {i}", native_vlan=native_vlan
            ),
            "racktemplate": lambda i: RackTemplate.objects.create(name=f"QB Template {i}"),
            "rack": lambda i: Rack.objects.create(name=f"QB Rack {i}", slot_count=1),
            "networkswitchtype": lambda i: NetworkSwitchType.objects.create(
                manufacturer="QB", model="M", name=f"QB SwitchType {i}", port_count=0
            ),
            "networkswitch": lambda i: NetworkSwitch.objects.create(
                switch_type=switch_type, hostname=f"qb-switch-{i}"
            ),
            "networkdevicetype": lambda i: NetworkDeviceType.objects.create(
                manufacturer="QB", model="M", name=f"QB DeviceType {i}", port_count=0
            ),
            "networkdevice": lambda i: NetworkDevice.objects.create(
                device_type=device_type, hostname=f"qb-device-{i}"
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
        routes = ["/audit/"]
        for slug in REGISTRY:
            routes.append(self._list_url(slug))
            routes.append(self._detail_url(slug))
        return routes

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


class ParityContentTests(ParityFixtureMixin, TestCase):
    """Read-parity content, asserted with distinctive fixture values
    (review note 7) — never a bare ``assertContains(response, "VLAN")``.
    """

    def setUp(self) -> None:
        super().setUp()
        self.client.login(username="stageb-admin", password="testpass123")

    def test_vlan_list_and_detail_render_declared_fields(self) -> None:
        list_response = self.client.get(self._list_url("vlan"))
        self.assertContains(list_response, "StageB Native")
        self.assertContains(list_response, "10.210.0.0/21")
        self.assertContains(list_response, "10.210.0.1")

        detail_response = self.client.get(self._detail_url("vlan"))
        self.assertContains(detail_response, "StageB Native")
        self.assertContains(detail_response, "10.210.0.0/21")
        self.assertContains(detail_response, "10.210.0.1")

    def test_switchportvlanprofile_allowed_vlans_m2m_renders_both(self) -> None:
        # The value a naive `_meta.fields` walk would have dropped
        # entirely — allowed_vlans is a form field in the admin, not a
        # model field or an inline (review note 2).
        for response in (
            self.client.get(self._list_url("switchportvlanprofile")),
            self.client.get(self._detail_url("switchportvlanprofile")),
        ):
            self.assertContains(response, "StageB Allowed One")
            self.assertContains(response, "StageB Allowed Two")
            self.assertContains(response, "StageB Profile")
            self.assertContains(response, "Trunk")
            self.assertContains(response, "StageB Native")  # native_vlan relation

    def test_racktemplate_vlans_m2m_renders_both(self) -> None:
        for response in (
            self.client.get(self._list_url("racktemplate")),
            self.client.get(self._detail_url("racktemplate")),
        ):
            self.assertContains(response, "StageB Allowed One")
            self.assertContains(response, "StageB Allowed Two")
            self.assertContains(response, "StageB Template")

    def test_rack_list_renders_and_detail_redirects_to_elevation(self) -> None:
        list_response = self.client.get(self._list_url("rack"))
        self.assertContains(list_response, "StageB Rack")
        detail_response = self.client.get(self._detail_url("rack"))
        self.assertEqual(detail_response.status_code, 301)

    def test_networkswitchtype_type_ports_inline_renders_columns(self) -> None:
        response = self.client.get(self._detail_url("networkswitchtype"))
        self.assertContains(response, "StageB Switch Type")
        self.assertContains(response, "StageB Profile")  # the type port's profile relation

    def test_networkswitch_addresses_and_ports_inline_render_profile_summary_matches(self) -> None:
        list_response = self.client.get(self._list_url("networkswitch"))
        self.assertContains(list_response, "stageb-switch1")
        self.assertContains(list_response, "SBSW001")
        self.assertContains(list_response, "StageB Switch Type")

        detail_response = self.client.get(self._detail_url("networkswitch"))
        self.assertContains(detail_response, "stageb-switch1")
        # Addresses inline — the switch's materialized static address on the racked VLAN.
        assert self.switch_address.address is not None  # materialized (rack + RackVlanRange), never DHCP
        self.assertContains(detail_response, self.switch_address.address)
        # Ports inline — the profile_summary computed column must match
        # switch_port_profile_summary() exactly (review note 2's "reuse,
        # do not reimplement the formatting twice").
        expected_summary = switch_port_profile_summary(self.switch_port)
        self.assertContains(detail_response, expected_summary)

    def test_networkdevicetype_type_ports_inline_renders_columns(self) -> None:
        response = self.client.get(self._detail_url("networkdevicetype"))
        self.assertContains(response, "StageB Device Port")
        self.assertContains(response, "StageB Native")  # the type port's vlan relation

    def test_networkdevice_list_renders_and_detail_redirects_to_device_page(self) -> None:
        list_response = self.client.get(self._list_url("networkdevice"))
        self.assertContains(list_response, "stageb-device1")
        self.assertContains(list_response, "SBDEV001")
        detail_response = self.client.get(self._detail_url("networkdevice"))
        self.assertEqual(detail_response.status_code, 301)

    def test_device_shaped_page_shows_port_number_offset_and_switch_port(self) -> None:
        # The three fields review note 3 found missing from Stage A's
        # device page: port number, the numeric slot_offset, and the
        # connected switch *port* (not just the switch).
        response = self.client.get(f"/devices/{self.device.pk}/")
        self.assertContains(response, "Port #")
        self.assertContains(response, "Offset")
        self.assertContains(response, "Switch port")
        self.assertContains(response, str(self.switch_port))

    def test_default_gateway_renders_and_dash_for_dhcp(self) -> None:
        racked_response = self.client.get(f"/devices/{self.device.pk}/")
        self.assertContains(racked_response, "10.210.0.1")  # the static port's default_gateway

        dhcp_response = self.client.get(f"/devices/{self.dhcp_device.pk}/")
        self.assertTrue(self.dhcp_device_port.is_dhcp)
        self.assertContains(dhcp_response, "DHCP")

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
        self.assertContains(response, "add")
        self.assertContains(response, str(vlan_a))

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
        self.assertContains(response, "deleted-actor@example.com")
        self.assertContains(response, "system or deleted actor")

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
        # NetworkSwitch is tracked with include_fields=["rack", "rack_slot",
        # "created_at"] (settings.py:263) — hostname/serial_number changes
        # are simply not tracked at all, which the page must say plainly.
        switch_type = _make_switch_type(port_count=0)
        switch = NetworkSwitch.objects.create(switch_type=switch_type, hostname="Audit Switch Original")
        switch_content_type = ContentType.objects.get_for_model(NetworkSwitch)
        count_after_create = LogEntry.objects.filter(
            content_type=switch_content_type, object_id=switch.pk
        ).count()

        switch.hostname = "Audit Switch Renamed"
        switch.save()

        # hostname isn't in NetworkSwitch's include_fields, so this rename
        # produces no *new* LogEntry at all (auditlog only writes an
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
        vlan_a = VLAN.objects.create(name="Filter VLAN A", vlan_id=231, subnet="10.231.0.0/24")
        VLAN.objects.create(name="Filter VLAN B", vlan_id=232, subnet="10.232.0.0/24")
        # auditlog only attaches an actor via its middleware, which reads
        # the current request's user — a plain save() outside a request
        # cycle has no request to read, so the actor filter needs
        # set_actor() to get a non-null actor onto this LogEntry at all.
        with set_actor(actor=self.admin_user):
            vlan_a.name = "Filter VLAN A Renamed"
            vlan_a.save()

        actor_response = self.client.get(f"/audit/?actor={self.admin_user.pk}")
        self.assertContains(actor_response, "Filter VLAN A Renamed")

        action_response = self.client.get(f"/audit/?action={LogEntry.Action.CREATE}")
        self.assertContains(action_response, "Filter VLAN B")
        self.assertNotContains(action_response, "Filter VLAN A Renamed")  # that entry is an UPDATE

        content_type_pk = ContentType.objects.get_for_model(VLAN).pk
        content_type_response = self.client.get(f"/audit/?content_type={content_type_pk}")
        self.assertContains(content_type_response, "Filter VLAN A Renamed")
        self.assertContains(content_type_response, "Filter VLAN B")

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
