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

from auditlog.models import LogEntry
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.models import User as DjangoUser
from django.core.management import call_command
from django.db import connection
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
    PortType,
    Rack,
    RackVlanRange,
)
from .suggestions import suggest_rack_vlan_range, suggest_slot_address
from .views import resolve_slot_spans, safe_slot_address

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
                self.assertEqual(self.client.get(url).status_code, 200, url)

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
